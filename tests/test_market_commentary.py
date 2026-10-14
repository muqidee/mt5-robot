"""Offline commentary, authorization separation and bounded shared scheduling."""

from dataclasses import replace
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from bijisatu.config import Config
from bijisatu.engine import Engine
from bijisatu.models import Bar, Signal
from bijisatu.observability import configured_logging
from bijisatu.ollama import MarketCommentary, OllamaClient, OllamaEntryFilter, commentary_snapshot, market_snapshot
import test_engine as engine_tests
from test_ollama import FakeSocket, response
from test_short_term import short_term_bars


class CommentaryEngineTests(unittest.TestCase):
    position = engine_tests.EngineTests.position
    baseline = engine_tests.EngineTests.baseline

    def setUp(self):
        engine_tests.EngineTests.setUp(self)
        self.entry, self.trend = short_term_bars()
        self.now = self.entry[-1].time + 3600
        self.config = replace(self.config, strategy_mode="short_term", market_commentary_enabled=True)
        self.broker.bars.side_effect = lambda symbol, minutes: self.entry if minutes == 60 else self.trend
        self.evaluate.return_value = None

    def worker(self, result=None, failure=None):
        options = {"side_effect": failure} if failure else {"return_value": result or {"bias": "buy", "reason": "Market only"}}
        mocked = patch("bijisatu.ollama.OllamaClient.judge", **options)
        judge = mocked.start()
        self.addCleanup(mocked.stop)
        worker = MarketCommentary(self.config)
        self.addCleanup(worker.close)
        return worker, judge

    def test_startup_no_setup_once_then_new_closed_bar(self):
        self.entry[-1] = replace(self.entry[-1], open=100, close=100)
        worker, judge = self.worker()
        engine = Engine(self.broker, self.config, self.store, True, commentary=worker)
        for _ in range(3):
            result = engine.step(self.now)
            worker.queue.join()
        self.assertEqual(result["skipped"], {"no_trend_pullback_setup": 1})
        judge.assert_called_once()
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})
        self.now += 3600
        self.entry = self.entry[1:] + [replace(self.entry[-1], time=self.entry[-1].time + 3600)]
        engine.step(self.now)
        worker.queue.join()
        self.assertEqual(judge.call_count, 2)

    def test_held_symbol_has_market_only_commentary_even_at_position_limit(self):
        worker, judge = self.worker()
        self.baseline()
        self.broker.positions.return_value = [self.position(symbol="EURUSD")]
        config = replace(self.config, max_positions=1)
        result = Engine(self.broker, config, self.store, True, commentary=worker).step(self.now)
        worker.queue.join()
        self.assertEqual(result["status"], "blocked")
        judge.assert_called_once()
        self.broker.send.assert_not_called()
        snapshot = judge.call_args.args[0]
        for key in ("side", "account", "equity", "volume", "login", "order"):
            self.assertNotIn(key, snapshot)

    def test_disabled_no_market_fetch_or_network_and_invalid_settings(self):
        worker = Mock()
        self.broker.positions.return_value = [self.position(symbol="EURUSD")]
        self.baseline()
        config = replace(self.config, market_commentary_enabled=False)
        Engine(self.broker, config, self.store, commentary=worker).step(self.now)
        worker.submit.assert_not_called()
        self.broker.bars.assert_not_called()
        self.assertFalse(Config().market_commentary_enabled)
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                replace(Config(), market_commentary_enabled=value).validate()

    def test_invalid_future_stale_and_warmup_do_not_request(self):
        worker, judge = self.worker()
        original = self.entry
        for entry in (original[:-1], original[-10:],
                      original[:-1] + [replace(original[-1], low=500)],
                      original[:-1] + [replace(original[-1], close=float("nan"))],
                      original[:-1] + [replace(original[-1], time=self.now)],
                      [replace(bar, time=bar.time - 72000) for bar in original]):
            self.entry = entry
            engine = Engine(self.broker, self.config, self.store, commentary=worker)
            engine.step(self.now)
            worker.queue.join()
        judge.assert_not_called()

    def test_commentary_never_supplies_filter_approval(self):
        worker, judge = self.worker()
        self.evaluate.return_value = Signal("buy", self.entry[-1].time, 2, "controlled")
        config = replace(self.config, ollama_filter_enabled=True)
        entry_filter = Mock()
        entry_filter.check.return_value = ("pending", None)
        engine = Engine(self.broker, config, self.store, True, commentary=worker, entry_filter=entry_filter)
        engine.step(self.now)
        worker.queue.join()
        self.broker.send.assert_not_called()
        self.assertEqual(self.store.data["attempts"], {})
        entry_filter.lookup.assert_not_called()
        judge.assert_called_once()

    def test_commentary_error_or_neutral_cannot_suppress_approved_strategy(self):
        self.evaluate.return_value = Signal("buy", self.entry[-1].time, .002, "controlled")
        for verdict in (TimeoutError(), ValueError(), {"bias": "neutral", "reason": "Mixed"},
                        {"decision": "skip", "reason": "Not commentary"}):
            worker, _ = self.worker(failure=verdict if isinstance(verdict, Exception) else None,
                                    result=None if isinstance(verdict, Exception) else verdict)
            entry_filter = Mock()
            entry_filter.check.return_value = ("allowed", object())
            entry_filter.lookup.return_value = "allowed"
            self.broker.send.reset_mock()
            store = type(self.store)(self.directory / (uuid4().hex + ".json"))
            config = replace(self.config, ollama_filter_enabled=True)
            result = Engine(self.broker, config, store, True, commentary=worker, entry_filter=entry_filter).step(self.now)
            worker.queue.join()
            self.assertEqual(result["status"], "sent")
            self.broker.send.assert_called_once()
            worker.close()

    def test_rejection_conditions_are_info_once_per_bar(self):
        self.evaluate.side_effect = None
        self.evaluate.return_value = None
        worker, _ = self.worker()
        engine = Engine(self.broker, self.config, self.store, commentary=worker)
        with self.assertLogs("bijisatu", level="INFO") as logs:
            engine.step(self.now)
            worker.queue.join()
            engine.step(self.now)
        assessments = [record for record in logs.records if getattr(record, "event", "") == "setup_assessment"]
        self.assertEqual(len(assessments), 1)
        self.assertIn("failed_conditions", assessments[0].details)


class CommentaryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Config(), strategy_mode="short_term", market_commentary_enabled=True,
                              ollama_observation_timeout_seconds=.1, ollama_observation_queue_capacity=1)
        self.entry, self.trend = short_term_bars()
        self.now = self.entry[-1].time + 3600
        self.args = ("TEST", 60, 240, self.entry, self.trend, self.now)

    def test_filter_snapshot_carries_measured_conditions_not_assumed_side(self):
        signal = Signal("sell", self.entry[-1].time, 2, "Synthetic conflict")
        snapshot = market_snapshot("TEST", signal, 60, 240, self.entry, self.trend, .01)
        self.assertEqual(snapshot["side"], "sell")
        self.assertEqual(snapshot["context"]["trend_side"], "buy")
        self.assertFalse(snapshot["context"]["pullback_reclaimed"])
        measured = snapshot["context"]["assessment"]
        self.assertTrue(measured["conditions"]["buy"]["trend"])
        self.assertFalse(measured["conditions"]["sell"]["trend"])
        self.assertIn("ema20_slope", measured["trend_candles"][-1])

    def test_timeout_is_consumed_and_symbol_capacity_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def stalled(snapshot):
            entered.set()
            release.wait(2)
            raise TimeoutError()
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=stalled) as judge:
            worker = MarketCommentary(replace(self.config, max_symbols=2))
            try:
                worker.submit(*self.args)
                self.assertTrue(entered.wait(1))
                worker.submit("SECOND", *self.args[1:])
                worker.submit("THIRD", *self.args[1:])
                worker.submit("THIRD", *self.args[1:])
                release.set()
                worker.queue.join()
                worker.submit(*self.args)
                self.assertEqual(judge.call_count, 2)
            finally:
                release.set()
                worker.close()

    def test_startup_batch_includes_five_symbols_without_scheduler_race(self):
        config = replace(self.config, max_symbols=5)
        with patch("bijisatu.ollama.OllamaClient.judge", return_value={"bias": "neutral", "reason": "Mixed"}) as judge:
            worker = MarketCommentary(config, entry_filter=Mock())
            try:
                for symbol in ("EURUSD", "USDJPY", "XAUUSD", "GBPUSD", "AUDUSD"):
                    worker.submit(symbol, *self.args[1:])
                self.assertEqual(worker.queue.qsize(), 5)
                for _ in range(5):
                    worker.run_one()
                self.assertEqual(judge.call_count, 5)
                self.assertEqual(worker.queue.maxsize, 5)
            finally:
                worker.close()

    def test_shared_worker_filter_priority_and_no_parallel_cpu_requests(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def judge(snapshot):
            calls.append(snapshot["symbol"])
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return {"bias": "neutral", "reason": "Market only"} if "assessment" in snapshot else {"decision": "allow", "reason": "Aligned"}
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=judge):
            entry_filter = OllamaEntryFilter(self.config)
            worker = MarketCommentary(self.config, entry_filter=entry_filter)
            try:
                worker.submit(*self.args)
                self.assertTrue(entered.wait(1))
                worker.submit("COMMENT", *self.args[1:])
                signal = Signal("buy", self.entry[-1].time, 2, "controlled")
                entry_filter.check("FILTER", signal, 60, 240, self.entry, self.trend, .01)
                release.set()
                entry_filter.queue.join()
                worker.queue.join()
                self.assertEqual(calls, ["TEST", "FILTER", "COMMENT"])
                self.assertIsNone(worker._thread)
            finally:
                release.set()
                worker.close()
                entry_filter.close()

    def test_actual_mock_http_result_timeout_invalid_and_compact_daily_logs(self):
        directory = Path(__file__).parent / (".commentary-log-" + uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        console = io.StringIO()
        snapshot = commentary_snapshot(*self.args)
        for content, failure in (('{"bias":"neutral","reason":"Closed candles mixed"}', None),
                                 ('private raw untrusted reply', None), (None, TimeoutError())):
            connection = Mock()
            connection.sock = FakeSocket(response(content, extra={"model": "qwen3:4b"}))
            connection.request.side_effect = failure
            with redirect_stderr(console), configured_logging(directory, verbose=False, mode="test", broker_utc_offset_hours=None), patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection):
                client = OllamaClient("http://127.0.0.1:11435", "qwen3:4b", .1, market_commentary=True)
                if failure or content == 'private raw untrusted reply':
                    with self.assertRaises((TimeoutError, ValueError)):
                        client.judge(snapshot)
                else:
                    self.assertEqual(client.judge(snapshot)["bias"], "neutral")
            body = json.loads(connection.request.call_args.kwargs["body"])
            self.assertEqual(body["options"]["num_thread"], 4)
            self.assertEqual(body["options"]["num_predict"], 128)
            self.assertEqual(body["format"]["required"], ["bias", "reason"])
        records = [json.loads(line) for path in (directory / "bijisatu").glob("*.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual(sum(record["event"] == "ollama_request_started" for record in records), 3)
        self.assertEqual(sum(record["event"] == "ollama_request_result" for record in records), 1)
        readable = "".join(path.read_text() for path in (directory / "bijisatu").glob("*.log"))
        self.assertIn("Request id:", readable)
        self.assertNotIn("Request id:", console.getvalue())
        for output in (console.getvalue(), readable):
            self.assertIn("Bias: NEUTRAL", output)
            self.assertIn("Latency:", output)
            self.assertIn("timed out", output)
            self.assertNotIn("private raw untrusted reply", output)
            self.assertNotIn("Latest entry candle", output)
            self.assertTrue(all(len(line) <= 96 for line in output.splitlines()))


if __name__ == "__main__":
    unittest.main()

