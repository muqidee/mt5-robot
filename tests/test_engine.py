import shutil
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bijisatu.broker import BrokerError, MT5Broker, OrderUncertain
from bijisatu.config import Config
from bijisatu.engine import Engine, trading_day
from bijisatu.models import Account, Bar, Position, Signal, SymbolSpec, Tick
from bijisatu.state import StateError, StateStore
from bijisatu.ollama import OllamaObserver


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / (".engine-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "account.json"
        self.store = StateStore(self.path)
        self.config = Config(account_login=123, broker_utc_offset_hours=0, commission_per_lot=0)
        self.now = int(datetime(2026, 10, 6, 12, tzinfo=timezone.utc).timestamp())
        self.account = Account(123, "test", "USD", 10000, 10000, True, True)
        self.spec = SymbolSpec("EURUSD", 0.00001, 5, 0.00001, 0.01, 100, 0.01, 0)
        self.broker = Mock(spec=MT5Broker)
        self.broker.symbols.return_value = ["EURUSD"]
        self.broker.account.return_value = self.account
        self.broker.positions.return_value = []
        self.broker.activity.return_value = (False, False)
        self.broker.has_orders.return_value = False
        self.broker.spec.side_effect = lambda symbol: replace(self.spec, name=symbol)
        self.broker.tick.side_effect = lambda symbol: Tick(1.1000, 1.1001, self.now)
        self.broker.bars.side_effect = lambda symbol, minutes: [
            Bar(self.now - minutes * 60, 1.1, 1.102, 1.098, 1.1)
        ]
        self.broker.profit.side_effect = lambda side, symbol, volume, entry, exit_price: (
            (exit_price - entry) * volume * 100000 * (1 if side == "buy" else -1)
        )
        self.broker.send.return_value = "accepted"
        evaluator = patch("bijisatu.engine.evaluate", return_value=Signal("buy", self.now - 60, 0.002, "controlled"))
        self.evaluate = evaluator.start()
        self.addCleanup(evaluator.stop)

    def engine(self, execute=False, store=None, config=None):
        return Engine(self.broker, config or self.config, store or self.store, execute=execute)

    def baseline(self, equity=10000):
        self.store.start_day(trading_day(self.now, 0)[0], equity, self.now - 600)

    def advance(self, seconds, new_signal=True):
        self.now += seconds
        if new_signal:
            self.evaluate.return_value = Signal("buy", self.now - 60, 0.002, "controlled")

    def position(self, **changes):
        return replace(Position(1, "GBPUSD", "buy", 1, 1.1, 1.099, self.config.magic), **changes)

    def test_default_mode_is_dry_run_even_on_trade_enabled_account(self):
        engine = Engine(self.broker, self.config, self.store)
        result = engine.step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertGreater(result["plan"]["risk_amount"], 0)
        self.assertLessEqual(result["plan"]["risk_amount"], 300)
        self.broker.send.assert_not_called()
        self.broker.connect.assert_not_called()
        self.evaluate.assert_called_once()

    def test_observer_results_never_change_plans_sends_or_claims(self):
        for execute in (False, True):
            for judgement in ({"decision": "allow"}, {"decision": "skip"}, TimeoutError("offline")):
                with self.subTest(execute=execute, judgement=judgement):
                    store = StateStore(self.directory / (uuid.uuid4().hex + ".json"))
                    observer = Mock()
                    if isinstance(judgement, Exception):
                        observer.submit.side_effect = judgement
                    else:
                        observer.submit.return_value = judgement
                    self.broker.send.reset_mock()
                    engine = Engine(self.broker, self.config, store, execute, observer=observer)
                    result = engine.step(self.now)
                    observer.submit.assert_called_once()
                    self.assertEqual(result["status"], "sent" if execute else "dry_run")
                    if execute:
                        self.broker.send.assert_called_once()
                        plan = self.broker.send.call_args.args[0]
                        self.assertEqual(plan.side, "buy")
                        self.assertEqual(plan.symbol, "EURUSD")
                        self.assertEqual(len(store.data["attempts"]), 1)
                    else:
                        self.broker.send.assert_not_called()
                        self.assertEqual(len(store.data["attempts"]), 1)
                        self.assertEqual(result["plan"]["side"], "buy")

    def test_async_judgement_does_not_change_order_or_duplicate_send(self):
        for execute in (False, True):
            baseline_store = StateStore(self.directory / (uuid.uuid4().hex + ".json"))
            self.broker.send.reset_mock()
            baseline = Engine(self.broker, self.config, baseline_store, execute).step(self.now)
            expected_plan = self.broker.send.call_args.args[0] if execute else baseline["plan"]
            for judgement in ({"decision": "allow", "reason": "ok"}, {"decision": "skip", "reason": "no"},
                              TimeoutError(), ValueError("malformed")):
                with self.subTest(execute=execute, judgement=judgement):
                    store = StateStore(self.directory / (uuid.uuid4().hex + ".json"))
                    config = replace(self.config, ollama_observation_enabled=True,
                                     ollama_observation_timeout_seconds=0.1)
                    options = {"side_effect": judgement} if isinstance(judgement, Exception) else {"return_value": judgement}
                    self.broker.send.reset_mock()
                    with patch("bijisatu.ollama.OllamaClient.judge", **options) as judge:
                        observer = OllamaObserver(config)
                        try:
                            engine = Engine(self.broker, config, store, execute, observer)
                            result = engine.step(self.now)
                            observer.queue.join()
                            judge.assert_called_once()
                            self.assertEqual(result["status"], baseline["status"])
                            actual_plan = self.broker.send.call_args.args[0] if execute else result["plan"]
                            self.assertEqual(actual_plan, expected_plan)
                            self.assertEqual(engine.step(self.now)["status"], "waiting")
                            observer.queue.join()
                            judge.assert_called_once()
                            self.assertEqual(self.broker.send.call_count, int(execute))
                        finally:
                            observer.close()

    def test_pending_judgement_never_delays_real_order_path(self):
        started, release = threading.Event(), threading.Event()
        def judge(snapshot):
            started.set()
            release.wait(1)
            raise TimeoutError("mocked CPU inference stalled")
        config = replace(self.config, ollama_observation_enabled=True,
                         ollama_observation_timeout_seconds=0.1)
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=judge):
            observer = OllamaObserver(config)
            try:
                before = time.monotonic()
                engine = Engine(self.broker, config, self.store, True, observer)
                self.assertEqual(engine.step(self.now)["status"], "sent")
                self.assertLess(time.monotonic() - before, 0.25)
                self.assertTrue(started.wait(1))
                self.assertFalse(release.is_set())
                self.broker.send.assert_called_once()
                self.assertEqual(engine.step(self.now)["status"], "waiting")
                self.broker.send.assert_called_once()
            finally:
                release.set()
                observer.close()

    def test_default_engine_never_creates_or_calls_ollama(self):
        with patch("bijisatu.ollama.OllamaClient.judge") as judge:
            engine = self.engine()
            self.assertIsNone(engine.observer)
            self.assertEqual(engine.step(self.now)["status"], "dry_run")
            judge.assert_not_called()

    def test_dry_run_needs_no_execution_configuration(self):
        self.assertEqual(self.engine(config=Config()).step(self.now)["status"], "dry_run")
        self.broker.send.assert_not_called()

    def test_execute_requires_configuration_triple_before_broker_use(self):
        for field in ("account_login", "broker_utc_offset_hours", "commission_per_lot"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.engine(execute=True, config=replace(self.config, **{field: None}))
        self.broker.symbols.assert_not_called()
        self.broker.send.assert_not_called()

    def test_send_has_durable_claim_before_external_call(self):
        def send(plan):
            durable = StateStore(self.path).data
            self.assertEqual(durable["attempts"][plan.symbol]["bar_time"], plan.bar_time)
            return "accepted"
        self.broker.send.side_effect = send
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "sent")
        self.broker.send.assert_called_once()

    def test_failed_claim_prevents_order(self):
        self.baseline()
        with patch.object(self.store, "claim", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self.engine(execute=True).step(self.now)
        self.broker.send.assert_not_called()

    def test_same_signal_is_not_retried_after_cooldown_or_restart(self):
        config = replace(self.config, cooldown_seconds=1)
        self.assertEqual(self.engine(execute=True, config=config).step(self.now)["status"], "sent")
        # Keep the original bar fresh so deduplication, not staleness, blocks it.
        self.advance(2, new_signal=False)
        self.assertEqual(self.engine(execute=True, config=config, store=StateStore(self.path)).step(self.now)["status"], "waiting")
        self.broker.send.assert_called_once()

    def test_new_signal_waits_for_cooldown_then_can_enter(self):
        engine = self.engine(execute=True)
        self.assertEqual(engine.step(self.now)["status"], "sent")
        self.advance(self.config.cooldown_seconds - 1)
        self.assertEqual(engine.step(self.now)["status"], "waiting")
        self.broker.send.assert_called_once()
        self.advance(2)
        self.assertEqual(engine.step(self.now)["status"], "sent")
        self.assertEqual(self.broker.send.call_count, 2)

    def test_uncertain_send_halts_day_and_is_not_retried_after_restart(self):
        self.broker.send.side_effect = OrderUncertain("No acknowledgement")
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "halted")
        self.advance(600)
        self.broker.account.return_value = replace(self.account, equity=11000)
        self.assertEqual(self.engine(execute=True, store=StateStore(self.path)).step(self.now)["status"], "halted")
        self.broker.send.assert_called_once()
        self.assertTrue(StateStore(self.path).data["halted"])

    def test_equity_limit_includes_floating_loss_and_latches_after_recovery(self):
        self.baseline()
        positions = [self.position()]
        self.broker.positions.return_value = positions
        self.broker.account.return_value = replace(self.account, equity=9500, balance=10000)
        result = self.engine(execute=True).step(self.now)
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["loss"], 500)
        self.advance(600)
        self.broker.account.return_value = replace(self.account, equity=10100)
        self.assertEqual(self.engine(execute=True, store=StateStore(self.path)).step(self.now)["status"], "halted")
        self.assertEqual(self.broker.positions.return_value, positions)
        self.broker.send.assert_not_called()
        self.broker.close.assert_not_called()

    def test_equity_limit_is_rechecked_after_scanning(self):
        self.baseline()
        self.broker.account.side_effect = [self.account, replace(self.account, equity=9499)]
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "halted")
        self.assertTrue(StateStore(self.path).data["halted"])
        self.broker.send.assert_not_called()

    def test_cashflow_halts_existing_day_without_rebasing(self):
        self.baseline()
        self.broker.activity.return_value = (False, True)
        self.broker.account.return_value = replace(self.account, equity=12000, balance=12000)
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "halted")
        self.assertEqual(StateStore(self.path).data["baseline"], 10000)
        self.broker.send.assert_not_called()

    def test_clean_start_refuses_positions_prior_trades_or_cashflow(self):
        for positions, activity in (([self.position()], (False, False)),
                                    ([], (True, False)), ([], (False, True))):
            with self.subTest(positions=positions, activity=activity):
                self.broker.positions.return_value = positions
                self.broker.activity.return_value = activity
                with self.assertRaises(StateError):
                    self.engine(execute=True).step(self.now)
                self.assertIsNone(self.store.data)
                self.broker.send.assert_not_called()

    def test_clean_next_day_resets_latch(self):
        self.baseline()
        self.store.halt("Yesterday's loss cap")
        self.advance(86400)
        self.broker.account.return_value = replace(self.account, equity=9500)
        self.assertEqual(self.engine().step(self.now)["status"], "dry_run")
        self.assertEqual(self.store.data["baseline"], 9500)
        self.assertFalse(self.store.data["halted"])

    def test_continuous_midnight_rollover_accepts_existing_positions(self):
        _, midnight = trading_day(self.now, 0)
        self.now = int(midnight + 86400 + 10)
        self.store.start_day("2026-10-06", 10000, self.now - 20)
        self.store.halt("Prior day cap")
        self.broker.positions.return_value = [self.position()]
        self.broker.account.return_value = replace(self.account, equity=9900)
        self.evaluate.return_value = None
        self.assertEqual(self.engine().step(self.now)["status"], "waiting")
        self.assertEqual(self.store.data["day"], "2026-10-07")
        self.assertEqual(self.store.data["baseline"], 9900)
        self.assertFalse(self.store.data["halted"])

    def test_missed_midnight_with_exposure_cannot_reset_baseline(self):
        self.baseline()
        self.advance(86400)
        self.broker.positions.return_value = [self.position()]
        with self.assertRaises(StateError):
            self.engine(execute=True).step(self.now)
        self.assertEqual(StateStore(self.path).data["day"], "2026-10-06")
        self.broker.send.assert_not_called()

    def test_cashflow_even_at_continuous_rollover_requires_clean_baseline(self):
        _, midnight = trading_day(self.now, 0)
        self.now = int(midnight + 86400 + 10)
        self.store.start_day("2026-10-06", 10000, self.now - 20)
        self.broker.activity.return_value = (False, True)
        with self.assertRaises(StateError):
            self.engine(execute=True).step(self.now)
        self.broker.send.assert_not_called()

    def test_clock_backwards_fails_closed(self):
        self.baseline()
        self.store.data["last_seen"] = self.now + 60
        self.store.save()
        with self.assertRaises(StateError):
            self.engine(execute=True).step(self.now)
        self.broker.send.assert_not_called()

    def test_open_risk_and_current_equity_loss_reduce_remaining_budget(self):
        self.baseline()
        self.broker.account.return_value = replace(self.account, equity=9700)
        self.broker.positions.return_value = [self.position()]
        result = self.engine().step(self.now)
        self.assertEqual(result["status"], "dry_run")
        # 500 daily limit - 300 equity loss - 115 buffered open-stop risk.
        self.assertGreater(result["plan"]["risk_amount"], 80)
        self.assertLessEqual(result["plan"]["risk_amount"], 85 + 1e-8)
        self.broker.send.assert_not_called()

    def test_open_risk_uses_current_mark_not_entry_and_includes_commission(self):
        self.baseline()
        self.broker.account.return_value = replace(self.account, equity=9700)
        self.broker.positions.return_value = [self.position(price_current=1.09)]
        config = replace(self.config, commission_per_lot=10)
        result = self.engine(config=config).step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertGreater(result["plan"]["risk_amount"], 65)
        self.assertLessEqual(result["plan"]["risk_amount"], 73.5 + 1e-8)

    def test_exhausted_open_risk_budget_blocks_without_closing_positions(self):
        self.baseline()
        self.broker.positions.return_value = [self.position(sl=1.09)]
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "blocked")
        self.broker.send.assert_not_called()
        self.broker.close.assert_not_called()
        self.assertFalse(self.store.data["halted"])

    def test_foreign_and_missing_stop_positions_block_entries(self):
        self.baseline()
        for changes in ({"magic": 999}, {"sl": 0}, {"sl": float("nan")}):
            with self.subTest(changes=changes):
                self.broker.positions.return_value = [self.position(**changes)]
                with self.assertRaises(BrokerError):
                    self.engine(execute=True).step(self.now)
                self.broker.send.assert_not_called()

    def test_below_minimum_lot_is_skipped_not_rounded_up(self):
        self.spec = replace(self.spec, volume_min=1, volume_step=1)
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "waiting")
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_wide_stale_or_future_candidate_quote_is_skipped(self):
        for quote in (Tick(1.1, 1.101, self.now), Tick(1.1, 1.1001, self.now - 16),
                      Tick(1.1, 1.1001, self.now + 2)):
            with self.subTest(quote=quote):
                self.broker.tick.side_effect = None
                self.broker.tick.return_value = quote
                self.assertEqual(self.engine(execute=True).step(self.now)["status"], "waiting")
                self.broker.send.assert_not_called()

    def test_quote_is_rechecked_before_entry(self):
        for refreshed in (Tick(1.1, 1.101, self.now), Tick(1.1, 1.1001, self.now - 16)):
            with self.subTest(refreshed=refreshed):
                self.broker.tick.side_effect = [Tick(1.1, 1.1001, self.now), refreshed]
                result = self.engine(execute=True).step(self.now)
                self.assertIn(result["status"], ("waiting", "blocked"))
                self.broker.send.assert_not_called()
                self.assertEqual(self.store.data["attempts"], {})

    def test_quote_that_ages_during_scan_is_not_sent(self):
        elapsed = [0.0]
        signal = self.evaluate.return_value
        def evaluate(m1, m5):
            elapsed[0] = self.config.max_tick_age_seconds + 1
            return signal
        self.evaluate.side_effect = evaluate
        with patch("bijisatu.engine.time.monotonic", side_effect=lambda: elapsed[0]):
            result = self.engine(execute=True).step(self.now)
        self.assertIn(result["status"], ("waiting", "blocked"))
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_midnight_crossed_during_scan_requires_new_baseline(self):
        _, midnight = trading_day(self.now, 0)
        self.now = int(midnight + 86400 - 1)
        signal = Signal("buy", self.now - 60, 0.002, "controlled")
        elapsed = [0.0]
        def evaluate(m1, m5):
            elapsed[0] = 2.0
            return signal
        self.evaluate.side_effect = evaluate
        with patch("bijisatu.engine.time.monotonic", side_effect=lambda: elapsed[0]):
            result = self.engine(execute=True).step(self.now)
        self.assertEqual(result["status"], "waiting")
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["day"], "2026-10-06")
        self.assertEqual(self.store.data["attempts"], {})

    def test_lower_spread_candidate_is_selected(self):
        self.broker.symbols.return_value = ["WIDE", "TIGHT"]
        self.broker.tick.side_effect = lambda symbol: Tick(1.1, 1.1002 if symbol == "WIDE" else 1.1001, self.now)
        result = self.engine().step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["plan"]["symbol"], "TIGHT")

    def test_stale_closed_bars_are_not_evaluated(self):
        self.broker.bars.side_effect = lambda symbol, minutes: [Bar(self.now - 10000, 1.1, 1.2, 1.0, 1.1)]
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "waiting")
        self.evaluate.assert_not_called()
        self.broker.send.assert_not_called()

    def test_pending_orders_and_max_positions_block(self):
        self.baseline()
        self.broker.has_orders.return_value = True
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "blocked")
        self.broker.has_orders.return_value = False
        self.broker.positions.return_value = [self.position(), self.position(ticket=2, symbol="USDJPY")]
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "blocked")
        self.broker.send.assert_not_called()

    def test_no_signal_never_claims_or_sends(self):
        self.evaluate.return_value = None
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "waiting")
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.send.assert_not_called()

    def test_exposure_risk_is_refreshed_after_candidate_scan(self):
        self.baseline()
        self.broker.account.return_value = replace(self.account, equity=9700)
        self.broker.positions.side_effect = [[], [self.position()]]
        result = self.engine().step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertGreater(result["plan"]["risk_amount"], 80)
        self.assertLessEqual(result["plan"]["risk_amount"], 85 + 1e-8)

    def test_symbol_opened_during_scan_prevents_duplicate_entry(self):
        self.baseline()
        self.broker.positions.side_effect = [[], [self.position(symbol="EURUSD")]]
        self.assertEqual(self.engine(execute=True).step(self.now)["status"], "blocked")
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_execution_permission_is_required_but_not_for_observation(self):
        self.broker.account.return_value = replace(self.account, trade_allowed=False)
        with self.assertRaises(BrokerError):
            self.engine(execute=True).step(self.now)
        self.assertEqual(self.engine().step(self.now)["status"], "dry_run")
        self.broker.send.assert_not_called()

    def test_account_change_error_propagates_without_send(self):
        self.broker.account.side_effect = BrokerError("MT5 account changed")
        with self.assertRaises(BrokerError):
            self.engine(execute=True).step(self.now)
        self.broker.send.assert_not_called()

    def test_trading_day_respects_fractional_broker_offset(self):
        now = datetime(2026, 10, 6, 22, tzinfo=timezone.utc).timestamp()
        day, start = trading_day(now, 3.5)
        self.assertEqual(day, "2026-10-07")
        self.assertEqual(start, datetime(2026, 10, 6, 20, 30, tzinfo=timezone.utc).timestamp())


    def test_stale_quote_logs_age_and_threshold(self):
        self.broker.tick.side_effect = lambda symbol: Tick(1.1, 1.1001, self.now - 21)
        with self.assertLogs("bijisatu", level="DEBUG") as captured:
            result = self.engine().step(self.now)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["skipped"], {"stale_tick": 1})
        record = next(record for record in captured.records if getattr(record, "event", "") == "symbol_skip")
        self.assertEqual(record.details["reason"], "stale_tick")
        self.assertGreaterEqual(record.details["tick_age_seconds"], 21)
        self.assertEqual(record.details["max_tick_age_seconds"], 15)
        self.assertIn("Stale tick", record.details["error"])
        self.assertIn("spread_points", record.details)
        self.broker.send.assert_not_called()

    def test_future_quote_tolerance_accepts_small_leads_and_exact_boundary(self):
        engine = self.engine()
        quote = Tick(1.1, 1.1001, self.now)
        for lead in (0, 0.312, 1):
            with self.subTest(lead=lead):
                engine._fresh_tick(quote, self.now - lead)
        with self.assertRaisesRegex(BrokerError, "Future tick.*tolerance=1.0s"):
            engine._fresh_tick(quote, self.now - 1.001)
        self.broker.send.assert_not_called()

    def test_zero_tolerance_still_rejects_small_future_quotes(self):
        engine = self.engine(config=replace(self.config, max_tick_future_seconds=0))
        with self.assertRaisesRegex(BrokerError, "Future tick"):
            engine._fresh_tick(Tick(1.1, 1.1001, self.now), self.now - 0.312)
        self.broker.send.assert_not_called()

    def test_stale_quote_boundary_is_unchanged_by_future_tolerance(self):
        engine = self.engine()
        quote = Tick(1.1, 1.1001, self.now)
        engine._fresh_tick(quote, self.now + 15)
        with self.assertRaisesRegex(BrokerError, "Stale tick"):
            engine._fresh_tick(quote, self.now + 15.001)
        self.broker.send.assert_not_called()

    def test_open_risk_accepts_reported_subsecond_future_tick(self):
        engine = self.engine()
        with patch("bijisatu.engine.time.monotonic", return_value=0):
            reserved = engine._open_risk([self.position()], self.now - 0.312)
        self.assertGreater(reserved, 0)
        self.broker.send.assert_not_called()

    def test_open_risk_rejects_future_ticks_beyond_tolerance(self):
        self.baseline()
        self.broker.positions.return_value = [self.position()]
        self.broker.tick.side_effect = lambda symbol: Tick(1.1, 1.1001, self.now + 2)
        with patch("bijisatu.engine.time.monotonic", return_value=0):
            result = self.engine(execute=True).step(self.now)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Future tick", result["reason"])
        self.assertEqual(result["retry_seconds"], self.config.poll_seconds)
        self.assertFalse(self.store.data["halted"])
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_open_risk_time_block_recovers_without_reset_or_order_retry(self):
        self.baseline()
        baseline = self.store.data["baseline"]
        self.broker.positions.return_value = [self.position()]
        engine = self.engine()
        for lead in (1.199, -16):
            with self.subTest(lead=lead):
                self.broker.tick.side_effect = None
                self.broker.tick.return_value = Tick(1.1, 1.1001, self.now + lead)
                with patch("bijisatu.engine.time.monotonic", return_value=0):
                    with self.assertLogs("bijisatu", level="WARNING") as captured:
                        result = engine.step(self.now)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(captured.records[0].event, "quote_time_blocked")
                self.assertNotIn("remaining_risk", result["account"])
                self.assertEqual(self.store.data["baseline"], baseline)
                self.assertFalse(self.store.data["halted"])
                self.assertEqual(self.store.data["attempts"], {})
                self.broker.profit.assert_not_called()
        self.broker.tick.return_value = Tick(1.1, 1.1001, self.now)
        with patch("bijisatu.engine.time.monotonic", return_value=0):
            recovered = engine.step(self.now)
        self.assertEqual(recovered["status"], "dry_run")
        self.assertGreater(recovered["account"]["reserved_open_risk"], 0)
        self.assertEqual(self.store.data["baseline"], baseline)
        self.broker.send.assert_not_called()

    def test_entry_scan_and_refresh_accept_one_second_clock_lead(self):
        self.broker.tick.side_effect = lambda symbol: Tick(1.1, 1.1001, self.now + 1)
        with patch("bijisatu.engine.time.monotonic", return_value=0):
            result = self.engine().step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertNotIn("future_tick", result["skipped"])
        self.assertEqual(self.broker.tick.call_count, 2)
        self.broker.send.assert_not_called()

    def test_entry_refresh_rejects_clock_lead_beyond_tolerance(self):
        self.broker.tick.side_effect = [
            Tick(1.1, 1.1001, self.now), Tick(1.1, 1.1001, self.now + 2),
        ]
        with patch("bijisatu.engine.time.monotonic", return_value=0):
            result = self.engine(execute=True).step(self.now)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Future tick", result["reason"])
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_future_quote_has_distinct_diagnostic(self):
        self.broker.tick.side_effect = lambda symbol: Tick(1.1, 1.1001, self.now + 60)
        with self.assertLogs("bijisatu", level="DEBUG") as captured:
            result = self.engine().step(self.now)
        self.assertEqual(result["skipped"], {"future_tick": 1})
        record = next(record for record in captured.records if getattr(record, "event", "") == "symbol_skip")
        self.assertIn("Future tick", record.details["error"])
        self.assertLess(record.details["tick_age_seconds"], 0)
        self.broker.send.assert_not_called()

    def test_waiting_explains_missing_history_and_account_risk(self):
        self.evaluate.return_value = None
        with self.assertLogs("bijisatu", level="DEBUG") as captured:
            result = self.engine().step(self.now)
        self.assertEqual(result["skipped"], {"insufficient_history": 1})
        self.assertEqual(result["account"]["currency"], "USD")
        self.assertEqual(result["account"]["daily_loss_limit"], 500)
        self.assertEqual(result["account"]["remaining_risk"], 500)
        self.assertEqual(captured.records[-1].event, "scan_completed")

    def test_quote_time_is_sampled_after_slow_fetch(self):
        elapsed = [0.0]

        def tick(symbol):
            elapsed[0] += 2
            return Tick(1.1, 1.1001, self.now + int(elapsed[0]))

        self.broker.tick.side_effect = tick
        with patch("bijisatu.engine.time.monotonic", side_effect=lambda: elapsed[0]):
            result = self.engine().step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertNotIn("future_tick", result["skipped"])
        self.broker.send.assert_not_called()


    def test_open_risk_capacity_tracks_current_equity_not_saved_baseline(self):
        config = replace(self.config, risk_fraction=.01, daily_loss_fraction=.5,
                         max_positions=10, max_open_risk_fraction=.05)
        for equity in (5000, 10000, 20000):
            with self.subTest(equity=equity):
                self.broker.account.return_value = replace(self.account, equity=equity)
                store = StateStore(self.directory / f"equity-{equity}.json")
                store.start_day(trading_day(self.now, 0)[0], 4000, self.now - 600)
                engine = self.engine(config=config, store=store)
                reserved = equity * .049
                with patch.object(engine, "_open_risk", return_value=reserved):
                    result = engine.step(self.now)
                self.assertEqual(result["status"], "dry_run")
                self.assertEqual(result["account"]["open_risk_limit"], equity * .05)
                self.assertAlmostEqual(result["account"]["remaining_open_risk"], equity * .001)
                self.assertLessEqual(result["plan"]["risk_amount"], equity * .001 + 1e-8)
                self.assertGreater(result["plan"]["risk_amount"], 0)
                self.assertEqual(store.data["baseline"], 4000)
        self.broker.send.assert_not_called()

    def test_combined_open_risk_includes_costs_and_buffer(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, daily_loss_fraction=.2,
                         max_positions=10, commission_per_lot=5)
        self.broker.positions.return_value = [
            self.position(ticket=index, symbol=f"HELD{index}") for index in range(4)
        ]
        result = self.engine(config=config).step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertAlmostEqual(result["account"]["reserved_open_risk"], 483)
        self.assertAlmostEqual(result["account"]["remaining_open_risk"], 17)
        self.assertLessEqual(result["plan"]["risk_amount"] + 483, 500 + 1e-8)
        self.assertLessEqual(result["plan"]["risk_amount"], 100)

    def test_open_risk_ceiling_blocks_without_halt_or_position_changes(self):
        self.baseline()
        config = replace(self.config, max_positions=10, daily_loss_fraction=.2)
        positions = [self.position(sl=1.09)]
        self.broker.positions.return_value = positions
        result = self.engine(config=config).step(self.now)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["account"]["remaining_open_risk"], 0)
        self.assertGreater(result["account"]["remaining_daily_risk"], 0)
        self.assertFalse(self.store.data["halted"])
        self.assertEqual(self.broker.positions.return_value, positions)
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.bars.assert_not_called()
        self.broker.send.assert_not_called()
        self.broker.close.assert_not_called()

    def test_positive_open_capacity_below_minimum_lot_never_forces_entry(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10,
                         daily_loss_fraction=.2)
        engine = self.engine(config=config)
        with patch.object(engine, "_open_risk", return_value=498):
            result = engine.step(self.now)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["account"]["remaining_risk"], 2)
        self.assertEqual(result["skipped"], {"insufficient_risk_for_minimum_lot": 1})
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.send.assert_not_called()

    def test_equity_drop_revalidates_open_risk_before_entry(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10,
                         daily_loss_fraction=.2)
        self.broker.account.side_effect = [self.account, replace(self.account, equity=9000)]
        engine = self.engine(config=config)
        with patch.object(engine, "_open_risk", return_value=460):
            result = engine.step(self.now)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["account"]["open_risk_limit"], 450)
        self.assertEqual(result["account"]["remaining_risk"], 0)
        self.assertGreater(result["account"]["remaining_daily_risk"], 0)
        self.assertEqual(self.store.data["attempts"], {})
        self.assertFalse(self.store.data["halted"])
        self.broker.send.assert_not_called()

    def test_exposure_and_equity_refresh_resize_to_new_open_capacity(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10,
                         daily_loss_fraction=.2)
        self.broker.account.side_effect = [self.account, replace(self.account, equity=10500)]
        before = [self.position(ticket=index, symbol=f"HELD{index}") for index in range(3)]
        after = before + [self.position(ticket=4, symbol="NEWHELD")]
        self.broker.positions.side_effect = [before, after]
        result = self.engine(config=config).step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["account"]["open_risk_limit"], 525)
        self.assertAlmostEqual(result["account"]["reserved_open_risk"], 460)
        self.assertAlmostEqual(result["account"]["remaining_risk"], 65)
        self.assertLessEqual(result["plan"]["risk_amount"], 65 + 1e-8)
        self.assertGreater(result["plan"]["risk_amount"], 60)
        self.assertEqual(self.store.data["baseline"], 10000)

    def test_daily_budget_is_independently_tighter_than_open_risk_capacity(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10,
                         daily_loss_fraction=.02, max_open_risk_fraction=.05)
        self.broker.account.return_value = replace(self.account, equity=9820)
        result = self.engine(config=config).step(self.now)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["account"]["remaining_daily_risk"], 20)
        self.assertEqual(result["account"]["remaining_open_risk"], 491)
        self.assertLessEqual(result["plan"]["risk_amount"], 20)

    def test_ten_position_hard_cap_blocks_and_nine_can_enter(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10)
        held = [self.position(ticket=index, symbol=f"HELD{index}", volume=.01)
                for index in range(10)]
        self.broker.positions.return_value = held
        self.assertEqual(self.engine(config=config).step(self.now)["status"], "blocked")
        self.broker.positions.return_value = held[:-1]
        self.assertEqual(self.engine(config=config).step(self.now)["status"], "dry_run")
        self.broker.send.assert_not_called()

    def test_tenth_position_opened_during_scan_blocks_another_entry(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10)
        held = [self.position(ticket=index, symbol=f"HELD{index}", volume=.01)
                for index in range(10)]
        self.broker.positions.side_effect = [held[:-1], held]
        self.assertEqual(self.engine(config=config).step(self.now)["status"], "blocked")
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.send.assert_not_called()

    def test_other_qualifying_symbols_can_enter_on_subsequent_scans(self):
        self.baseline()
        config = replace(self.config, risk_fraction=.01, max_positions=10)
        self.broker.symbols.return_value = ["EURUSD", "GBPUSD"]
        first = self.engine(execute=True, config=config).step(self.now)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(self.broker.send.call_count, 1)
        self.broker.positions.return_value = [self.position(symbol=first["plan"]["symbol"], volume=.1)]
        second = self.engine(execute=True, config=config).step(self.now)
        self.assertEqual(second["status"], "sent")
        self.assertNotEqual(second["plan"]["symbol"], first["plan"]["symbol"])
        self.assertEqual(self.broker.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()

