"""Human-readable request/reply logs using mocked sockets, never a service."""

from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from bijisatu.config import Config
from bijisatu.observability import configured_logging
from bijisatu.ollama import OllamaClient, OllamaEntryFilter, OllamaObserver, market_snapshot
from bijisatu.strategy import evaluate
from test_ollama import FakeSocket, response
from test_short_term import short_term_bars


class RequestLoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / (".request-log-test-" + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.console = io.StringIO()
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        self.clock = patch("bijisatu.observability._utc_now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.config = Config(strategy_mode="short_term", ollama_observation_enabled=True)
        self.entry, self.trend = short_term_bars()
        self.signal = evaluate(self.entry, self.trend, entry_minutes=60, trend_minutes=240)
        self.snapshot = market_snapshot("TEST", self.signal, 60, 240, self.entry, self.trend, .01)

    def records(self):
        return [json.loads(line) for path in (self.directory / "bijisatu").glob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()]

    def scope(self):
        return configured_logging(self.directory, verbose=False, mode="dry-run", broker_utc_offset_hours=None)

    def test_actual_worker_http_send_and_reply_linked_visible_in_both_modes(self):
        for filter_mode in (False, True):
            with self.subTest(filter_mode=filter_mode):
                connection = Mock()
                connection.sock = FakeSocket(response(extra={"model": "qwen3:4b"}))
                records_at_send = []
                connection.request.side_effect = lambda *args, **kwargs: records_at_send.extend(self.records())
                config = replace(self.config, ollama_filter_enabled=filter_mode,
                                 ollama_observation_enabled=not filter_mode)
                with redirect_stderr(self.console), self.scope(), patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection):
                    worker = OllamaEntryFilter(config) if filter_mode else OllamaObserver(config)
                    try:
                        args = ("TEST", self.signal, 60, 240, self.entry, self.trend, .01)
                        worker.check(*args) if filter_mode else worker.submit(*args)
                        worker.queue.join()
                    finally:
                        worker.close()
                connection.request.assert_called_once()
                starts = [r for r in records_at_send if r["event"] == "ollama_request_started"]
                self.assertTrue(starts)
                start = starts[-1]["details"]
                result = next(r["details"] for r in self.records()
                              if r["event"] == "ollama_request_result" and r["details"]["request_id"] == start["request_id"])
                self.assertEqual(result["decision"], "allow")
                self.assertEqual(result["reason"], "Trend confirmed")
                self.assertGreaterEqual(result["latency_seconds"], 0)
                self.assertEqual(start["entry_timeframe"], "H1")
                self.assertEqual(start["trend_timeframe"], "H4")
                self.assertEqual(start["strategy_mode"], "short_term")
                self.assertEqual(start["ollama_mode"], "filter" if filter_mode else "observe")
                self.assertEqual(start["timeout_seconds"], 10)
                text = "".join(path.read_text(encoding="utf-8") for path in (self.directory / "bijisatu").glob("*.log"))
                self.assertIn("Request id: " + start["request_id"], text)
                self.assertNotIn("Request id:", self.console.getvalue())
                self.assertNotIn(start["request_id"], self.console.getvalue())
                for output in (text, self.console.getvalue()):
                    self.assertIn("Reason: Trend confirmed", output)
                    self.assertIn("Decision: ALLOW", output)
                body = json.loads(connection.request.call_args.kwargs["body"])
                prompt = json.loads(body["messages"][1]["content"])
                for key in ("account", "account_login", "equity", "balance", "password", "volume", "terminal_path"):
                    self.assertNotIn(key, prompt)
                    self.assertNotIn(key, start)

    def test_console_hides_request_ids_and_wraps_compact_results(self):
        request_id = "abc123" * 5
        reason = "Trend is bearish, but confirmation must use a closed entry candle. " * 3
        details = {
            "symbol": "GBPUSDc", "model": "qwen3.5:4b", "request_id": request_id,
            "bar_time": 1791453600, "entry_timeframe": "H1", "trend_timeframe": "H4",
            "bias": "sell", "reason": reason, "latency_seconds": .562,
        }
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                console = io.StringIO()
                with redirect_stderr(console), configured_logging(
                    self.directory, verbose=verbose, mode="dry-run", broker_utc_offset_hours=None
                ) as logger:
                    logger.info("Market commentary request result", extra={
                        "event": "ollama_request_result", "details": details,
                    })
                output = console.getvalue()
                self.assertNotIn("Request id:", output)
                self.assertNotIn(request_id, output)
                if not verbose:
                    self.assertIn("Bias: SELL\n", output)
                    self.assertIn("Latency: 0.562 s", output)
                    self.assertIn("Timeframes: H1/H4", output)
                    self.assertNotIn("1791453600", output)
                    self.assertTrue(all(len(line) <= 96 for line in output.splitlines()))
                self.assertEqual(self.records()[-1]["details"], details)
                readable = "".join(path.read_text(encoding="utf-8")
                                   for path in (self.directory / "bijisatu").glob("*.log"))
                self.assertIn("Request id: " + request_id, readable)

    def test_queue_submission_is_not_a_request_start(self):
        entered, release = threading.Event(), threading.Event()
        connection = Mock()
        connection.sock = FakeSocket(response())
        connection.connect.side_effect = lambda: (entered.set(), release.wait(2))
        with redirect_stderr(self.console), self.scope(), patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection):
            worker = OllamaObserver(self.config)
            try:
                worker.submit("TEST", self.signal, 60, 240, self.entry, self.trend, .01)
                self.assertTrue(entered.wait(1))
                self.assertTrue(any(r["event"] == "ollama_observation_queued" for r in self.records()))
                self.assertFalse(any(r["event"] == "ollama_request_started" for r in self.records()))
                connection.request.assert_not_called()
            finally:
                release.set()
                worker.queue.join()
                worker.close()
        self.assertEqual(sum(r["event"] == "ollama_request_started" for r in self.records()), 1)

    def test_timeout_and_invalid_reply_explain_failure_without_echoing_content(self):
        for raw, failure, expected in ((response(), TimeoutError(), "timed out"),
                                      (response('private oversized arbitrary reply'), None, "Response rejected"),
                                      (response('{"decision":"allow","reason":"unfinished', extra={"done_reason": "length"}), None, "truncated")):
            with self.subTest(expected=expected):
                connection = Mock()
                connection.sock = FakeSocket(raw)
                connection.request.side_effect = failure
                with redirect_stderr(self.console), self.scope(), patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection):
                    with self.assertRaises((TimeoutError, ValueError)):
                        OllamaClient("http://127.0.0.1:11435", "qwen3:4b", .1).judge(self.snapshot)
                records = self.records()
                failed = [r["details"] for r in records if r["event"] == "ollama_request_failed"][-1]
                self.assertIn(expected, failed["reason"])
                self.assertTrue(any(r["event"] == "ollama_request_started" and r["details"]["request_id"] == failed["request_id"] for r in records))
                self.assertNotIn("private oversized arbitrary reply", self.console.getvalue())
                text = "".join(path.read_text(encoding="utf-8") for path in (self.directory / "bijisatu").glob("*.log"))
                self.assertIn(expected, text)
                self.assertNotIn("private oversized arbitrary reply", text)
                connection.close.assert_called_once()

    def test_daily_readable_file_same_console_format_append_rotation_and_normalization(self):
        with redirect_stderr(self.console), self.scope() as logger:
            for at in (self.now, self.now + timedelta(days=1)):
                record = logger.makeRecord(logger.name, 20, __file__, 1, "Human event", (), None,
                                           extra={"event": "test", "details": {"atr": float("nan"), "password": "hidden"}})
                record.created = at.timestamp()
                logger.handle(record)
        lines = self.console.getvalue()
        for at in (self.now, self.now + timedelta(days=1)):
            text = (self.directory / "bijisatu" / f"{at.date()}.log").read_text(encoding="utf-8")
            self.assertIn(text, lines)
            self.assertIn("Atr: Unavailable", text)
            self.assertIn("Password: [redacted]", text)
            self.assertNotIn("hidden", text)
        with redirect_stderr(self.console), self.scope() as logger:
            record.created = self.now.timestamp()
            logger.handle(record)
        self.assertEqual((self.directory / "bijisatu" / "2026-10-08.log").read_text().count("Human event"), 2)
        self.assertEqual(logger.handlers, [])

    def test_readable_disk_write_flush_and_close_failures_propagate(self):
        for operation in ("write", "flush", "close"):
            with self.subTest(operation=operation), redirect_stderr(self.console):
                saved_stream = None
                try:
                    with self.assertRaisesRegex(OSError, "Readable disk full"):
                        with self.scope() as logger:
                            handler = logger.handlers[1]
                            saved_stream = handler.stream
                            wrapped = Mock(wraps=saved_stream)
                            getattr(wrapped, operation).side_effect = OSError("Readable disk full")
                            handler.stream = wrapped
                            if operation != "close":
                                try:
                                    record = logger.makeRecord(
                                        logger.name, 20, __file__, 1, "Readable failure test", (), None
                                    )
                                    record.created = self.now.timestamp()
                                    logger.handle(record)
                                finally:
                                    handler.stream = saved_stream
                finally:
                    if saved_stream is not None:
                        saved_stream.close()
                self.assertEqual(logger.handlers, [])

    def test_readable_setup_failure_closes_json_stream_and_restores_logger(self):
        from bijisatu.observability import _DailyJsonLinesHandler
        handlers = []
        def factory(directory, suffix=".jsonl"):
            if suffix == ".log":
                raise OSError("Readable log disk unavailable")
            handler = _DailyJsonLinesHandler(directory)
            handlers.append(handler)
            return handler
        with patch("bijisatu.observability._DailyJsonLinesHandler", side_effect=factory):
            with self.assertRaisesRegex(OSError, "Readable log"):
                with self.scope():
                    pass
        self.assertIsNone(handlers[0].stream)


if __name__ == "__main__":
    unittest.main()

