"""Offline timeframe and configuration regressions; no terminal is initialized."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from uuid import uuid4

from bijisatu.backtest import _aggregate_closed, run_backtest
from bijisatu.broker import MT5Broker
from bijisatu.cli import main
from bijisatu.config import Config, load_config
from bijisatu.engine import Engine
from bijisatu.models import Account, Bar, Signal, SymbolSpec, Tick
from bijisatu.state import StateStore
from bijisatu.strategy import evaluate, timeframes
from test_backtest import csv_file, csv_text, warmup


def candle(time, close, opening=None):
    opening = close if opening is None else opening
    return Bar(time, opening, max(opening, close) + .2,
               min(opening, close) - .2, close, 10)


def intraday_bars():
    trend = [candle(index * 3600, 80 + index * .2) for index in range(100)]
    entry = [candle(270000 + index * 900, 100) for index in range(100)]
    entry[-2] = candle(entry[-2].time, 99, 100)
    entry[-1] = candle(entry[-1].time, 101, 99.2)
    return entry, trend


class IntradayStrategyTests(unittest.TestCase):
    def test_defaults_and_generalized_close_cutoff(self):
        self.assertEqual(timeframes(Config().strategy_mode), (1, 5))
        self.assertEqual(timeframes("intraday"), (15, 60))
        entry, trend = intraday_bars()
        signal = evaluate(entry, trend, entry_minutes=15, trend_minutes=60)
        self.assertEqual(signal.side, "buy")
        self.assertIn("H1", signal.reason)
        self.assertIn("M15", signal.reason)
        future = replace(candle(359100, 500), close=float("nan"))
        self.assertEqual(evaluate(entry, trend + [future], entry_minutes=15, trend_minutes=60), signal)
        self.assertEqual(trend[-1].time + 3600, entry[-1].time + 900)
        entry = [replace(bar, time=bar.time - 1) for bar in entry]
        self.assertIsNone(evaluate(entry, trend, entry_minutes=15, trend_minutes=60))

    def test_incomplete_h1_never_supplies_warmup(self):
        entry, trend = intraday_bars()
        trend[-1] = replace(trend[-1], time=trend[-1].time + 1)
        self.assertIsNone(evaluate(entry, trend, entry_minutes=15, trend_minutes=60))

    def test_symmetric_intraday_sell(self):
        entry, trend = intraday_bars()
        def reflect(bar):
            return Bar(bar.time, 200 - bar.open, 200 - bar.low,
                       200 - bar.high, 200 - bar.close, bar.tick_volume)
        signal = evaluate([reflect(bar) for bar in entry], [reflect(bar) for bar in trend],
                          entry_minutes=15, trend_minutes=60)
        self.assertEqual(signal.side, "sell")

    def test_invalid_timeframe_arguments(self):
        for entry, trend in ((0, 60), (15, 15), (15, 61), (True, 60), (15, "60")):
            with self.subTest(entry=entry, trend=trend), self.assertRaises(ValueError):
                evaluate([], [], entry_minutes=entry, trend_minutes=trend)

    def test_complete_aligned_groups_only(self):
        bars = [Bar(index * 60, 100, 102, 99, 101, 2) for index in range(60)]
        self.assertEqual(_aggregate_closed(bars, 60), Bar(0, 100, 102, 99, 101, 120))
        self.assertEqual(_aggregate_closed(bars, 15), Bar(2700, 100, 102, 99, 101, 30))
        for duration in (15, 60):
            with self.subTest(duration=duration):
                self.assertIsNone(_aggregate_closed(bars[:-1], duration))
                self.assertIsNone(_aggregate_closed(bars[:-2] + bars[-1:], duration))
                self.assertIsNone(_aggregate_closed(bars[-duration + 1:], duration))


class IntradayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / (".intraday-test-" + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.store = StateStore(self.directory / "state.json")
        self.config = Config(strategy_mode="intraday", risk_fraction=.005,
                             daily_loss_fraction=.02, cooldown_seconds=900,
                             broker_utc_offset_hours=0, commission_per_lot=0)
        self.now = 360000
        self.entry, self.trend = intraday_bars()
        self.broker = Mock(spec=MT5Broker)
        self.broker.symbols.return_value = ["TEST"]
        self.broker.account.return_value = Account(123, "test", "USD", 10000, 10000, True, True)
        self.broker.positions.return_value = []
        self.broker.activity.return_value = (False, False)
        self.broker.has_orders.return_value = False
        self.broker.spec.return_value = SymbolSpec("TEST", .01, 2, .01, .01, 100, .01, 0)
        self.broker.tick.side_effect = lambda symbol: Tick(101, 101.01, self.now)
        self.broker.bars.side_effect = lambda symbol, minutes: self.entry if minutes == 15 else self.trend
        self.broker.profit.side_effect = lambda side, symbol, volume, entry, exit_price: volume * (exit_price - entry)

    def step(self):
        return Engine(self.broker, self.config, self.store).step(self.now)

    def test_real_evaluator_uses_m15_h1_and_never_connects_or_sends(self):
        result = self.step()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["plan"]["bar_time"], self.entry[-1].time)
        self.broker.bars.assert_has_calls([call("TEST", 15), call("TEST", 60)])
        self.broker.connect.assert_not_called()
        self.broker.send.assert_not_called()

    def test_stale_and_future_entry_or_trend_fail_closed(self):
        original_entry, original_trend = self.entry, self.trend
        for which, shift in (("entry", -931), ("entry", 1), ("trend", -3691), ("trend", 1)):
            with self.subTest(which=which, shift=shift):
                self.entry, self.trend = original_entry, original_trend
                bars = getattr(self, which)
                setattr(self, which, [replace(bar, time=bar.time + shift) for bar in bars])
                with patch("bijisatu.engine.evaluate") as evaluator:
                    self.assertEqual(self.step()["skipped"], {"stale_or_future_candles": 1})
                    evaluator.assert_not_called()
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_h1_closing_after_entry_cutoff_does_not_supply_live_warmup(self):
        self.entry = [replace(bar, time=bar.time - 900) for bar in self.entry]
        self.now += 1
        result = self.step()
        self.assertEqual(result["skipped"], {"insufficient_history": 1})
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.send.assert_not_called()

    def test_reduced_daily_limit_halts_against_existing_baseline(self):
        self.store.start_day("1970-01-05", 10000, self.now - 60)
        self.broker.account.return_value = replace(self.broker.account.return_value, equity=9799)
        self.assertEqual(self.step()["status"], "halted")
        self.assertEqual(self.store.data["baseline"], 10000)
        self.assertTrue(StateStore(self.directory / "state.json").data["halted"])
        self.broker.bars.assert_not_called()

    def test_intraday_signal_expires_during_scan(self):
        elapsed = [0.0]
        def delayed(*args, **kwargs):
            elapsed[0] = 931
            return Signal("buy", self.entry[-1].time, 1, "fixture")
        with patch("bijisatu.engine.evaluate", side_effect=delayed), patch(
            "bijisatu.engine.time.monotonic", side_effect=lambda: elapsed[0]
        ):
            self.assertEqual(self.step()["skipped"], {"signal_expired_during_scan": 1})
        self.assertEqual(self.store.data["attempts"], {})

    def test_trend_freshness_is_rechecked_after_scan(self):
        self.trend = [replace(bar, time=bar.time - 3650) for bar in self.trend]
        elapsed = [0.0]
        def delayed(*args, **kwargs):
            elapsed[0] = 41
            return Signal("buy", self.entry[-1].time, 1, "fixture")
        with patch("bijisatu.engine.evaluate", side_effect=delayed), patch(
            "bijisatu.engine.time.monotonic", side_effect=lambda: elapsed[0]
        ):
            self.assertEqual(self.step()["skipped"], {"signal_expired_during_scan": 1})
        self.assertEqual(self.store.data["attempts"], {})

    def test_warmup_and_persisted_cooldown_and_deduplication(self):
        self.trend = self.trend[-99:]
        self.assertEqual(self.step()["skipped"], {"insufficient_history": 1})
        self.entry, self.trend = intraday_bars()
        self.assertEqual(self.step()["status"], "dry_run")
        self.now += 899
        self.assertEqual(self.step()["skipped"], {"cooldown": 1})
        self.now += 2
        self.store = StateStore(self.directory / "state.json")
        self.assertEqual(self.step()["skipped"], {"signal_already_processed": 1})

    def test_existing_daily_halt_survives_new_strategy(self):
        self.store.start_day("1970-01-05", 10000, self.now - 60)
        self.store.halt("Saved loss halt")
        self.assertEqual(self.step()["status"], "halted")
        self.assertEqual(self.store.data["baseline"], 10000)
        self.broker.bars.assert_not_called()

    def test_intraday_uses_same_100_bar_window_as_backtest(self):
        self.entry = [candle(269100, 500)] + self.entry
        self.trend = [candle(0, 500)] + [replace(bar, time=bar.time + 3600) for bar in self.trend]
        self.now += 3600
        self.entry = [replace(bar, time=bar.time + 3600) for bar in self.entry]
        with patch("bijisatu.engine.evaluate", return_value=None) as evaluator:
            self.step()
        args, kwargs = evaluator.call_args
        self.assertEqual([len(bars) for bars in args], [100, 100])
        self.assertEqual(kwargs, dict(entry_minutes=15, trend_minutes=60))


class IntradayBacktestTests(unittest.TestCase):
    def test_backtest_selection_no_lookahead_and_incomplete_h1(self):
        bars = [Bar(index * 60, 100, 100, 100, 100) for index in range(6060)]
        seen = []
        def evaluator(entry, trend, **kwargs):
            self.assertEqual(kwargs, dict(entry_minutes=15, trend_minutes=60))
            self.assertEqual((len(entry), len(trend)), (100, 100))
            self.assertLessEqual(trend[-1].time + 3600, entry[-1].time + 900)
            seen.append((entry[-1].time, trend[-1].time))
            return None
        with csv_file(csv_text(bars)) as path, patch("bijisatu.backtest.strategy.evaluate", side_effect=evaluator):
            result = run_backtest(path, strategy_mode="intraday", spread=.1,
                                  commission_per_lot=0, value_per_price_unit=1)
        self.assertEqual(seen, [(359100 + offset, 356400) for offset in range(0, 3600, 900)]
                         + [(362700, 360000)])
        self.assertEqual(result["parameters"]["entry_minutes"], 15)
        self.assertEqual(result["trade_count"], 0)
        missing = [bar for bar in bars if bar.time != 60]
        with csv_file(csv_text(missing)) as path, patch("bijisatu.backtest.strategy.evaluate", return_value=None) as evaluator:
            run_backtest(path, strategy_mode="intraday", spread=0, commission_per_lot=0,
                         value_per_price_unit=1)
            self.assertEqual(evaluator.call_count, 1)
            self.assertEqual(evaluator.call_args.args[1][0].time, 3600)

    def test_backtest_custom_stop_reward_spread_cooldown_and_buffer(self):
        bars = warmup() + [Bar(t, 100, 110, 90, 100) for t in range(30000, 31260, 60)]
        def evaluator(entry, trend):
            return Signal("buy", entry[-1].time, 1, "fixture")
        options = dict(spread=.2, commission_per_lot=1, value_per_price_unit=10,
                       stop_atr=2, reward_ratio=3, max_spread_atr=.25,
                       cooldown_seconds=900, risk_buffer=1.2, risk_fraction=.005)
        with csv_file(csv_text(bars)) as path, patch("bijisatu.backtest.strategy.evaluate", side_effect=evaluator):
            result = run_backtest(path, **options)
            rejected = run_backtest(path, **(options | dict(max_spread_atr=.15)))
        self.assertEqual([trade["entry_time"] for trade in result["trades"]], [30000, 30900])
        trade = result["trades"][0]
        self.assertAlmostEqual(trade["entry_price"] - trade["sl"], 2)
        self.assertAlmostEqual(trade["tp"] - trade["entry_price"], 6)
        self.assertEqual(trade["volume"], 1.98)
        self.assertEqual(rejected["trade_count"], 0)
        for name, value in options.items():
            self.assertEqual(result["parameters"][name], value)

    def test_intraday_trade_uses_selected_parameters_and_next_minute_open(self):
        bars = [Bar(index * 60, 100, 100, 100, 100) for index in range(6001)]
        def evaluator(entry, trend, **kwargs):
            return Signal("buy", entry[-1].time, 1, "fixture")
        with csv_file(csv_text(bars)) as path, patch("bijisatu.backtest.strategy.evaluate", side_effect=evaluator):
            result = run_backtest(path, strategy_mode="intraday", spread=.2,
                                  commission_per_lot=0, value_per_price_unit=1,
                                  stop_atr=2, reward_ratio=3, max_spread_atr=.25,
                                  cooldown_seconds=900)
        trade = result["trades"][0]
        self.assertEqual(trade["signal_time"], 359100)
        self.assertEqual(trade["entry_time"], 360000)
        self.assertAlmostEqual(trade["entry_price"] - trade["sl"], 2)
        self.assertAlmostEqual(trade["tp"] - trade["entry_price"], 6)
        self.assertEqual(result["parameters"]["cooldown_seconds"], 900)

    def test_pending_entry_is_discarded_after_large_gap(self):
        bars = warmup() + [Bar(36000, 100, 100, 100, 100)]
        with csv_file(csv_text(bars)) as path, patch("bijisatu.backtest.strategy.evaluate", return_value=Signal("buy", 29940, 1, "fixture")):
            result = run_backtest(path, spread=0, commission_per_lot=0, value_per_price_unit=1)
        self.assertEqual(result["trade_count"], 0)

    def test_invalid_config_and_backtest_strategy_settings(self):
        for value in ("unknown", "M15", None, 15, True, []):
            with self.subTest(mode=value), self.assertRaises(ValueError):
                Config(strategy_mode=value).validate()
        options = dict(spread=0, commission_per_lot=0, value_per_price_unit=1)
        for name, value in (("strategy_mode", "unknown"), ("stop_atr", 0), ("stop_atr", True),
                            ("reward_ratio", float("nan")), ("max_spread_atr", 1),
                            ("cooldown_seconds", 0), ("cooldown_seconds", 1.5),
                            ("cooldown_seconds", True), ("risk_buffer", .9)):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                run_backtest(Path("never-read.csv"), **(options | {name: value}))

    def test_cli_config_is_offline_and_explicit_flags_override(self):
        with csv_file(json.dumps(dict(strategy_mode="intraday", stop_atr=2, reward_ratio=3,
                                     max_spread_atr=.2, cooldown_seconds=900,
                                     risk_fraction=.005, daily_loss_fraction=.02,
                                     risk_buffer=1.3, max_open_risk_fraction=.06, commission_per_lot=4,
                                     broker_utc_offset_hours=2))) as config_path:
            config = load_config(config_path)
            with patch("bijisatu.cli.MT5Broker") as broker, patch("bijisatu.backtest.run_backtest", return_value={}) as simulator:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["backtest", "offline.csv", "--config", str(config_path),
                                           "--spread", ".1", "--value-per-price-unit", "10"]), 0)
                kwargs = simulator.call_args.kwargs
                for key in ("strategy_mode", "stop_atr", "reward_ratio", "max_spread_atr",
                            "cooldown_seconds", "risk_fraction", "daily_loss_fraction",
                            "risk_buffer", "max_open_risk_fraction", "commission_per_lot", "broker_utc_offset_hours"):
                    self.assertEqual(kwargs[key], getattr(config, key))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["backtest", "offline.csv", "--config", str(config_path),
                                           "--spread", ".1", "--value-per-price-unit", "10",
                                           "--strategy-mode", "scalping", "--stop-atr", "3",
                                           "--reward-ratio", "2", "--max-spread-atr", ".3",
                                           "--cooldown-seconds", "600", "--risk", ".01",
                                           "--daily-loss", ".04", "--max-open-risk", ".03",
                                           "--commission-per-lot", "0"]), 0)
                kwargs = simulator.call_args.kwargs
                self.assertEqual(kwargs["strategy_mode"], "scalping")
                self.assertEqual(kwargs["max_open_risk_fraction"], .03)
                self.assertEqual((kwargs["stop_atr"], kwargs["reward_ratio"], kwargs["max_spread_atr"],
                                  kwargs["cooldown_seconds"], kwargs["risk_fraction"],
                                  kwargs["daily_loss_fraction"], kwargs["commission_per_lot"]),
                                 (3, 2, .3, 600, .01, .04, 0))
                broker.assert_not_called()
        with csv_file('{"strategy_mode": "unknown"}') as path, redirect_stderr(io.StringIO()), patch("bijisatu.backtest.run_backtest") as simulator:
            self.assertEqual(main(["backtest", "offline.csv", "--config", str(path), "--spread", "0", "--value-per-price-unit", "1"]), 2)
            simulator.assert_not_called()

    def test_broker_timeframe_constants_without_terminal(self):
        broker = MT5Broker(Config())
        broker.mt5 = SimpleNamespace(TIMEFRAME_M1=1, TIMEFRAME_M5=5,
                                     TIMEFRAME_M15=15, TIMEFRAME_H1=16385,
                                     copy_rates_from_pos=Mock(return_value=[]))
        for minutes, constant in ((1, 1), (5, 5), (15, 15), (60, 16385)):
            self.assertEqual(broker.bars("TEST", minutes), [])
            broker.mt5.copy_rates_from_pos.assert_called_with("TEST", constant, 1, 300)
        for minutes in (2, 0, True, 15.0):
            with self.assertRaises(ValueError):
                broker.bars("TEST", minutes)


if __name__ == "__main__":
    unittest.main()

