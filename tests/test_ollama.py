import io
import json
import logging
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from bijisatu.config import Config
from bijisatu.models import Bar, Signal
from bijisatu.ollama import (
    MAX_BODY, OllamaClient, OllamaObserver, _DeadlineReader, market_snapshot, validate_endpoint,
)


class FakeSocket:
    def __init__(self, raw):
        self.data = io.BytesIO(raw)
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv_into(self, buffer, count):
        chunk = self.data.read(count)
        buffer[:len(chunk)] = chunk
        return len(chunk)


def response(content=None, status=200, extra=None):
    envelope = {"done": True, "message": {"role": "assistant", "content": content or '{"decision":"allow","reason":"Trend confirmed"}'}}
    if extra:
        envelope.update(extra)
    body = json.dumps(envelope).encode()
    return f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body


class ClientTests(unittest.TestCase):
    def call(self, raw):
        connection = Mock()
        connection.sock = FakeSocket(raw)
        with patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection) as factory:
            result = OllamaClient("http://localhost:11435", "qwen3:4b", 0.5).judge({"atr": 0.002})
        factory.assert_called_once_with("127.0.0.1", 11435, timeout=0.5)
        connection.close.assert_called_once()
        return result, connection

    def test_structured_request_and_no_proxy_or_dns(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://external.invalid", "ALL_PROXY": "http://external.invalid"}):
            result, connection = self.call(response())
        self.assertEqual(result["decision"], "allow")
        args = connection.request.call_args
        self.assertEqual(args.args, ("POST", "/api/chat"))
        body = json.loads(args.kwargs["body"])
        self.assertFalse(body["think"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["options"], {"temperature": 0, "seed": 0, "num_predict": 64, "num_ctx": 2048})
        self.assertFalse(body["format"]["additionalProperties"])
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"atr": 0.002})

    def test_filter_prompt_and_exact_response_model_validation(self):
        for model, valid in (("qwen3:4b", True), ("other", False), (None, False)):
            connection = Mock()
            connection.sock = FakeSocket(response(extra={"model": model}))
            with patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection):
                client = OllamaClient("http://127.0.0.1:11435", "qwen3:4b", 0.5, entry_filter=True)
                if valid:
                    self.assertEqual(client.judge({"symbol": "EURUSD"})["decision"], "allow")
                else:
                    with self.assertRaises(ValueError):
                        client.judge({"symbol": "EURUSD"})
                body = json.loads(connection.request.call_args.kwargs["body"])
                self.assertIn("entry veto only", body["messages"][0]["content"])
                self.assertNotIn("observation only", body["messages"][0]["content"])
                connection.close.assert_called_once()

    def test_redirects_and_non_success_are_never_followed(self):
        for status in (301, 302, 307, 308, 404, 500):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.call(response(status=status))

    def test_untrusted_response_rejected(self):
        bad = [
            '{"decision":"execute","reason":"x"}',
            '{"decision":true,"reason":"x"}',
            '{"decision":"skip","reason":3}',
            '{"decision":"skip","reason":""}',
            '{"decision":"skip","reason":"x","volume":1}',
            '{"decision":"skip","reason":"x","decision":"allow"}',
            '{"decision":"allow","reason":"line\\nbreak"}',
            json.dumps({"decision": "skip", "reason": "x" * 161}),
            '{"decision":"skip","reason":NaN}',
            '```json\n{"decision":"skip","reason":"x"}\n```',
            '[]',
        ]
        for content in bad:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.call(response(content))
        for extra in ({"done": False}, {"duration": float("nan")}, {"duration": float("inf")},
                      {"message": {"role": "assistant", "content": "{}", "tool_calls": []}}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.call(response(extra=extra))

    def test_bounded_body(self):
        body = b"x" * (MAX_BODY + 1)
        raw = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        with self.assertRaises(ValueError):
            self.call(raw)

    def test_deadline_applies_to_every_read_and_headers_are_bounded(self):
        sock = FakeSocket(b"abcd")
        reader = _DeadlineReader(sock, 10)
        with patch("bijisatu.ollama.time.monotonic", side_effect=[9, 10.1]):
            self.assertEqual(reader.readinto(bytearray(1)), 1)
            with self.assertRaises(TimeoutError):
                reader.readinto(bytearray(1))
        self.assertEqual(sock.timeouts, [1])
        reader.remaining = 0
        with patch("bijisatu.ollama.time.monotonic", return_value=9), self.assertRaises(ValueError):
            reader.readinto(bytearray(1))

    def test_connection_uses_up_deadline_without_sending_request(self):
        connection = Mock()
        with patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection), \
                patch("bijisatu.ollama.time.monotonic", side_effect=[100, 100.6]):
            with self.assertRaises(TimeoutError):
                OllamaClient("http://127.0.0.1:11435", "qwen3:4b", 0.5).judge({})
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_timeout_and_connection_errors_close_socket(self):
        for failure in (TimeoutError(), ConnectionRefusedError()):
            connection = Mock()
            connection.connect.side_effect = failure
            with patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection):
                with self.assertRaises(type(failure)):
                    OllamaClient("http://127.0.0.1:11435", "qwen3:4b", 0.1).judge({})
            connection.close.assert_called_once()


