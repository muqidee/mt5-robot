"""Synthetic, offline H1/H4 behavior; no market performance validation."""

from contextlib import redirect_stdout
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
from bijisatu.cli import main, parser
from bijisatu.config import Config, load_config
from bijisatu.engine import Engine
from bijisatu.models import Account, Bar, SymbolSpec, Tick
from bijisatu.ollama import OllamaEntryFilter, market_snapshot
from bijisatu.state import StateStore
from bijisatu.strategy import assess, evaluate, fresh_candle, timeframes
from test_backtest import csv_file, csv_text
from test_intraday import candle


def short_term_bars():
    trend = [candle(index * 14400, 80 + index * .2) for index in range(100)]
    entry = [candle(1080000 + index * 3600, 100) for index in range(100)]
    entry[-2] = candle(entry[-2].time, 99, 100)
    entry[-1] = candle(entry[-1].time, 101, 99.2)
    return entry, trend


class ShortTermStrategyTests(unittest.TestCase):
    def assessment(self, entry, trend):
        return assess(entry, trend, entry_minutes=60, trend_minutes=240)

    def test_mode_and_symmetric_signal_labels(self):
        self.assertEqual(timeframes("short_term"), (60, 240))
        self.assertEqual(timeframes("scalping"), (1, 5))
        self.assertEqual(timeframes("intraday"), (15, 60))
        entry, trend = short_term_bars()
        result = self.assessment(entry, trend)
        self.assertEqual(result.signal.side, "buy")
        self.assertIn("H4", result.signal.reason)
        self.assertIn("H1", result.signal.reason)
        self.assertEqual(evaluate(entry, trend, entry_minutes=60, trend_minutes=240), result.signal)
        def reflect(bar):
            return Bar(bar.time, 200 - bar.open, 200 - bar.low,
                       200 - bar.high, 200 - bar.close, bar.tick_volume)
        self.assertEqual(self.assessment([reflect(b) for b in entry], [reflect(b) for b in trend]).signal.side, "sell")

    def test_future_h4_excluded_exactly_including_warmup(self):
        entry, trend = short_term_bars()
        expected = self.assessment(entry, trend)
        future = replace(candle(trend[-1].time + 14400, 500), close=float("nan"))
        result = self.assessment(entry, trend + [future])
        self.assertEqual(result.signal, expected.signal)
        self.assertEqual(result.diagnostics["future_trend_bars_excluded"], 1)
        trend[-1] = replace(trend[-1], time=trend[-1].time + 1)
        result = self.assessment(entry, trend)
        self.assertIsNone(result.signal)
        self.assertEqual(result.diagnostics["closed_trend_bars"], 99)
        self.assertEqual(result.diagnostics["rejected_reasons"], ["insufficient_history"])
        self.assertFalse(result.diagnostics["sufficient_history"])

    def test_actionable_conditions_and_finite_serialization(self):
        entry, trend = short_term_bars()
        result = self.assessment(entry, trend)
        details = result.diagnostics
        self.assertEqual(details["trend_direction"], "buy")
        self.assertEqual(details["rejected_reasons"], [])
        self.assertEqual(len(details["trend_candles"]), 3)
        self.assertTrue(all(details["conditions"]["buy"].values()))
        self.assertIn("previous_vs_ema20", details["entry"])
        self.assertGreater(details["atr14"], 0)
        json.dumps(details, allow_nan=False)
        entry[-2] = candle(entry[-2].time, 101)
        self.assertIn("no_pullback", self.assessment(entry, trend).diagnostics["rejected_reasons"])
        entry, trend = short_term_bars()
        entry[-1] = candle(entry[-1].time, 98, 99)
        self.assertIn("no_reclaim", self.assessment(entry, trend).diagnostics["rejected_reasons"])
        entry, trend = short_term_bars()
        entry[-1] = candle(entry[-1].time, 101, 102)
        self.assertIn("no_directional_candle", self.assessment(entry, trend).diagnostics["rejected_reasons"])
        trend = [candle(bar.time, 100) for bar in trend]
        self.assertEqual(self.assessment(entry, trend).diagnostics["rejected_reasons"], ["no_trend"])

    def test_history_invalid_values_and_freshness_windows(self):
        entry, trend = short_term_bars()
        for entries, trends in ((entry[-99:], trend), (entry, trend[-99:])):
            self.assertIsNone(self.assessment(entries, trends).signal)
        entry[-1] = replace(entry[-1], close=float("inf"))
        result = self.assessment(entry, trend)
        self.assertEqual(result.diagnostics["rejected_reasons"], ["invalid_candle_values"])
        json.dumps(result.diagnostics, allow_nan=False)
        for minutes, grace in ((60, 30), (240, 90)):
            close = trend[-1].time + minutes * 60
            self.assertTrue(fresh_candle(trend[-1], minutes, close + minutes * 60 + grace, grace))
            self.assertFalse(fresh_candle(trend[-1], minutes, close + minutes * 60 + grace + .001, grace))
            self.assertFalse(fresh_candle(trend[-1], minutes, close - .001, grace))

    def test_broker_uses_h4_metadata_constant_and_closed_positions(self):
        broker = MT5Broker(Config())
        broker.mt5 = SimpleNamespace(TIMEFRAME_H1=16385, TIMEFRAME_H4=16388,
                                     copy_rates_from_pos=Mock(return_value=[]))
        for duration, constant in ((60, 16385), (240, 16388)):
            self.assertEqual(broker.bars("TEST", duration), [])
            broker.mt5.copy_rates_from_pos.assert_called_with("TEST", constant, 1, 300)

    def test_contiguous_h1_h4_aggregation(self):
        bars = [Bar(index * 60, 100, 102, 99, 101, 2) for index in range(240)]
        self.assertEqual(_aggregate_closed(bars, 240), Bar(0, 100, 102, 99, 101, 480))
        self.assertEqual(_aggregate_closed(bars, 60), Bar(10800, 100, 102, 99, 101, 120))
        self.assertIsNone(_aggregate_closed(bars[:-1], 240))
        self.assertIsNone(_aggregate_closed(bars[:50] + bars[51:], 240))
        shifted = [replace(bar, time=bar.time + 7200) for bar in bars]
        self.assertIsNone(_aggregate_closed(shifted, 240))
        self.assertEqual(_aggregate_closed(shifted, 240, 7200), Bar(7200, 100, 102, 99, 101, 480))


class ShortTermRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / (".short-term-test-" + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.store = StateStore(self.directory / "state.json")
        self.config = Config(strategy_mode="short_term", risk_fraction=.15,
                             daily_loss_fraction=.2, max_open_risk_fraction=.2,
                             broker_utc_offset_hours=0, commission_per_lot=0,
                             ollama_observation_enabled=True)
        self.entry, self.trend = short_term_bars()
        self.now = 1440001
        self.broker = Mock(spec=MT5Broker)
        self.broker.symbols.return_value = ["TEST"]
        self.broker.account.return_value = Account(123, "test", "USD", 10000, 10000, True, True)
        self.broker.positions.return_value = []
        self.broker.activity.return_value = (False, False)
        self.broker.has_orders.return_value = False
        self.broker.spec.return_value = SymbolSpec("TEST", .01, 2, .01, .01, 10000, .01, 0)
        self.broker.tick.side_effect = lambda symbol: Tick(101, 101.01, self.now)
        self.broker.bars.side_effect = lambda symbol, minutes: self.entry if minutes == 60 else self.trend
        self.broker.profit.side_effect = lambda side, symbol, volume, entry, exit_price: volume * (exit_price - entry)

    def step(self, **kwargs):
        return Engine(self.broker, self.config, self.store, **kwargs).step(self.now)

    def test_h1_h4_requests_fixed_risk_and_observer_bindings(self):
        observer = Mock()
        with self.assertLogs("bijisatu", "DEBUG") as logs:
            result = self.step(observer=observer)
        self.assertEqual(result["status"], "dry_run", result)
        self.assertLessEqual(result["plan"]["risk_amount"], 1500)
        self.assertGreater(result["plan"]["risk_amount"], 1499)
        self.assertEqual(self.config.risk_fraction, .15)
        self.broker.bars.assert_has_calls([call("TEST", 60), call("TEST", 240)])
        self.assertEqual(observer.submit.call_args.args[2:4], (60, 240))
        assessment = next(r.details for r in logs.records if r.event == "strategy_assessment")
        self.assertEqual(assessment["assessment"]["trend_timeframe"], "H4")
        self.broker.connect.assert_not_called()
        self.broker.send.assert_not_called()

    def test_filter_binding_is_mode_and_timeframe_specific_without_network(self):
        self.config = replace(self.config, ollama_filter_enabled=True, ollama_observation_enabled=False)
        with patch("bijisatu.ollama.OllamaClient.judge", return_value={"decision": "allow", "reason": "Trend agrees"}):
            filter_ = OllamaEntryFilter(self.config)
            try:
                result = self.step(entry_filter=filter_)
                filter_.queue.join()
                binding = filter_._records["TEST"].binding
                self.assertEqual(binding.strategy_mode, "short_term")
                self.assertEqual(filter_.lookup(binding), "allowed")
                self.assertEqual(self.store.data["attempts"], {})
                self.assertIn("ollama_filter_pending", result["skipped"])
                self.assertEqual(self.step(entry_filter=filter_)["status"], "dry_run")
                self.assertEqual(filter_.lookup(replace(binding, strategy_mode="intraday")), "unavailable")
            finally:
                filter_.close()
        snapshot = market_snapshot("TEST", evaluate(self.entry, self.trend, entry_minutes=60, trend_minutes=240),
                                   60, 240, self.entry, self.trend, .01)
        self.assertEqual(snapshot["strategy_mode"], "short_term")
        self.assertNotIn("equity", snapshot)
        self.broker.send.assert_not_called()

    def test_future_h4_cannot_supply_live_warmup(self):
        self.entry = [replace(bar, time=bar.time - 3600) for bar in self.entry]
        self.assertEqual(self.step()["skipped"], {"insufficient_history": 1})
        self.assertEqual(self.store.data["attempts"], {})

    def test_future_and_stale_entry_and_trend(self):
        original_entry, original_trend = self.entry, self.trend
        for which, shift in (("entry", 2), ("entry", -3631), ("trend", 2), ("trend", -14491)):
            with self.subTest(which=which, shift=shift):
                self.entry, self.trend = original_entry, original_trend
                setattr(self, which, [replace(bar, time=bar.time + shift) for bar in getattr(self, which)])
                self.assertEqual(self.step()["skipped"], {"stale_or_future_candles": 1})
        self.assertEqual(self.store.data["attempts"], {})

    def test_100_bar_runtime_window_and_expiry_after_slow_response(self):
        self.entry = [candle(self.entry[0].time - 3600, 1000)] + self.entry
        self.trend = [candle(0, 1000)] + [replace(bar, time=bar.time + 14400) for bar in self.trend]
        self.entry = [replace(bar, time=bar.time + 14400) for bar in self.entry]
        self.now += 14400
        with patch("bijisatu.engine.evaluate", return_value=None) as evaluator:
            self.step()
        self.assertEqual([len(bars) for bars in evaluator.call_args.args], [100, 100])
        elapsed = [0.0]
        observer = Mock()
        observer.submit.side_effect = lambda *args: elapsed.__setitem__(0, 3631)
        with patch("bijisatu.engine.time.monotonic", side_effect=lambda: elapsed[0]):
            self.assertEqual(self.step(observer=observer)["skipped"], {"signal_expired_during_scan": 1})
        self.assertEqual(self.store.data["attempts"], {})

    def test_saved_halt_preserved_and_ticktime_safe_retry(self):
        self.broker.tick.side_effect = lambda symbol: Tick(101, 101.01, self.now if self.broker.tick.call_count == 1 else self.now - 16)
        result = self.step()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["retry_seconds"], self.config.poll_seconds)
        self.assertEqual(self.store.data["attempts"], {})
        self.store.halt("Saved halt")
        self.assertEqual(self.step()["status"], "halted")
        self.assertEqual(StateStore(self.directory / "state.json").data["reason"], "Saved halt")


