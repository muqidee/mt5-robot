import json
import re
import unittest
from dataclasses import asdict
from pathlib import Path

from bijisatu.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
        cls.properties = cls.schema["properties"]

    def test_every_runtime_setting_has_a_description_and_matching_default(self):
        expected = json.loads(json.dumps(asdict(Config())))
        self.assertEqual(set(self.properties), set(expected))
        for name, default in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.properties[name]["default"], default)
                self.assertGreater(len(self.properties[name]["description"]), 30)
                self.assertIn("type", self.properties[name])
        self.assertFalse(self.schema["additionalProperties"])

    def test_example_stays_valid_plain_json_with_runtime_defaults(self):
        example = ROOT / "config.example.json"
        data = json.loads(example.read_text(encoding="utf-8"))
        self.assertNotIn("$schema", data)
        self.assertEqual(data, json.loads(json.dumps(asdict(Config()))))
        self.assertEqual(asdict(load_config(example)), asdict(Config()) | {"symbols": []})

    def test_editor_associates_both_configs_with_the_local_schema(self):
        settings = json.loads((ROOT / ".vscode" / "settings.json").read_text(encoding="utf-8"))
        matches = [item for item in settings["json.schemas"] if item["url"] == "./config.schema.json"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(set(matches[0]["fileMatch"]), {"/config.example.json", "/config.local.json"})

    def test_reference_documents_every_key(self):
        guide = (ROOT / "docs" / "robots" / "bijisatu.md").read_text(encoding="utf-8")
        for name in self.properties:
            with self.subTest(name=name):
                self.assertIn(f"| `{name}` |", guide)

    def test_nullable_settings_and_nonempty_strings_match_runtime_intent(self):
        for name in ("account_login", "broker_utc_offset_hours", "commission_per_lot", "terminal_path"):
            with self.subTest(name=name):
                self.assertIn("null", self.properties[name]["type"])
                self.assertIsNone(self.properties[name]["default"])
        for entry in (self.properties["state_dir"], self.properties["log_dir"], self.properties["symbols"]["items"]):
            with self.subTest(entry=entry):
                self.assertIsNone(re.search(entry["pattern"], "   "))
                self.assertIsNotNone(re.search(entry["pattern"], "EURUSDc"))


if __name__ == "__main__":
    unittest.main()