class ObservationConfigTests(unittest.TestCase):
    def test_loopback_origins_only(self):
        for url in ("http://127.0.0.1:11435", "http://localhost:11435/", "http://[::1]:11435", "http://127.1.2.3"):
            validate_endpoint(url)
        for url in ("https://localhost", "http://example.com", "http://192.168.1.2", "http://0.0.0.0",
                    "http://127.0.0.1.evil", "http://localhost@evil", "http://user@localhost",
                    "http://localhost:0", "http://localhost:65536", "http://localhost/api/chat",
                    "http://localhost?q=x", "http://localhost#x", "http://localhost?", "http://localhost#",
                    "http://localhost\n", "http://[::1%25eth0]", "file://localhost", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_endpoint(url)

    def test_defaults_disabled_and_invalid_settings(self):
        self.assertFalse(Config().ollama_observation_enabled)
        Config().validate()
        fields = {
            "ollama_observation_enabled": [1, "true"],
            "ollama_observation_model": [None, "", "x" * 129, "qwen\n", "qwen name"],
            "ollama_observation_timeout_seconds": [True, 0, 31, float("nan"), float("inf"), "10"],
            "ollama_observation_queue_capacity": [True, 0, 33, 1.0],
        }
        for name, values in fields.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(Config(), **{name: value}).validate()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.signal = Signal("buy", 600, 0.002, "do not transmit this reason")
        self.bars = [Bar(600, 1.1, 1.2, 1.0, 1.15)]
        self.trend = [Bar(300, 1.1, 1.2, 1.0, 1.15)]
        self.args = ("EURUSD", self.signal, 1, 5, self.bars, self.trend, 0.0001)
        self.config = replace(Config(), ollama_observation_timeout_seconds=0.1, ollama_observation_queue_capacity=1)

    def test_snapshot_contains_only_bounded_closed_market_data(self):
        snapshot = market_snapshot(*self.args)
        encoded = json.dumps(snapshot)
        self.assertNotIn(self.signal.reason, encoded)
        for key in ("account", "login", "equity", "volume", "state", "credentials"):
            self.assertNotIn(key, encoded)
        self.assertEqual(snapshot["context"]["trend_side"], "buy")
        self.assertLess(len(encoded), 2048)
        for bad_bar in (replace(self.bars[0], time=660), replace(self.bars[0], close=float("nan"))):
            with self.assertRaises(ValueError):
                market_snapshot(*self.args[:4], [bad_bar], self.trend, 0.0001)

    def test_results_errors_and_dedup_are_log_only(self):
        for result in ({"decision": "allow", "reason": "ok"}, {"decision": "skip", "reason": "no"},
                       TimeoutError("secret raw text"), ValueError("secret raw text")):
            with self.subTest(result=result):
                completed = threading.Event()
                def judge(snapshot):
                    completed.set()
                    if isinstance(result, Exception):
                        raise result
                    return result
                with patch("bijisatu.ollama.OllamaClient.judge", side_effect=judge), self.assertLogs("bijisatu", level=logging.INFO) as logs:
                    observer = OllamaObserver(self.config)
                    observer.submit(*self.args)
                    self.assertTrue(completed.wait(1))
                    observer.queue.join()
                    observer.submit(*self.args)
                    observer.close()
                events = [record.event for record in logs.records]
                self.assertEqual(events.count("ollama_observation_queued"), 1)
                expected = "ollama_observation_result" if isinstance(result, dict) else (
                    "ollama_observation_unavailable" if isinstance(result, TimeoutError) else "ollama_observation_error")
                self.assertIn(expected, events)
                self.assertNotIn("secret raw text", str([r.details for r in logs.records]))
                self.assertFalse(observer._thread.is_alive())

    def test_stalled_http_timeout_is_bounded_without_waiting_on_submission(self):
        reading = threading.Event()
        class StalledSocket(FakeSocket):
            def recv_into(self, buffer, count):
                reading.set()
                threading.Event().wait(self.timeouts[-1])
                raise TimeoutError("mocked CPU inference stalled")
        connection = Mock()
        connection.sock = StalledSocket(b"")
        with patch("bijisatu.ollama.http.client.HTTPConnection", return_value=connection), \
                self.assertLogs("bijisatu", level=logging.INFO) as logs:
            observer = OllamaObserver(self.config)
            try:
                observer.submit(*self.args)
                self.assertTrue(reading.wait(1))
                started = time.monotonic()
                observer.submit("GBPUSD", *self.args[1:])
                self.assertLess(time.monotonic() - started, 0.05)
                observer.close()
                self.assertLess(time.monotonic() - started, 0.4)
                self.assertFalse(observer._thread.is_alive())
            finally:
                observer.close()
        connection.close.assert_called_once()
        self.assertTrue(any(r.event == "ollama_observation_unavailable" and r.details.get("reason") == "timeout" for r in logs.records))

    def test_saturation_is_nonblocking_and_shutdown_cancels_pending(self):
        started, release = threading.Event(), threading.Event()
        def judge(snapshot):
            started.set()
            release.wait(1)
            return {"decision": "skip", "reason": "ok"}
        with patch("bijisatu.ollama.OllamaClient.judge", side_effect=judge), self.assertLogs("bijisatu", level=logging.INFO) as logs:
            observer = OllamaObserver(self.config)
            try:
                observer.submit(*self.args)
                self.assertTrue(started.wait(1))
                before = time.monotonic()
                observer.submit("GBPUSD", *self.args[1:])
                observer.submit("USDJPY", *self.args[1:])
                observer.submit("USDJPY", *self.args[1:])
                self.assertLess(time.monotonic() - before, 0.1)
                self.assertEqual(observer.queue.qsize(), 1)
                observer.close()
                count = len(logs.records)
                release.set()
                observer._thread.join(1)
                self.assertEqual(len(logs.records), count)
            finally:
                release.set()
                observer.close()
        reasons = [r.details.get("reason") for r in logs.records]
        self.assertEqual(reasons.count("queue_full"), 1)
        self.assertIn("shutdown_cancelled", reasons)


if __name__ == "__main__":
    unittest.main()

