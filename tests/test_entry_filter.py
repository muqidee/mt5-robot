import json
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from bijisatu.broker import BrokerError, OrderUncertain
from bijisatu.config import Config
from bijisatu.engine import Engine
from bijisatu.models import Bar, Signal, Tick
from bijisatu.ollama import OllamaEntryFilter
from tests import test_engine as engine_tests


class FilterWorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Config(), ollama_filter_enabled=True, max_symbols=2,
                              ollama_observation_queue_capacity=1,
                              ollama_observation_timeout_seconds=0.1)
        self.signal = Signal("buy", 600, 0.002, "not transmitted")
        self.args = ("EURUSD", self.signal, 1, 5,
                     [Bar(600, 1.1, 1.2, 1.0, 1.15)],
                     [Bar(300, 1.1, 1.2, 1.0, 1.15)], 0.0001)

    def worker(self, result):
        options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
        mocked = patch("bijisatu.ollama.OllamaClient.judge", **options)
        judge = mocked.start()
        self.addCleanup(mocked.stop)
        worker = OllamaEntryFilter(self.config)
        self.addCleanup(worker.close)
        return worker, judge

    def test_pending_then_exact_approval_and_dedup(self):
        worker, judge = self.worker({"decision": "allow", "reason": "Aligned"})
        status, binding = worker.check(*self.args)
        self.assertEqual(status, "pending")
        worker.queue.join()
        self.assertEqual(worker.check(*self.args), ("allowed", binding))
        judge.assert_called_once()
        snapshot = judge.call_args.args[0]
        self.assertEqual(snapshot["strategy_mode"], "scalping")
        for field in ("login", "equity", "volume", "account", "secret"):
            self.assertNotIn(field, snapshot)

    def test_errors_malformed_and_deny_fail_closed(self):
        for verdict, status in (({"decision": "skip", "reason": "No"}, "rejected"),
                                (TimeoutError(), "unavailable"), (ConnectionRefusedError(), "unavailable"),
                                (ValueError(), "unavailable"), ({"decision": "allow"}, "unavailable"),
                                ({"decision": "allow", "reason": "\x1b[x"}, "unavailable"),
                                ({"decision": "allow", "reason": "Ok", "volume": 1}, "unavailable")):
            with self.subTest(verdict=verdict):
                worker, judge = self.worker(verdict)
                _, binding = worker.check(*self.args)
                worker.queue.join()
                self.assertEqual(worker.lookup(binding), status)
                worker.check(*self.args)
                judge.assert_called_once()
                worker.close()

    def test_identity_expiration_and_model_changes_reject(self):
        worker, judge = self.worker({"decision": "allow", "reason": "Ok"})
        _, binding = worker.check(*self.args)
        worker.queue.join()
        for changed in (replace(binding, symbol="GBPUSD"), replace(binding, side="sell"),
                        replace(binding, bar_time=601), replace(binding, strategy_mode="intraday"),
                        replace(binding, digest="different"), replace(binding, model="other"),
                        replace(binding, endpoint="http://127.0.0.1:11434")):
            with self.subTest(changed=changed):
                self.assertEqual(worker.lookup(changed), "unavailable")
        changed_args = (*self.args[:-1], 0.0002)
        self.assertEqual(worker.check(*changed_args)[0], "unavailable")
        judge.assert_called_once()
        worker.client.model = "other"
        self.assertEqual(worker.lookup(binding), "unavailable")
        worker.client.model = binding.model
        with patch("bijisatu.ollama.time.monotonic", return_value=worker._records["EURUSD"].requested_at + 31):
            self.assertEqual(worker.lookup(binding), "unavailable")
        self.assertEqual(worker.lookup(binding), "unavailable")

    def test_new_bar_needs_own_result_and_old_backlog_cannot_authorize(self):
        worker, judge = self.worker({"decision": "allow", "reason": "Ok"})
        _, old = worker.check(*self.args)
        worker.queue.join()
        args = ("EURUSD", replace(self.signal, bar_time=660), 1, 5,
                [Bar(660, 1.1, 1.2, 1.0, 1.15)], self.args[5], 0.0001)
        self.assertEqual(worker.check(*args)[0], "pending")
        self.assertEqual(worker.lookup(old), "unavailable")
        worker.queue.join()
        self.assertEqual(judge.call_count, 2)

    def test_old_inflight_response_cannot_authorize_new_bar(self):
        entered, release = threading.Event(), threading.Event()
        def judge(snapshot):
            if snapshot["bar_time"] == 600:
                entered.set()
                release.wait(1)
                return {"decision": "allow", "reason": "Old setup"}
            return {"decision": "skip", "reason": "New setup rejected"}
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=judge):
            worker = OllamaEntryFilter(self.config)
            try:
                _, old = worker.check(*self.args)
                self.assertTrue(entered.wait(1))
                args = ("EURUSD", replace(self.signal, bar_time=660), 1, 5,
                        [Bar(660, 1.1, 1.2, 1.0, 1.15)], self.args[5], 0.0001)
                status, new = worker.check(*args)
                self.assertEqual(status, "pending")
                self.assertEqual(worker.lookup(old), "unavailable")
                release.set()
                worker.queue.join()
                self.assertEqual(worker.lookup(new), "rejected")
                self.assertEqual(worker.lookup(old), "unavailable")
            finally:
                release.set()
                worker.close()

    def test_queue_full_nonblocking_and_symbol_memory_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def stalled(snapshot):
            entered.set()
            release.wait(2)
            return {"decision": "allow", "reason": "Ok"}
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=stalled):
            config = replace(self.config, max_symbols=3)
            worker = OllamaEntryFilter(config)
            try:
                worker.check(*self.args)
                self.assertTrue(entered.wait(1))
                self.assertEqual(worker.check("GBPUSD", *self.args[1:])[0], "pending")
                before = time.monotonic()
                self.assertEqual(worker.check("USDJPY", *self.args[1:])[0], "unavailable")
                self.assertLess(time.monotonic() - before, 0.1)
                self.assertEqual(worker.check("AUDUSD", *self.args[1:])[0], "unavailable")
                self.assertEqual(len(worker._records), 3)
                self.assertEqual(worker.check("USDJPY", *self.args[1:])[0], "unavailable")
            finally:
                release.set()
                worker.close()
            self.assertEqual(len(worker._records), 0)

    def test_config_schema_and_example_align_with_defaults(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "config.schema.json").read_text(encoding="utf-8"))
        example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        defaults = json.loads(json.dumps(asdict(Config())))
        self.assertEqual(example, defaults)
        self.assertEqual(set(schema["properties"]), set(defaults))
        for name, value in defaults.items():
            self.assertEqual(schema["properties"][name].get("default"), value, name)
        self.assertIn("allOf", schema)
        self.assertIn("ollama_filter_enabled", (root / "docs" / "robots" / "bijisatu.md").read_text())

    def test_config_exclusivity(self):
        self.assertFalse(Config().ollama_filter_enabled)
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                replace(Config(), ollama_filter_enabled=value).validate()
        with self.assertRaises(ValueError):
            replace(Config(), ollama_filter_enabled=True, ollama_observation_enabled=True).validate()


