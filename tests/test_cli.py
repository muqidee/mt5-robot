import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from bijisatu.cli import main, parser, run
from bijisatu.config import Config


class CliTests(unittest.TestCase):
    def test_observer_owned_inside_logging_and_disabled_by_default(self):
        for enabled in (False, True):
            for fails in (False, True):
                with self.subTest(enabled=enabled, fails=fails):
                    events = []
                    @contextmanager
                    def logging_context(*args, **kwargs):
                        events.append("logging_open")
                        try:
                            yield Mock()
                        finally:
                            events.append("logging_closed")
                    def connected(*args, **kwargs):
                        self.assertEqual(kwargs["observer"] is not None, enabled)
                        if fails:
                            raise ValueError("controlled failure")
                        return 0
                    with patch("bijisatu.cli.load_config", return_value=replace(Config(), ollama_observation_enabled=enabled)), \
                            patch("bijisatu.cli.configured_logging", side_effect=logging_context), \
                            patch("bijisatu.cli.OllamaObserver") as observer, \
                            patch("bijisatu.cli.MT5Broker"), \
                            patch("bijisatu.cli._run_connected", side_effect=connected):
                        observer.return_value.close.side_effect = lambda: events.append("observer_closed")
                        args = parser().parse_args(["run", "--once"])
                        if fails:
                            with self.assertRaises(ValueError):
                                run(args)
                        else:
                            self.assertEqual(run(args), 0)
                        if enabled:
                            observer.assert_called_once()
                            observer.return_value.close.assert_called_once()
                            self.assertLess(events.index("logging_open"), events.index("observer_closed"))
                        else:
                            observer.assert_not_called()
                        if enabled:
                            self.assertLess(events.index("observer_closed"), events.index("logging_closed"))

    def test_observer_startup_failure_does_not_stop_runtime(self):
        with patch("bijisatu.cli.load_config", return_value=replace(Config(), ollama_observation_enabled=True)), \
                patch("bijisatu.cli.configured_logging") as logging_context, \
                patch("bijisatu.cli.OllamaObserver", side_effect=RuntimeError("controlled startup failure")), \
                patch("bijisatu.cli.MT5Broker"), \
                patch("bijisatu.cli._run_connected", return_value=0) as connected:
            self.assertEqual(run(parser().parse_args(["run", "--once"])), 0)
            self.assertIsNone(connected.call_args.kwargs["observer"])
            records = logging_context.return_value.__enter__.return_value.info.call_args_list
            self.assertTrue(any(item.kwargs.get("extra", {}).get("event") == "ollama_observation_unavailable" for item in records))

    def test_filter_lifecycle_and_startup_failure_keep_gate_enabled(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                config = replace(Config(), ollama_filter_enabled=True)
                with patch("bijisatu.cli.load_config", return_value=config), \
                        patch("bijisatu.cli.configured_logging"), \
                        patch("bijisatu.cli.OllamaEntryFilter") as worker, \
                        patch("bijisatu.cli.OllamaObserver") as observer, \
                        patch("bijisatu.cli.MT5Broker"), \
                        patch("bijisatu.cli._run_connected", return_value=0) as connected:
                    if fails:
                        worker.side_effect = RuntimeError("startup failed")
                    self.assertEqual(run(parser().parse_args(["run", "--once"])), 0)
                    self.assertTrue(connected.call_args.args[1].ollama_filter_enabled)
                    if fails:
                        self.assertIsNone(connected.call_args.kwargs["entry_filter"])
                    else:
                        self.assertIs(connected.call_args.kwargs["entry_filter"], worker.return_value)
                        worker.return_value.close.assert_called_once()
                    observer.assert_not_called()

    def test_run_defaults_to_no_execution(self):
        args = parser().parse_args(["run", "--once"])
        self.assertFalse(args.execute)
        self.assertTrue(args.once)

    def test_execution_requires_account_configuration_before_connection(self):
        with patch("bijisatu.cli.MT5Broker") as broker, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "--execute"]), 2)
            broker.assert_not_called()

    def test_execution_requires_environment_opt_in_before_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"account_login": 123, "broker_utc_offset_hours": 0,
                                        "commission_per_lot": 0}), encoding="utf-8")
            with patch.dict(os.environ, {"BIJISATU_ALLOW_ORDERS": "NO"}), patch("bijisatu.cli.MT5Broker") as broker:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["run", "--execute", "--config", str(path)]), 2)
                broker.assert_not_called()

    def test_bad_json_fails_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            with patch("bijisatu.cli.MT5Broker") as broker, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "--config", str(path)]), 2)
                broker.assert_not_called()

    def test_missing_csv_is_reported_without_connecting(self):
        with patch("bijisatu.cli.MT5Broker") as broker, redirect_stderr(io.StringIO()):
            result = main(["backtest", "missing-market-data.csv", "--spread", "0.01",
                           "--commission-per-lot", "0", "--value-per-price-unit", "100"])
            self.assertEqual(result, 2)
            broker.assert_not_called()

    def test_doctor_never_connects(self):
        with patch("bijisatu.cli.MT5Broker") as broker, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["doctor"]), 0)
            broker.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())["terminal_connected"])


if __name__ == "__main__":
    unittest.main()

