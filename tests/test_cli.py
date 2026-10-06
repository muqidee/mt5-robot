import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bijisatu.cli import main, parser


class CliTests(unittest.TestCase):
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