class FilterEngineTests(unittest.TestCase):
    setUp = engine_tests.EngineTests.setUp
    baseline = engine_tests.EngineTests.baseline
    position = engine_tests.EngineTests.position

    def filtered(self, verdict=None):
        config = replace(self.config, ollama_filter_enabled=True, ollama_observation_timeout_seconds=0.1)
        options = {"side_effect": verdict} if isinstance(verdict, Exception) else {
            "return_value": verdict or {"decision": "allow", "reason": "Aligned"}}
        mocked = patch("bijisatu.ollama.OllamaClient.judge", **options)
        judge = mocked.start()
        self.addCleanup(mocked.stop)
        worker = OllamaEntryFilter(config)
        self.addCleanup(worker.close)
        def send(plan, *, before_send):
            before_send()
            return "accepted"
        self.broker.send.side_effect = send
        return Engine(self.broker, config, self.store, True, entry_filter=worker), worker, judge

    def approved(self):
        engine, worker, judge = self.filtered()
        self.assertEqual(engine.step(self.now)["status"], "waiting")
        self.assertFalse(self.store.data["halted"])
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.send.assert_not_called()
        worker.queue.join()
        return engine, worker, judge

    def test_approved_only_next_scan_and_one_send(self):
        engine, worker, judge = self.approved()
        self.assertEqual(engine.step(self.now)["status"], "sent")
        self.assertEqual(len(self.store.data["attempts"]), 1)
        self.assertEqual(engine.step(self.now)["status"], "waiting")
        self.broker.send.assert_called_once()
        judge.assert_called_once()

    def test_full_queue_scan_never_sends_or_claims(self):
        entered, release = threading.Event(), threading.Event()
        def stalled(snapshot):
            entered.set()
            release.wait(1)
            return {"decision": "allow", "reason": "Ok"}
        self.broker.symbols.return_value = ["EURUSD", "GBPUSD", "USDJPY"]
        config = replace(self.config, ollama_filter_enabled=True, ollama_observation_queue_capacity=1,
                         ollama_observation_timeout_seconds=0.1)
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=stalled):
            worker = OllamaEntryFilter(config)
            try:
                engine = Engine(self.broker, config, self.store, True, entry_filter=worker)
                result = engine.step(self.now)
                self.assertEqual(result["status"], "waiting")
                self.assertGreaterEqual(result["skipped"].get("ollama_filter_unavailable", 0), 1)
                self.broker.send.assert_not_called()
                self.assertEqual(self.store.data["attempts"], {})
            finally:
                release.set()
                worker.close()

    def test_missing_client_fails_closed_direct_engine(self):
        config = replace(self.config, ollama_filter_enabled=True)
        engine = Engine(self.broker, config, self.store, True)
        result = engine.step(self.now)
        self.assertEqual(result["skipped"], {"ollama_filter_unavailable": 1})
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_deny_errors_and_malformed_never_send_or_claim(self):
        for verdict in ({"decision": "skip", "reason": "No"}, TimeoutError(), ConnectionRefusedError(),
                        ValueError(), {"decision": "allow", "reason": "Ok", "volume": 1}):
            with self.subTest(verdict=verdict):
                engine, worker, _ = self.filtered(verdict)
                engine.step(self.now)
                worker.queue.join()
                self.assertEqual(engine.step(self.now)["status"], "waiting")
                self.broker.send.assert_not_called()
                self.assertEqual(self.store.data["attempts"], {})
                self.assertFalse(self.store.data["halted"])
                worker.close()

    def test_approval_does_not_override_fresh_equity_budget(self):
        engine, _, _ = self.approved()
        self.broker.account.side_effect = [self.account, replace(self.account, equity=9500)]
        self.assertEqual(engine.step(self.now)["status"], "halted")
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_approval_keeps_open_risk_and_exposure_limits(self):
        engine, _, _ = self.approved()
        self.broker.positions.side_effect = [[], [self.position(volume=10, sl=1.099)]]
        self.assertEqual(engine.step(self.now)["status"], "blocked")
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_final_budget_and_cashflow_guards_prevent_claim(self):
        engine, _, _ = self.approved()
        self.broker.account.side_effect = [self.account, self.account, replace(self.account, equity=9550)]
        self.assertEqual(engine.step(self.now)["status"], "blocked")
        self.assertEqual(self.store.data["attempts"], {})
        self.broker.account.side_effect = None
        self.broker.activity.side_effect = [(False, False), (False, True)]
        self.assertEqual(engine.step(self.now)["status"], "halted")
        self.assertEqual(self.store.data["attempts"], {})

    def test_day_rollover_after_lookup_prevents_claim(self):
        engine, _, _ = self.approved()
        real_day = engine_tests.trading_day(self.now, 0)
        with patch("bijisatu.engine.trading_day", side_effect=[real_day, real_day, ("2026-10-07", self.now)]):
            self.assertEqual(engine.step(self.now)["status"], "waiting")
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})

    def test_pending_cpu_never_waits_or_halts(self):
        entered, release = threading.Event(), threading.Event()
        def stalled(snapshot):
            entered.set()
            release.wait(1)
            return {"decision": "allow", "reason": "Ok"}
        engine, worker, _ = self.filtered()
        with patch.object(worker.client, "judge", side_effect=stalled):
            try:
                before = time.monotonic()
                self.assertEqual(engine.step(self.now)["status"], "waiting")
                self.assertLess(time.monotonic() - before, 0.25)
                self.assertTrue(entered.wait(1))
                self.assertEqual(engine.step(self.now)["status"], "waiting")
                self.assertFalse(self.store.data["halted"])
                self.assertEqual(self.store.data["attempts"], {})
                self.broker.send.assert_not_called()
            finally:
                release.set()
                worker.queue.join()

    def test_final_quote_guard_prevents_claim(self):
        engine, _, _ = self.approved()
        self.broker.tick.side_effect = [Tick(1.1, 1.1001, self.now), Tick(1.1, 1.1001, self.now),
                                        Tick(1.1, 1.1001, self.now - 60)]
        self.assertEqual(engine.step(self.now)["status"], "blocked")
        self.assertEqual(self.store.data["attempts"], {})
        self.assertFalse(self.store.data["halted"])

    def test_preflight_failure_does_not_claim(self):
        engine, _, _ = self.approved()
        self.broker.send.side_effect = BrokerError("Margin rejected before callback")
        self.assertEqual(engine.step(self.now)["status"], "blocked")
        self.assertEqual(self.store.data["attempts"], {})

    def test_expired_or_changed_side_never_send(self):
        engine, worker, _ = self.approved()
        self.evaluate.return_value = replace(self.evaluate.return_value, side="sell")
        self.assertEqual(engine.step(self.now)["status"], "waiting")
        self.broker.send.assert_not_called()
        self.evaluate.return_value = replace(self.evaluate.return_value, side="buy")
        worker._records["EURUSD"].requested_at -= 31
        self.assertEqual(engine.step(self.now)["status"], "waiting")
        self.broker.send.assert_not_called()

    def test_uncertain_acknowledgement_halts_and_never_retries(self):
        engine, _, _ = self.approved()
        def uncertain(plan, *, before_send):
            before_send()
            raise OrderUncertain("No acknowledgement")
        self.broker.send.side_effect = uncertain
        self.assertEqual(engine.step(self.now)["status"], "halted")
        self.assertEqual(engine.step(self.now)["status"], "halted")
        self.broker.send.assert_called_once()
        self.assertEqual(len(self.store.data["attempts"]), 1)

