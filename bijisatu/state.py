import json
import math
import os
from contextlib import contextmanager
from pathlib import Path


class StateError(RuntimeError):
    pass


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StateError("Another BijiSatu instance holds the account lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = None
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self._validate()
            except (ValueError, TypeError, KeyError) as exc:
                raise StateError("Invalid state file; refusing to reset risk history") from exc

    def _validate(self) -> None:
        data = self.data
        if not isinstance(data, dict) or data["version"] != 1 or not isinstance(data["day"], str):
            raise ValueError("Unsupported state")
        for key in ("baseline", "baseline_at", "last_seen"):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid state value")
        if type(data["halted"]) is not bool or not isinstance(data["reason"], str) or not isinstance(data["attempts"], dict):
            raise ValueError("Invalid state fields")
        for symbol, attempt in data["attempts"].items():
            if not isinstance(symbol, str) or not isinstance(attempt, dict):
                raise ValueError("Invalid entry attempt")
            for key in ("bar_time", "at"):
                value = attempt[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("Invalid entry timestamp")

    def save(self) -> None:
        self._validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def start_day(self, day: str, equity: float, now: float) -> None:
        attempts = self.data["attempts"] if self.data else {}
        self.data = {"version": 1, "day": day, "baseline": equity, "baseline_at": now,
                     "last_seen": now, "halted": False, "reason": "", "attempts": attempts}
        self.save()

    def halt(self, reason: str) -> None:
        self.data["halted"] = True
        self.data["reason"] = reason
        self.save()

    def claim(self, symbol: str, bar_time: int, now: float) -> None:
        self.data["attempts"][symbol] = {"bar_time": bar_time, "at": now}
        self.save()

