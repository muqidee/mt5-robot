import io
import json
import logging
import math
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import UUID

from bijisatu.observability import configured_logging, market_sessions


UTC = timezone.utc
START = datetime(2026, 3, 16, 14, 0, tzinfo=UTC)


class MarketSessionTests(unittest.TestCase):
    def test_naive_datetime_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            market_sessions(datetime(2026, 3, 16, 14))

    def test_all_local_session_boundaries(self):
        from zoneinfo import ZoneInfo

        for name, zone, start, end in (
            ("Sydney", "Australia/Sydney", 8, 17),
            ("Tokyo", "Asia/Tokyo", 9, 18),
            ("London", "Europe/London", 8, 17),
            ("New York", "America/New_York", 8, 17),
        ):
            with self.subTest(session=name):
                opening = datetime(2026, 3, 16, start, tzinfo=ZoneInfo(zone))
                closing = opening.replace(hour=end)
                self.assertNotIn(name, market_sessions(opening - timedelta(microseconds=1)))
                self.assertIn(name, market_sessions(opening))
                self.assertIn(name, market_sessions(closing - timedelta(microseconds=1)))
                self.assertNotIn(name, market_sessions(closing))
                self.assertEqual(market_sessions(opening), market_sessions(opening.astimezone(UTC)))

    def test_us_uk_spring_dst_mismatch_weeks(self):
        for day, london_at_seven, new_york_at_twelve in (
            ("2026-03-06", False, False),
            ("2026-03-09", False, True),
            ("2026-03-27", False, True),
            ("2026-03-30", True, True),
        ):
            with self.subTest(day=day):
                morning = datetime.fromisoformat(day + "T07:00:00+00:00")
                noon = morning.replace(hour=12)
                self.assertEqual("London" in market_sessions(morning), london_at_seven)
                self.assertEqual("New York" in market_sessions(noon), new_york_at_twelve)

    def test_us_uk_autumn_dst_mismatch_week(self):
        for day, london_at_seven, new_york_at_twelve in (
            ("2026-10-23", True, True),
            ("2026-10-26", False, True),
            ("2026-11-02", False, False),
        ):
            with self.subTest(day=day):
                morning = datetime.fromisoformat(day + "T07:00:00+00:00")
                self.assertEqual("London" in market_sessions(morning), london_at_seven)
                self.assertEqual("New York" in market_sessions(morning.replace(hour=12)), new_york_at_twelve)

    def test_sydney_dst_transitions(self):
        for at, expected in (
            ("2026-04-02T21:00:00+00:00", True),
            ("2026-04-05T21:00:00+00:00", False),
            ("2026-04-05T22:00:00+00:00", True),
            ("2026-10-01T21:00:00+00:00", False),
            ("2026-10-04T21:00:00+00:00", True),
        ):
            with self.subTest(at=at):
                self.assertEqual("Sydney" in market_sessions(datetime.fromisoformat(at)), expected)

    def test_overlapping_sessions(self):
        self.assertEqual(market_sessions(START), ["London", "New York"])
        self.assertEqual(market_sessions(START.replace(hour=1)), ["Sydney", "Tokyo"])

    def test_weekday_is_local_not_utc(self):
        self.assertEqual(market_sessions(datetime(2026, 3, 15, 21, tzinfo=UTC)), ["Sydney"])
        self.assertEqual(market_sessions(datetime(2026, 3, 13, 20, tzinfo=UTC)), ["New York"])
        self.assertEqual(market_sessions(datetime(2026, 3, 14, 14, tzinfo=UTC)), [])


class LoggingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix=".observability-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.console = io.StringIO()
        self.clock = patch("bijisatu.observability._utc_now", return_value=START)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.stderr = redirect_stderr(self.console)
        self.stderr.__enter__()
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def scope(self, **kwargs):
        options = dict(verbose=False, mode="paper", broker_utc_offset_hours=None)
        options.update(kwargs)
        return configured_logging(self.directory, **options)

    def emit(self, logger, message="Market update", *, at=START, level=logging.INFO, **extra):
        record = logger.makeRecord(logger.name, level, __file__, 1, message, (), None, extra=extra)
        record.created = at.timestamp()
        logger.handle(record)

    def records(self, day="2026-03-16"):
        path = self.directory / "bijisatu" / f"{day}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_schema_and_nested_fields_do_not_override_metadata(self):
        with self.scope(broker_utc_offset_hours=5.5) as logger:
            self.assertEqual(logger.name, "bijisatu")
            self.assertEqual(logger.level, logging.DEBUG)
            self.emit(logger, "Price café", event="quote", details={"robot": "other", "mode": "other", "price": 1.2})
            record, = self.records()
        self.assertEqual(record["timestamp_utc"], START.isoformat())
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["event"], "quote")
        self.assertEqual(record["message"], "Price café")
        self.assertEqual(record["robot"], "BijiSatu")
        self.assertEqual(record["mode"], "paper")
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["active_sessions"], ["London", "New York"])
        self.assertEqual(record["broker_time"], "2026-03-16T19:30:00+05:30")
        self.assertEqual(record["broker_utc_offset_hours"], 5.5)
        self.assertEqual(record["details"], {"robot": "other", "mode": "other", "price": 1.2})
        self.assertEqual(UUID(record["run_id"]).version, 4)
        self.assertIn("UTC | INFO | Mode: paper | Session: London + New York | Price café", self.console.getvalue())
        self.assertIn("Price: 1.2", self.console.getvalue())

    def assert_human_console(self):
        console = self.console.getvalue()
        self.assertNotIn("{", console)
        self.assertNotIn("}", console)
        self.assertNotIn('"', console)

    def test_console_header_always_shows_time_level_mode_and_sessions(self):
        for at, sessions, details in (
            (START, "London + New York", {}),
            (START.replace(hour=1), "Sydney + Tokyo", {}),
            (datetime(2026, 3, 14, 14, tzinfo=UTC), "No active sessions", None),
        ):
            with self.subTest(at=at), self.scope(mode="backtest") as logger:
                self.console.seek(0)
                self.console.truncate()
                self.emit(logger, at=at, level=logging.WARNING, details=details)
                header = self.console.getvalue().splitlines()[0]
                self.assertIn(at.strftime("%Y-%m-%d %H:%M:%S") + " UTC", header)
                self.assertIn("| WARNING | Mode: backtest |", header)
                self.assertIn("Session: " + sessions, header)
                self.assert_human_console()

    def test_runtime_and_heartbeat_finances_are_readable_without_rounding_file_values(self):
        details = {
            "cycle": 7, "status": "Scanning", "remaining": 12, "symbols": 20,
            "skipped": {"spread_too_wide": 3, "no_signal": 5},
            "account": {
                "currency": "USD", "equity": 12345.67891, "balance": 12400.12345,
                "daily_baseline": 12500.56789, "daily_loss": 154.88898,
                "daily_loss_limit": 625.0283945, "open_positions": 2,
                "reserved_open_risk": 100.123456, "remaining_risk": 370.0159585,
            },
            "plan": {"symbol": "EURUSD", "entry": 1.123456789, "sl": 1.120000123,
                     "tp": 1.130987654, "risk_amount": 123.456789, "risk_fraction": 0.0123456789},
        }
        for event in ("runtime_status", "heartbeat"):
            with self.subTest(event=event), self.scope() as logger:
                self.console.seek(0)
                self.console.truncate()
                self.emit(logger, "Scan progress", event=event, details=details)
                text = self.console.getvalue()
                for expected in (
                    "Cycle: 7", "Status: Scanning", "Remaining: 12", "Symbols scanned: 20",
                    "Skipped reasons:\n    Spread too wide: 3\n    No signal: 5",
                    "Equity: 12,345.68 USD", "Balance: 12,400.12 USD",
                    "Daily baseline: 12,500.57 USD", "Daily loss: 154.89 USD",
                    "Daily loss limit: 625.03 USD", "Open positions: 2",
                    "Reserved open risk: 100.12 USD", "Remaining risk: 370.02 USD",
                    "Entry: 1.123456789", "SL: 1.120000123", "TP: 1.130987654",
                    "Risk amount: 123.46 USD", "Risk fraction: 0.0123456789",
                ):
                    self.assertIn(expected, text)
                self.assert_human_console()
                self.assertEqual(self.records()[-1]["details"], details)
        self.assertEqual(details["account"]["equity"], 12345.67891)

    def test_generic_nested_details_use_labels_instead_of_object_representations(self):
        details = {
            "symbols": ["EURUSD", "USDJPY"],
            "checks": [{"ready": True, "reason": None}, {"ready": False}],
            "metrics": {"price": 0.000000123456789, "ratio": 0.123456789, "count": 2},
            "skipped": {}, "pending": [],
        }
        with self.scope() as logger:
            self.emit(logger, event="analysis", details=details)
        text = self.console.getvalue()
        for expected in (
            "Symbols scanned: EURUSD, USDJPY", "Checks:\n    Item 1:\n      Ready: Yes",
            "Reason: Unavailable", "Item 2:\n      Ready: No", "Price: 0.000000123456789",
            "Ratio: 0.123456789", "Count: 2", "Skipped reasons: None", "Pending: None",
        ):
            self.assertIn(expected, text)
        self.assert_human_console()
        self.assertEqual(self.records()[0]["details"], details)

    def test_account_financial_snapshot_does_not_expose_identifiers_or_config(self):
        with self.scope() as logger:
            self.emit(logger, details={"account": {
                "currency": "EUR", "equity": 1.239, "id": 987654321,
                "login": "private-login", "password": "private-password",
                "config": {"token": "private-token"}, "unknown": "private-field",
            }})
        record, = self.records()
        self.assertEqual(record["details"]["account"], {"currency": "EUR", "equity": 1.239})
        combined = json.dumps(record) + self.console.getvalue()
        for secret in ("987654321", "private-login", "private-password", "private-token", "private-field"):
            self.assertNotIn(secret, combined)
        self.assertIn("Equity: 1.24 EUR", self.console.getvalue())
        self.assert_human_console()

    def test_missing_currency_and_nonfinite_money_are_displayed_without_invented_units(self):
        with self.scope() as logger:
            self.emit(logger, details={"account": {"equity": 1234.5678, "balance": float("nan")}})
        self.assertIn("Equity: 1,234.57\n", self.console.getvalue())
        self.assertIn("Balance: Unavailable", self.console.getvalue())
        self.assertEqual(self.records()[0]["details"]["account"], {"equity": 1234.5678, "balance": None})
        self.assert_human_console()

    def test_console_formatter_does_not_call_json_serializer(self):
        with self.scope() as logger:
            console_handler = logger.handlers[1]
            record = logger.makeRecord(logger.name, logging.INFO, __file__, 1, "Status", (), None,
                                       extra={"details": {"nested": {"value": 1.25}}})
            record.created = START.timestamp()
            with patch("bijisatu.observability.json.dumps", side_effect=AssertionError("JSON is file-only")):
                text = console_handler.format(record)
        self.assertIn("Nested:\n    Value: 1.25", text)

    def test_optional_broker_offset_is_explicit_and_default_event(self):
        with self.scope() as logger:
            self.emit(logger)
        record, = self.records()
        self.assertEqual(record["event"], "message")
        self.assertIsNone(record["broker_time"])
        self.assertIsNone(record["broker_utc_offset_hours"])

    def test_negative_broker_offset_keeps_utc_filename(self):
        with self.scope(broker_utc_offset_hours=-4) as logger:
            self.emit(logger, at=START.replace(hour=1))
        record, = self.records()
        self.assertEqual(record["broker_time"], "2026-03-15T21:00:00-04:00")
        self.assertEqual(record["broker_utc_offset_hours"], -4)

    def test_debug_always_in_file_but_console_obeys_verbose(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose), self.scope(verbose=verbose) as logger:
                self.console.seek(0)
                self.console.truncate()
                self.emit(logger, "Debug analysis", level=logging.DEBUG)
                self.emit(logger, "Visible status")
                self.assertEqual("Debug analysis" in self.console.getvalue(), verbose)
                self.assertIn("Visible status", self.console.getvalue())
        self.assertEqual([record["level"] for record in self.records()], ["DEBUG", "INFO"] * 2)

    def test_utc_daily_rollover_and_existing_days_are_preserved(self):
        archive = self.directory / "bijisatu" / "2000-01-01.jsonl"
        archive.parent.mkdir()
        archive.write_text('old history\n', encoding="utf-8")
        with self.scope() as logger:
            self.emit(logger, "Before midnight", at=START.replace(hour=23, minute=59, second=59))
            previous_stream = logger.handlers[0].stream
            self.emit(logger, "After midnight", at=datetime(2026, 3, 17, tzinfo=UTC))
            self.assertTrue(previous_stream.closed)
            self.assertEqual([r["message"] for r in self.records()], ["Before midnight"])
            self.assertEqual([r["message"] for r in self.records("2026-03-17")], ["After midnight"])
            self.emit(logger, "Clock moved back", at=START)
        self.assertEqual([r["message"] for r in self.records()], ["Before midnight", "Clock moved back"])
        self.assertEqual(archive.read_text(encoding="utf-8"), "old history\n")

    def test_restarts_append_and_use_distinct_run_ids(self):
        for index in range(2):
            with self.scope() as logger:
                self.emit(logger, f"Run {index}")
                self.emit(logger, f"Run {index} second record")
        records = self.records()
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0]["run_id"], records[1]["run_id"])
        self.assertEqual(records[2]["run_id"], records[3]["run_id"])
        self.assertNotEqual(records[0]["run_id"], records[2]["run_id"])

    def test_scope_restores_configuration_and_never_contaminates_root(self):
        logger = logging.getLogger("bijisatu")
        original = (logger.level, logger.handlers[:], logger.filters[:], logger.propagate, logger.disabled)
        previous_handler = logging.StreamHandler(io.StringIO())
        previous_filter = logging.Filter("blocked")
        root = logging.getLogger()
        root_handlers = root.handlers[:]
        root_probe = logging.StreamHandler(io.StringIO())
        root.addHandler(root_probe)
        try:
            logger.handlers = [previous_handler]
            logger.filters = [previous_filter]
            logger.setLevel(logging.ERROR)
            logger.propagate = True
            logger.disabled = True
            for fail in (False, True):
                try:
                    with self.scope() as configured:
                        own_handlers = configured.handlers[:]
                        stream = own_handlers[0].stream
                        self.emit(configured)
                        self.assertEqual(root.handlers, root_handlers + [root_probe])
                        if fail:
                            raise RuntimeError("scope failure")
                except RuntimeError as error:
                    self.assertEqual(str(error), "scope failure")
                self.assertTrue(stream.closed)
                self.assertTrue(all(handler._closed for handler in own_handlers))
                self.assertEqual(logger.handlers, [previous_handler])
                self.assertEqual(logger.filters, [previous_filter])
                self.assertEqual(logger.level, logging.ERROR)
                self.assertTrue(logger.propagate)
                self.assertTrue(logger.disabled)
                self.assertFalse(previous_handler._closed)
            self.assertEqual(len(self.records()), 2)
            self.assertEqual(root_probe.stream.getvalue(), "")
            self.assertEqual(previous_handler.stream.getvalue(), "")
        finally:
            logger.level, logger.handlers, logger.filters, logger.propagate, logger.disabled = original
            root.removeHandler(root_probe)
            root_probe.close()
            previous_handler.close()
        self.assertEqual(root.handlers, root_handlers)

    def test_nested_scopes_restore_outer_handlers(self):
        with self.scope() as outer:
            handlers = outer.handlers[:]
            self.emit(outer, "Outer before")
            with self.scope(mode="backtest") as inner:
                self.emit(inner, "Inner")
            self.assertEqual(outer.handlers, handlers)
            self.emit(outer, "Outer after")
        records = self.records()
        self.assertEqual([r["mode"] for r in records], ["paper", "backtest", "paper"])
        self.assertEqual(records[0]["run_id"], records[2]["run_id"])

    def test_nonfinite_numbers_and_unsupported_objects_are_valid_json(self):
        recursive = {}
        recursive["self"] = recursive
        details = {"values": [float("nan"), float("inf"), -float("inf"), 2.5],
                   "nested": {"number": float("nan")}, "object": object(), "cycle": recursive}
        with self.scope() as logger:
            self.emit(logger, details=details)
        record, = self.records()
        self.assertEqual(record["details"], {"values": [None, None, None, 2.5],
                                             "nested": {"number": None}, "object": None,
                                             "cycle": {"self": None}})
        text = (self.directory / "bijisatu" / "2026-03-16.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertTrue(text.endswith("\n"))
        self.assertTrue(math.isnan(details["values"][0]))

    def test_sensitive_details_are_redacted_and_arbitrary_extras_not_attached(self):
        with self.scope() as logger:
            self.emit(logger, details={"account_login": 1234567, "nested": {"password": "hidden-password",
                      "access_token": "hidden-token", "config": {"other": "hidden-config"}}, "spread": 0.1},
                      account_id="hidden-extra")
        record, = self.records()
        self.assertEqual(record["details"]["account_login"], "[redacted]")
        self.assertEqual(record["details"]["nested"], dict.fromkeys(("password", "access_token", "config"), "[redacted]"))
        self.assertEqual(record["details"]["spread"], 0.1)
        combined = json.dumps(record) + self.console.getvalue()
        for secret in ("1234567", "hidden-password", "hidden-token", "hidden-config", "hidden-extra"):
            self.assertNotIn(secret, combined)

    def test_exception_locals_and_non_mapping_details_are_not_attached(self):
        with self.scope() as logger:
            try:
                private_value = "private-local-value"
                raise ValueError("sensitive-exception-text")
            except ValueError:
                with patch("logging.time.time", return_value=START.timestamp()):
                    logger.exception("Operation failed", extra={"details": private_value})
        record, = self.records()
        self.assertNotIn("details", record)
        self.assertNotIn("private-local-value", json.dumps(record) + self.console.getvalue())
        self.assertNotIn("sensitive-exception-text", json.dumps(record) + self.console.getvalue())

    def test_unwritable_root_fails_before_yield_and_preserves_logger(self):
        logger = logging.getLogger("bijisatu")
        previous = (logger.level, logger.handlers[:], logger.filters[:], logger.propagate, logger.disabled)
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("root is read-only")):
            with self.assertRaisesRegex(OSError, "read-only"):
                with self.scope():
                    self.fail("Logging scope must not start without a writable log root")
        self.assertEqual((logger.level, logger.handlers, logger.filters, logger.propagate, logger.disabled), previous)

    def test_file_open_failure_and_invalid_directory_propagate(self):
        with patch("pathlib.Path.open", side_effect=PermissionError("cannot append")):
            with self.assertRaisesRegex(OSError, "cannot append"):
                with self.scope():
                    self.fail("Log initialization must fail")
        blocked = self.directory / "not-a-directory"
        blocked.write_text("existing data", encoding="utf-8")
        with self.assertRaises(OSError):
            with configured_logging(blocked, verbose=False, mode="paper", broker_utc_offset_hours=None):
                self.fail("A file cannot be used as the log root")
        self.assertEqual(blocked.read_text(encoding="utf-8"), "existing data")

    def test_write_and_flush_failures_propagate_even_with_logging_errors_disabled(self):
        for operation in ("write", "flush"):
            with self.subTest(operation=operation), self.scope() as logger:
                handler = logger.handlers[0]
                stream = handler.stream
                wrapped = Mock(wraps=stream)
                getattr(wrapped, operation).side_effect = OSError("disk full")
                handler.stream = wrapped
                try:
                    with patch("logging.raiseExceptions", False), self.assertRaisesRegex(OSError, "disk full"):
                        self.emit(logger)
                finally:
                    handler.stream = stream

    def test_rollover_failure_propagates_and_keeps_previous_records(self):
        with self.scope() as logger:
            self.emit(logger, "Before failure")
            with patch("pathlib.Path.open", side_effect=OSError("rollover failed")):
                with self.assertRaisesRegex(OSError, "rollover failed"):
                    self.emit(logger, at=START + timedelta(days=1))
        self.assertEqual([r["message"] for r in self.records()], ["Before failure"])

    def test_cleanup_restores_logger_even_when_file_close_fails(self):
        logger = logging.getLogger("bijisatu")
        original = logger.handlers[:]
        with self.assertRaisesRegex(OSError, "close failed"):
            with self.scope() as configured:
                handler, console = configured.handlers
                stream = handler.stream
                wrapped = Mock(wraps=stream)
                wrapped.close.side_effect = OSError("close failed")
                handler.stream = wrapped
        try:
            self.assertEqual(logger.handlers, original)
            self.assertTrue(console._closed)
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()

