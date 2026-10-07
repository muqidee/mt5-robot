"""Deterministic execution tests; mocked signals make no profitability claim."""

from contextlib import contextmanager
import csv
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from bijisatu.backtest import run_backtest
from bijisatu.models import Bar, Signal


@contextmanager
def csv_file(text):
    # Keep fixtures inside the project, not a shared system temporary directory.
    path = Path(__file__).parent / f"_backtest_{uuid4().hex}.csv"
    try:
        path.write_text(text, encoding="utf-8")
        yield path
    finally:
        path.unlink(missing_ok=True)


def warmup():
    return [Bar(index * 60, 100, 100, 100, 100) for index in range(500)]


def csv_text(bars):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(("time", "open", "high", "low", "close", "tick_volume"))
    for bar in bars:
        writer.writerow((bar.time, bar.open, bar.high, bar.low, bar.close, bar.tick_volume))
    return stream.getvalue()


class BacktestTests(unittest.TestCase):
    def run_case(self, bars, *, side="buy", atr=1, signals=None, evaluate=None, **kwargs):
        options = dict(spread=0.1, commission_per_lot=0, value_per_price_unit=1)
        options.update(kwargs)
        if evaluate is None:
            def evaluate(m1, m5):
                if signals is None or m1[-1].time in signals:
                    return Signal(side, m1[-1].time, atr, "test fixture")
                return None
        with csv_file(csv_text(bars)) as path, patch("bijisatu.backtest.strategy.evaluate", side_effect=evaluate):
            result = run_backtest(path, **options)
        json.dumps(result, allow_nan=False)
        return result

    def test_next_open_and_only_closed_history(self):
        seen = []

        def evaluate(m1, m5):
            self.assertEqual(len(m1), 100)
            self.assertEqual(len(m5), 100)
            self.assertTrue(all(bar.time + 300 <= m1[-1].time + 60 for bar in m5))
            seen.append((m1[-1].time, m1[-1].close, m5[-1]))
            return Signal("buy", m1[-1].time, 1, "test")

        bars = warmup() + [Bar(30000, 110, 110, 110, 110)]
        result = self.run_case(bars, evaluate=evaluate, slippage=0.05)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0:2], (29940, 100))
        self.assertEqual(seen[0][2].time, 29700)
        trade = result["trades"][0]
        self.assertEqual(trade["signal_time"], 29940)
        self.assertEqual(trade["entry_time"], 30000)
        self.assertAlmostEqual(trade["entry_price"], 110.15)
        self.assertTrue(trade["forced_exit"])
        self.assertAlmostEqual(trade["exit_price"], 109.95)

    def test_signal_on_final_close_cannot_enter(self):
        self.assertEqual(self.run_case(warmup())["trade_count"], 0)

    def test_incomplete_m5_groups_are_not_used(self):
        bars = [bar for bar in warmup() if bar.time != 60]
        bars += [Bar(t, 100, 100, 100, 100) for t in range(30000, 30420, 60)]
        seen = []

        def evaluate(m1, m5):
            seen.append((m1, m5))
            return None

        self.run_case(bars, evaluate=evaluate)
        self.assertEqual(seen[0][0][-1].time, 30240)
        self.assertEqual(seen[0][1][0].time, 300)
        self.assertNotIn(0, [bar.time for bar in seen[0][1]])
        self.assertEqual(seen[1][1][-1].time, 30000)

    def test_initial_partial_m5_and_aggregation(self):
        bars = [Bar(t, 100, 102, 99, 101, 2) for t in range(60, 30300, 60)]
        seen = []

        def evaluate(m1, m5):
            seen.append(m5)
            return None

        self.run_case(bars, evaluate=evaluate)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], Bar(300, 100, 102, 99, 101, 10))
        self.assertEqual(seen[0][-1].time, 30000)

    def test_both_touches_stop_first_for_both_sides(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                result = self.run_case(warmup() + [Bar(30000, 100, 103, 97, 100)], side=side)
                trade = result["trades"][0]
                self.assertEqual(trade["exit_reason"], "stop_loss")
                self.assertAlmostEqual(trade["exit_price"], trade["sl"])
                self.assertLess(trade["pnl"], 0)

    def test_adverse_stop_gaps_exceed_planned_loss(self):
        for side, gap in (("buy", 90), ("sell", 110)):
            with self.subTest(side=side):
                bars = warmup() + [Bar(30000, 100, 100, 100, 100), Bar(30060, gap, gap, gap, gap)]
                result = self.run_case(bars, side=side, slippage=0.1, value_per_price_unit=100)
                trade = result["trades"][0]
                self.assertEqual(trade["exit_reason"], "stop_gap")
                expected = gap - 0.1 if side == "buy" else gap + 0.2
                self.assertAlmostEqual(trade["exit_price"], expected)
                self.assertLess(trade["pnl"], -500)
                self.assertEqual(result["daily_limit_days"], 1)
                self.assertGreater(result["max_drawdown_fraction"], 0.05)

    def test_tp_gap_no_improvement_and_open_precedes_later_extremes(self):
        bars = warmup() + [Bar(30000, 100, 100, 100, 100), Bar(30060, 110, 110, 90, 100)]
        result = self.run_case(bars, slippage=0.05)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "take_profit")
        self.assertAlmostEqual(trade["exit_price"], trade["tp"] - 0.05)

    def test_costs_and_floored_volume(self):
        bars = warmup() + [Bar(30000, 100, 100, 100, 100)]
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                result = self.run_case(bars, side=side, initial_equity=1000, slippage=0.05,
                                       commission_per_lot=2, value_per_price_unit=10)
                trade = result["trades"][0]
                self.assertEqual(trade["volume"], 1.71)  # floor(30 / (15.5 + 2))
                self.assertAlmostEqual(trade["gross_pnl"], -0.2 * 10 * 1.71)
                self.assertAlmostEqual(trade["commission"], 2 * 1.71)
                self.assertAlmostEqual(result["net_profit"], -4 * 1.71)
                self.assertAlmostEqual(result["final_equity"], 1000 - 4 * 1.71)
                self.assertEqual(result["total_commission"], trade["commission"])
                self.assertEqual(result["forced_exits"], 1)

    def test_cent_currency_rescaling_preserves_lots(self):
        bars = warmup() + [Bar(30000, 100, 103, 98, 100)]
        regular = self.run_case(bars, initial_equity=1000, commission_per_lot=2, value_per_price_unit=10)
        cents = self.run_case(bars, initial_equity=100000, commission_per_lot=200, value_per_price_unit=1000)
        self.assertEqual(regular["trades"][0]["volume"], cents["trades"][0]["volume"])
        self.assertAlmostEqual(cents["net_profit"], regular["net_profit"] * 100)
        self.assertAlmostEqual(cents["max_drawdown_fraction"], regular["max_drawdown_fraction"])

    def test_five_minute_cooldown_and_single_position(self):
        bars = warmup() + [Bar(t, 100, 103, 97, 100) for t in range(30000, 30360, 60)]
        result = self.run_case(bars, daily_loss_fraction=1, risk_fraction=0.001)
        self.assertEqual([trade["entry_time"] for trade in result["trades"]], [30000, 30300])
        flat = warmup() + [Bar(t, 100, 100, 100, 100) for t in range(30000, 30900, 60)]
        self.assertEqual(self.run_case(flat)["trade_count"], 1)

    def test_spread_ratio_and_minimum_lot_gate(self):
        bars = warmup() + [Bar(30000, 100, 100, 100, 100)]
        self.assertEqual(self.run_case(bars, spread=0.150001)["trade_count"], 0)
        self.assertEqual(self.run_case(bars, spread=0.15)["trade_count"], 1)
        self.assertEqual(self.run_case(bars, initial_equity=1, value_per_price_unit=100)["trade_count"], 0)

    def test_daily_remaining_budget_caps_volume(self):
        bars = warmup() + [Bar(t, 100, 103, 97, 100) for t in range(30000, 30360, 60)]
        result = self.run_case(bars, initial_equity=1000, spread=0, value_per_price_unit=10)
        first, second = result["trades"]
        self.assertEqual(first["volume"], 2)
        self.assertEqual(second["volume"], 1.33)
        self.assertGreaterEqual(result["final_equity"], 950)

    def test_floating_daily_latch_survives_recovery_until_next_offset_day(self):
        # +2 hours puts the next broker-day boundary at UTC 79200.
        boundary = 79200
        bars = warmup() + [
            Bar(30000, 100, 110, 100, 110),
            Bar(boundary, 110, 110, 90, 110),
            Bar(boundary + 60, 110, 124, 110, 123),
            Bar(boundary + 600, 100, 100, 100, 100),
        ] + [
            Bar(t, 100, 100, 100, 100)
            for t in range(boundary + 86400 - 300, boundary + 86400, 60)
        ] + [
            Bar(boundary + 86400, 100, 100, 100, 100),
            Bar(boundary + 86460, 100, 100, 100, 100),
        ]
        result = self.run_case(bars, atr=10, initial_equity=1000, risk_fraction=0.5,
                               daily_loss_fraction=0.1, spread=0, broker_utc_offset_hours=2,
                               max_open_risk_fraction=0.5)
        self.assertEqual(result["trade_count"], 2)
        first, second = result["trades"]
        self.assertEqual(first["exit_reason"], "take_profit")
        self.assertGreater(first["pnl"], 0)
        self.assertEqual(second["entry_time"], boundary + 86460)
        self.assertTrue(result["days"][1]["limit_reached"])
        self.assertGreater(result["days"][1]["ending_equity"], result["days"][1]["starting_equity"])
        self.assertFalse(result["days"][2]["limit_reached"])

    def test_gap_daily_latch_blocks_reentry_same_day(self):
        bars = warmup() + [Bar(30000, 100, 100, 100, 100)]
        bars += [Bar(t, 90, 90, 90, 90) for t in range(30060, 31200, 60)]
        result = self.run_case(bars, value_per_price_unit=100)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["daily_limit_days"], 1)

    def test_bad_or_stale_signal_fails_closed(self):
        bars = warmup() + [Bar(30000, 100, 100, 100, 100)]
        for signal in (Signal("buy", 0, 1, "stale"), Signal("buy", 29940, float("nan"), "bad"),
                       Signal("buy", 29940, 0, "bad"), Signal("other", 29940, 1, "bad")):
            with self.subTest(signal=signal):
                result = self.run_case(bars, evaluate=lambda m1, m5: signal)
                self.assertEqual(result["trade_count"], 0)

    def test_iso_timezone_and_unix_are_equivalent(self):
        header = "time,open,high,low,close\n"
        text = header + "1970-01-01T02:00:00+02:00,100,100,100,100\n1970-01-01T00:01:00Z,100,100,100,100\n120,100,100,100,100\n"
        with csv_file(text) as path:
            result = run_backtest(path, spread=0, commission_per_lot=0, value_per_price_unit=1)
        self.assertEqual(result["start_time"], 0)
        self.assertEqual(result["end_time"], 180)
        self.assertEqual(result["trade_count"], 0)

    def test_reject_bad_csv(self):
        header = "time,open,high,low,close\n"
        rows = [
            "", "0,100,100,100,nan\n", "0,100,inf,100,100\n",
            "0,100,99,98,100\n", "0,100,101,100,99\n", "0,0,1,0,1\n",
            "1,100,100,100,100\n", "-60,100,100,100,100\n",
            "0,100,100,100,100\n0,100,100,100,100\n",
            "60,100,100,100,100\n0,100,100,100,100\n",
            "1970-01-01T00:00:00,100,100,100,100\n",
            "1970-01-01T00:00:00.5Z,100,100,100,100\n",
            "not-a-time,100,100,100,100\n", "0,100,100,100\n", "0,100,100,100,100,extra\n",
        ]
        texts = [header + row for row in rows] + ["open,close\n100,100\n", "time,open,high,low,close,time\n"]
        for text in texts:
            with self.subTest(text=text), csv_file(text) as path, self.assertRaises(ValueError):
                run_backtest(path, spread=0, commission_per_lot=0, value_per_price_unit=1)

    def test_reject_invalid_parameters(self):
        for key, value in (("spread", -1), ("slippage", -1), ("commission_per_lot", -1),
                           ("initial_equity", 0), ("value_per_price_unit", 0),
                           ("risk_fraction", 0), ("risk_fraction", 1.1),
                           ("daily_loss_fraction", 0), ("daily_loss_fraction", 2),
                           ("volume_step", 0), ("volume_min", 101),
                           ("volume_max", 0), ("broker_utc_offset_hours", 25),
                           ("spread", float("nan")), ("slippage", float("inf")), ("spread", True),
                           ("max_open_risk_fraction", 0), ("max_open_risk_fraction", 1),
                           ("max_open_risk_fraction", -.1), ("max_open_risk_fraction", True),
                           ("max_open_risk_fraction", "0.05"),
                           ("max_open_risk_fraction", float("nan"))):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.run_case(warmup(), **{key: value})

    def test_open_risk_ceiling_caps_single_position_sizing(self):
        bars = warmup() + [Bar(30000, 100, 100, 100, 100)]
        result = self.run_case(bars, initial_equity=1000, risk_fraction=.2,
                               daily_loss_fraction=.5, max_open_risk_fraction=.005,
                               commission_per_lot=2, value_per_price_unit=10, risk_buffer=1.2)
        trade = result["trades"][0]
        self.assertEqual(trade["volume"], .24)
        self.assertLessEqual(trade["volume"] * (15 + 2) * 1.2, 5)
        self.assertEqual(result["parameters"]["max_open_risk_fraction"], .005)

    def test_real_strategy_flat_data_has_no_trades(self):
        with csv_file(csv_text(warmup())) as path:
            result = run_backtest(path, spread=0.1, commission_per_lot=2, value_per_price_unit=10)
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["final_equity"], 10000)
        self.assertEqual(result["max_drawdown"], 0)
        self.assertTrue(result["assumptions"])


if __name__ == "__main__":
    unittest.main()

