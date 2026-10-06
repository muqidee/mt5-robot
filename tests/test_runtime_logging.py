import io
import json
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from bijisatu.cli import _observe, main
from bijisatu.config import Config
from bijisatu.models import Account


class RuntimeLoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.logs = self.directory / "logs"
        self.config_path = self.directory / "config.json"
        self.config_path.write_text(json.dumps({"log_dir": str(self.logs),
                                                "state_dir": str(self.directory / "state")}), encoding="utf-8")
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.directory)})
        environment.start()
        self.addCleanup(environment.stop)
        self.result = {"status": "waiting", "symbols": 1, "remaining": 203.03250000000003,
                       "skipped": {"no_trend_pullback_setup": 1},
                       "account": {"equity": 4060.65, "balance": 4060.65, "currency": "USC",
                                   "daily_loss": 0, "daily_loss_limit": 203.0325,
                                   "remaining_risk": 203.0325, "reserved_open_risk": 0,
                                   "open_positions": 0}}

    def records(self):
        paths = list((self.logs / "bijisatu").glob("*.jsonl"))
        self.assertEqual(len(paths), 1)
        return [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]

    def test_once_writes_daily_log_with_sessions_without_sending_orders(self):
        with patch("bijisatu.cli.MT5Broker") as broker_type, patch("bijisatu.cli.Engine") as engine_type:
            broker = broker_type.return_value
            broker.account.return_value = Account(987654, "test-server", "USC", 4060.65, 4060.65, False, True)
            engine_type.return_value.symbols = ["EURUSDc"]
            engine_type.return_value.step.return_value = self.result
            with redirect_stderr(io.StringIO()) as console:
                result = main(["run", "--config", str(self.config_path), "--once"])
            self.assertEqual(result, 0)
            broker.send.assert_not_called()
            broker.close.assert_called_once()
            engine_type.assert_called_once()
            self.assertFalse(engine_type.call_args.args[3])
        records = self.records()
        events = {record["event"] for record in records}
        self.assertTrue({"logging_started", "account_connected", "runtime_status", "shutdown"} <= events)
        for record in records:
            self.assertIn("active_sessions", record)
            self.assertEqual(record["mode"], "dry-run")
            self.assertNotIn("account_login", record.get("details", {}))
        status = next(record for record in records if record["event"] == "runtime_status")
        self.assertEqual(status["details"]["remaining"], self.result["remaining"])
        self.assertNotIn('"account":', console.getvalue())
        self.assertNotIn('"status":', console.getvalue())
        for session in status["active_sessions"]:
            self.assertIn(session, console.getvalue())

    def test_connection_failure_is_logged_and_broker_is_closed(self):
        from bijisatu.broker import BrokerError
        with patch("bijisatu.cli.MT5Broker") as broker_type, redirect_stderr(io.StringIO()):
            broker = broker_type.return_value
            broker.connect.side_effect = BrokerError("Terminal disconnected")
            self.assertEqual(main(["run", "--config", str(self.config_path), "--once"]), 2)
            broker.send.assert_not_called()
            broker.close.assert_called_once()
        failure = next(record for record in self.records() if record["event"] == "runtime_error")
        self.assertEqual(failure["details"]["reason"], "Terminal disconnected")

    def test_unwritable_log_destination_prevents_terminal_connection(self):
        self.logs.write_text("not a directory", encoding="utf-8")
        with patch("bijisatu.cli.MT5Broker") as broker_type, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "--config", str(self.config_path), "--once"]), 2)
            broker_type.assert_not_called()

    def test_unchanged_status_emits_periodic_heartbeat(self):
        engine = Mock()
        engine.step.return_value = self.result
        logger = Mock(spec=logging.Logger)
        with patch("bijisatu.cli.time.monotonic", side_effect=[0, 5, 60]), \
                patch("bijisatu.cli.time.sleep", side_effect=[None, None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                _observe(engine, Config(), logger, once=False)
        self.assertEqual(engine.step.call_count, 3)
        self.assertEqual(logger.log.call_count, 2)
        first, heartbeat = logger.log.call_args_list
        self.assertEqual(first.kwargs["extra"]["event"], "runtime_status")
        self.assertEqual(heartbeat.kwargs["extra"]["event"], "heartbeat")
        self.assertEqual(heartbeat.kwargs["extra"]["details"]["cycle"], 3)

    def test_halted_status_is_a_warning(self):
        engine = Mock()
        engine.step.return_value = {"status": "halted", "reason": "Daily loss limit"}
        logger = Mock(spec=logging.Logger)
        self.assertEqual(_observe(engine, Config(), logger, once=True), 0)
        self.assertEqual(logger.log.call_args.args[0], logging.WARNING)


if __name__ == "__main__":
    unittest.main()

