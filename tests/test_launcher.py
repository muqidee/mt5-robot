import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import run_bijisatu


class LauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.python = self.root / ".venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.config = self.root / "config.local.json"
        self.config.write_text('{"symbols": ["EURUSDc"]}\n', encoding="utf-8")
        (self.root / "config.example.json").write_text('{"symbols": []}\n', encoding="utf-8")
        root = patch.object(run_bijisatu, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        runner = patch.object(run_bijisatu.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        stdout = redirect_stdout(io.StringIO())
        stderr = redirect_stderr(io.StringIO())
        self.stdout = stdout.__enter__()
        self.stderr = stderr.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)
        self.addCleanup(stderr.__exit__, None, None, None)

    def test_default_is_continuous_observation_and_clears_child_execution_permission(self):
        with patch.dict(os.environ, {"BIJISATU_ALLOW_ORDERS": "YES"}):
            self.assertEqual(run_bijisatu.main([]), 0)
            self.assertEqual(os.environ["BIJISATU_ALLOW_ORDERS"], "YES")
        command = self.runner.call_args.args[0]
        self.assertEqual(command, [str(self.python), "-m", "bijisatu", "run", "--config", str(self.config)])
        self.assertNotIn("BIJISATU_ALLOW_ORDERS", self.runner.call_args.kwargs["env"])
        self.assertEqual(self.runner.call_args.kwargs["cwd"], self.root)
        self.assertFalse(self.runner.call_args.kwargs["check"])

    def test_live_requires_numbered_confirmation_and_only_changes_child_environment(self):
        with patch.dict(os.environ, {"BIJISATU_ALLOW_ORDERS": "NO"}), patch("builtins.input", return_value="1"):
            self.assertEqual(run_bijisatu.main(["--execute", "--once", "--verbose"]), 0)
            self.assertEqual(os.environ["BIJISATU_ALLOW_ORDERS"], "NO")
        command = self.runner.call_args.args[0]
        for flag in ("--execute", "--once", "--verbose"):
            self.assertIn(flag, command)
        self.assertEqual(self.runner.call_args.kwargs["env"]["BIJISATU_ALLOW_ORDERS"], "YES")

    def test_numbered_menu_is_displayed_and_accepts_surrounding_whitespace(self):
        with patch("builtins.input", return_value=" 1 ") as prompt:
            self.assertEqual(run_bijisatu.main(["--execute"]), 0)
        self.assertIn("1. Enable order submission", self.stdout.getvalue())
        self.assertIn("2. Cancel (default)", self.stdout.getvalue())
        prompt.assert_called_once_with("Select an option [1/2, default 2]: ")
        self.runner.assert_called_once()

    def test_interrupted_confirmation_never_starts_child(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(run_bijisatu.main(["--execute"]), 130)
        self.runner.assert_not_called()

    def test_cancelled_confirmation_never_starts_child(self):
        for answer in ("", "2", "0", "3", "yes", "trade", "TRADE", "NO"):
            with self.subTest(answer=answer), patch("builtins.input", return_value=answer):
                self.assertEqual(run_bijisatu.main(["--execute"]), 0)
                self.runner.assert_not_called()

    def test_missing_confirmation_never_starts_child(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(run_bijisatu.main(["--execute"]), 2)
        self.runner.assert_not_called()

    def test_existing_configuration_is_not_overwritten(self):
        original = self.config.read_bytes()
        self.assertEqual(run_bijisatu.main([]), 0)
        self.assertEqual(self.config.read_bytes(), original)

    def test_missing_default_configuration_is_copied_from_example(self):
        self.config.unlink()
        self.assertEqual(run_bijisatu.main([]), 0)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"symbols": []})

    def test_relative_custom_path_is_resolved_from_project_not_current_directory(self):
        custom = self.root / "settings" / "custom.json"
        custom.parent.mkdir()
        custom.write_text("{}", encoding="utf-8")
        self.assertEqual(run_bijisatu.main(["--config", str(Path("settings") / "custom.json")]), 0)
        self.assertIn(str(custom), self.runner.call_args.args[0])

    def test_missing_custom_path_is_not_created(self):
        self.assertEqual(run_bijisatu.main(["--config", "missing.json"]), 2)
        self.assertFalse((self.root / "missing.json").exists())
        self.runner.assert_not_called()

    def test_missing_virtual_environment_does_not_start_child(self):
        self.python.unlink()
        self.assertEqual(run_bijisatu.main([]), 2)
        self.runner.assert_not_called()

    def test_child_exit_code_is_preserved(self):
        self.runner.return_value = subprocess.CompletedProcess([], 2)
        self.assertEqual(run_bijisatu.main([]), 2)

    def test_launch_failure_and_keyboard_interrupt_are_reported(self):
        for error, expected in ((OSError("Cannot start Python"), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error).__name__):
                self.runner.side_effect = error
                self.assertEqual(run_bijisatu.main([]), expected)


if __name__ == "__main__":
    unittest.main()