class ShortTermBacktestTests(unittest.TestCase):
    def test_full_h4_warmup_and_no_lookahead_aggregated_h1_input(self):
        bars = [Bar(index * 60, 100, 100, 100, 100) for index in range(24061)]
        seen = []
        def evaluator(entry, trend, **kwargs):
            self.assertEqual(kwargs, {"entry_minutes": 60, "trend_minutes": 240})
            self.assertEqual((len(entry), len(trend)), (100, 100))
            self.assertLessEqual(trend[-1].time + 14400, entry[-1].time + 3600)
            seen.append(entry[-1].time)
            return None
        with csv_file(csv_text(bars)) as path, patch("bijisatu.backtest.strategy.evaluate", side_effect=evaluator):
            result = run_backtest(path, strategy_mode="short_term", risk_fraction=.15,
                                  daily_loss_fraction=.2, max_open_risk_fraction=.2,
                                  spread=0, commission_per_lot=0, value_per_price_unit=1)
        self.assertEqual(seen, [1436400, 1440000])
        self.assertEqual(result["parameters"]["risk_fraction"], .15)
        self.assertEqual(result["parameters"]["trend_minutes"], 240)
        self.assertEqual(result["trade_count"], 0)

    def test_real_strategy_backtest_h1_h4_signal_and_fixed_risk_parity(self):
        bars = []
        for hour in range(400):
            close = 80 + hour * .05 if hour < 300 else 100
            if hour == 398:
                close = 99
            elif hour == 399:
                close = 101
            for minute in range(60):
                opening = 99.2 if hour == 399 and minute == 0 else close
                bars.append(candle((hour * 60 + minute) * 60, close, opening))
        bars.append(candle(1440000, 101))
        with csv_file(csv_text(bars)) as path:
            result = run_backtest(path, strategy_mode="short_term", risk_fraction=.15,
                                  daily_loss_fraction=.2, max_open_risk_fraction=.2,
                                  spread=.01, commission_per_lot=0, value_per_price_unit=100,
                                  stop_atr=2, reward_ratio=3, risk_buffer=1.15)
        self.assertEqual(result["trade_count"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["signal_time"], 1436400)
        self.assertEqual(trade["entry_time"], 1440000)
        entry = [_aggregate_closed(bars[:(hour + 1) * 60], 60) for hour in range(300, 400)]
        trend = [_aggregate_closed(bars[:(index + 1) * 240], 240) for index in range(100)]
        signal = evaluate(entry, trend, entry_minutes=60, trend_minutes=240)
        self.assertEqual(signal.bar_time, trade["signal_time"])
        self.assertAlmostEqual(trade["entry_price"] - trade["sl"], 2 * signal.atr)
        self.assertAlmostEqual(trade["tp"] - trade["entry_price"], 6 * signal.atr)
        self.assertLessEqual(trade["volume"] * 2 * signal.atr * 100 * 1.15, 1500)

    def test_cli_config_fixed_risk_and_costs_without_default_daily_mismatch(self):
        args = parser().parse_args(["backtest", "offline.csv", "--strategy-mode", "short_term",
                                   "--spread", "0", "--value-per-price-unit", "1"])
        self.assertEqual(args.strategy_mode, "short_term")
        settings = dict(strategy_mode="short_term", risk_fraction=.15, daily_loss_fraction=.2,
                        max_open_risk_fraction=.2, stop_atr=2, reward_ratio=3,
                        max_spread_atr=.2, cooldown_seconds=900, risk_buffer=1.15,
                        commission_per_lot=0, broker_utc_offset_hours=2)
        with csv_file(json.dumps(settings)) as config_path:
            load_config(config_path).validate()
            with patch("bijisatu.backtest.run_backtest", return_value={}) as simulator, patch("bijisatu.cli.MT5Broker") as broker:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["backtest", "offline.csv", "--config", str(config_path),
                                           "--spread", "0", "--value-per-price-unit", "1"]), 0)
                for key, value in settings.items():
                    self.assertEqual(simulator.call_args.kwargs[key], value)
                broker.assert_not_called()


if __name__ == "__main__":
    unittest.main()

