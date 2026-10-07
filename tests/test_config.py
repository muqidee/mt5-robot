import json
import shutil
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from bijisatu.config import Config, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / (".config-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)

    def load(self, data):
        path = self.directory / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_config(path)

    def test_defaults_are_three_percent_risk_and_five_percent_daily(self):
        config = load_config(None)
        self.assertEqual(config.risk_fraction, 0.03)
        self.assertEqual(config.daily_loss_fraction, 0.05)
        self.assertEqual(config.max_open_risk_fraction, 0.05)
        config.validate()
        with self.assertRaises(ValueError):
            config.validate(execute=True)

    def test_execution_requires_all_three_explicit_settings(self):
        settings = dict(account_login=123, broker_utc_offset_hours=0, commission_per_lot=0)
        Config(**settings).validate(execute=True)
        for missing in settings:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                Config(**{key: value for key, value in settings.items() if key != missing}).validate(execute=True)

    def test_json_settings_and_zero_cost_are_preserved(self):
        config = self.load(dict(account_login=123, broker_utc_offset_hours=-3.5,
                                commission_per_lot=0, symbols=["EURUSD", "XAUUSD"]))
        config.validate(execute=True)
        self.assertEqual(config.symbols, ["EURUSD", "XAUUSD"])
        self.assertEqual(config.broker_utc_offset_hours, -3.5)
        self.assertEqual(config.commission_per_lot, 0)

    def test_rejects_unknown_fields_and_non_object_json(self):
        for data in ({"risk_percent": 3}, [], None, 3, "settings"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)

    def test_rejects_malformed_json(self):
        path = self.directory / "broken.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(path)

    def test_invalid_configuration_values(self):
        cases = {
            "risk_fraction": [0, -0.1, 1, True, "0.03", 0.06],
            "daily_loss_fraction": [0, 1, True, "0.05"],
            "max_spread_atr": [0, 1, True],
            "max_margin_fraction": [0, 1, True],
            "max_positions": [0, -1, 1.5, True, 11],
            "max_open_risk_fraction": [0, -1, 1, True, "0.05", None],
            "max_symbols": [0, True],
            "poll_seconds": [0, True],
            "cooldown_seconds": [0, True],
            "max_tick_age_seconds": [0, True],
            "magic": [0, True],
            "deviation_points": [-1, 0.5, True],
            "stop_atr": [0, -1, True],
            "reward_ratio": [0, -1, True],
            "risk_buffer": [0.99, True],
            "account_login": [0, -1, True, "123"],
            "broker_utc_offset_hours": [-14.1, 14.1, True, "0"],
            "commission_per_lot": [-1, True, "0"],
            "symbols": ["EURUSD", [""], [" "], [1]],
            "state_dir": ["", " ", None],
            "log_dir": ["", " ", None, 1],
            "heartbeat_seconds": [0, -1, True, 1.5, "60"],
            "terminal_path": [1],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(Config(), **{field: value}).validate()

    def test_confirmed_portfolio_limits_are_valid(self):
        config = self.load(dict(risk_fraction=.01, daily_loss_fraction=.05,
                                max_positions=10, max_open_risk_fraction=.05))
        config.validate()
        self.assertEqual(config.max_positions, 10)
        self.assertEqual(config.max_open_risk_fraction, .05)

    def test_nonfinite_numbers_are_rejected(self):
        for field in ("risk_fraction", "daily_loss_fraction", "stop_atr", "reward_ratio",
                      "risk_buffer", "max_spread_atr", "max_margin_fraction",
                      "broker_utc_offset_hours", "commission_per_lot", "max_open_risk_fraction"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(Config(), **{field: value}).validate()


if __name__ == "__main__":
    unittest.main()

