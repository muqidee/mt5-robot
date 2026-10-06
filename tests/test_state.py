import copy
import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from bijisatu.state import StateError, StateStore, exclusive_lock


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / (".state-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "account.json"
        self.store = StateStore(self.path)
        self.now = 1791244800

    def start(self):
        self.store.start_day("2026-10-06", 10000, self.now)

    def test_absent_state_is_not_an_implicit_baseline(self):
        self.assertIsNone(self.store.data)
        self.assertFalse(self.path.exists())

    def test_day_claim_and_halt_survive_independent_reload(self):
        self.start()
        self.store.claim("EURUSD", self.now - 60, self.now + 1)
        self.store.halt("Uncertain acknowledgement")
        loaded = StateStore(self.path).data
        self.assertEqual(loaded, self.store.data)
        self.assertEqual(loaded["baseline"], 10000)
        self.assertTrue(loaded["halted"])
        self.assertEqual(loaded["reason"], "Uncertain acknowledgement")
        self.assertEqual(loaded["attempts"]["EURUSD"], {"bar_time": self.now - 60, "at": self.now + 1})

    def test_new_day_resets_latch_but_preserves_dedup_and_cooldown(self):
        self.start()
        self.store.claim("EURUSD", self.now - 60, self.now)
        self.store.halt("Daily limit")
        self.store.start_day("2026-10-07", 9500, self.now + 86400)
        loaded = StateStore(self.path).data
        self.assertEqual(loaded["baseline"], 9500)
        self.assertEqual(loaded["baseline_at"], self.now + 86400)
        self.assertEqual(loaded["last_seen"], self.now + 86400)
        self.assertFalse(loaded["halted"])
        self.assertEqual(loaded["reason"], "")
        self.assertIn("EURUSD", loaded["attempts"])

    def test_corrupt_state_fails_closed_without_overwriting_history(self):
        self.start()
        valid = copy.deepcopy(self.store.data)
        invalid = [None, [], {}, dict(valid, version=2), dict(valid, halted=1),
                   dict(valid, attempts=[]), dict(valid, reason=None)]
        for key in ("baseline", "baseline_at", "last_seen"):
            for value in (0, -1, True, "100", None, float("nan"), float("inf")):
                invalid.append(dict(valid, **{key: value}))
        invalid.extend(dict(valid, attempts={"EURUSD": attempt}) for attempt in (
            {}, None, {"bar_time": 0, "at": self.now},
            {"bar_time": self.now, "at": True}, {"bar_time": self.now, "at": float("nan")},
        ))
        for data in invalid:
            with self.subTest(data=data):
                original = json.dumps(data)
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(StateError):
                    StateStore(self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateError):
            StateStore(self.path)

    def test_failed_atomic_replace_keeps_last_durable_state(self):
        self.start()
        before = self.path.read_bytes()
        with patch("bijisatu.state.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.claim("EURUSD", self.now - 60, self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(StateStore(self.path).data["attempts"], {})
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_fsync_failure_keeps_last_durable_state(self):
        self.start()
        before = self.path.read_bytes()
        with patch("bijisatu.state.os.fsync", side_effect=OSError("flush failed")):
            with self.assertRaises(OSError):
                self.store.halt("uncertain")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_lock_excludes_same_account_and_releases_after_error(self):
        lock = self.directory / "account.lock"
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with exclusive_lock(lock):
                with self.assertRaises(StateError):
                    with exclusive_lock(lock):
                        self.fail("The same account lock was acquired twice")
                with exclusive_lock(self.directory / "other-account.lock"):
                    pass
                raise RuntimeError("body failed")
        with exclusive_lock(lock):
            pass


if __name__ == "__main__":
    unittest.main()

