"""Scoped, append-only logging; callers must not log credentials or config dumps.

Session labels are indicative weekday windows, not holiday/broker calendars or
trading filters. Scopes configure the process-wide robot logger, not the root.
"""

import json
import logging
import math
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TextIO
from uuid import uuid4
from zoneinfo import ZoneInfo


_SESSION_WINDOWS = (
    ("Sydney", "Australia/Sydney", 8, 17),
    ("Tokyo", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
)
_MONEY_FIELDS = {
    "equity", "balance", "daily_baseline", "daily_loss", "daily_loss_limit",
    "reserved_open_risk", "remaining_risk", "open_risk_limit",
    "remaining_daily_risk", "remaining_open_risk", "risk_amount", "estimated_loss",
    "worst_case_loss", "margin", "free_margin",
}
_ACCOUNT_FIELDS = _MONEY_FIELDS | {"currency", "open_positions"}
_DETAIL_LABELS = {
    "sl": "SL", "tp": "TP", "symbols": "Symbols scanned",
    "skipped": "Skipped reasons",
}
_SENSITIVE_KEYS = {
    "account", "accountid", "accountids", "accountlogin", "accountnumber",
    "login", "password", "passwd", "pwd", "token", "accesstoken",
    "refreshtoken", "authorization", "secret", "clientsecret", "apikey",
    "config", "configuration", "configdump", "credentials",
}


def market_sessions(at: datetime) -> list[str]:
    """Return approximate active local-weekday sessions, with IANA DST rules."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Session timestamps must be timezone-aware")
    sessions = []
    for name, zone, start, end in _SESSION_WINDOWS:
        local = at.astimezone(ZoneInfo(zone))
        if local.weekday() < 5 and start <= local.hour < end:
            sessions.append(name)
    return sessions


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(value: object, ancestors: frozenset[int] = frozenset()) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if id(value) in ancestors:
        return None
    ancestors = ancestors | {id(value)}
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            canonical = "".join(character for character in key.lower() if character.isalnum())
            if canonical == "account" and isinstance(item, Mapping):
                # Account snapshots expose financial metrics, never identifiers.
                normalized[key] = _normalize(
                    {name: metric for name, metric in item.items() if name in _ACCOUNT_FIELDS}, ancestors
                )
            else:
                normalized[key] = (
                    "[redacted]" if canonical in _SENSITIVE_KEYS
                    else _normalize(item, ancestors)
                )
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item, ancestors) for item in value]
    # Do not serialize arbitrary object attributes or reprs containing secrets.
    return None


class _StructuredFormatter(logging.Formatter):
    def __init__(self, mode: str, run_id: str, offset: float | None):
        super().__init__()
        self.mode = mode
        self.run_id = run_id
        self.offset = offset
        self.broker_zone = None if offset is None else timezone(timedelta(hours=offset))

    def payload(self, record: logging.LogRecord) -> dict:
        at = datetime.fromtimestamp(record.created, timezone.utc)
        event = getattr(record, "event", "message")
        payload = {
            "timestamp_utc": at.isoformat(),
            "level": record.levelname,
            "event": event if isinstance(event, str) else "message",
            "message": record.getMessage(),
            "robot": "BijiSatu",
            "mode": self.mode,
            "run_id": self.run_id,
            "pid": os.getpid(),
            "active_sessions": market_sessions(at),
            "broker_time": None if self.broker_zone is None else at.astimezone(self.broker_zone).isoformat(),
            "broker_utc_offset_hours": self.offset,
        }
        details = getattr(record, "details", None)
        if isinstance(details, Mapping):
            payload["details"] = _normalize(details)
        return payload

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.payload(record), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class _ConsoleFormatter(_StructuredFormatter):
    """Render labelled text without changing structured file values."""

    @staticmethod
    def _value(value: object, key: str, currency: str | None) -> str:
        if value is None:
            return "Unavailable"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (int, float)) and key in _MONEY_FIELDS:
            amount = f"{value:,.2f}"
            return f"{amount} {currency}" if currency else amount
        if isinstance(value, float):
            return format(Decimal(str(value)), "f")
        return str(value)

    def _lines(self, key: str, value: object, indent: int, currency: str | None) -> list[str]:
        label = _DETAIL_LABELS.get(key, key.replace("_", " ").replace("-", " ").capitalize())
        prefix = " " * indent + label + ":"
        if isinstance(value, Mapping):
            if not value:
                return [prefix + " None"]
            local_currency = value.get("currency")
            if isinstance(local_currency, str):
                currency = local_currency
            lines = [prefix]
            for name, item in value.items():
                lines.extend(self._lines(name, item, indent + 2, currency))
            return lines
        if isinstance(value, list):
            if not value:
                return [prefix + " None"]
            if all(not isinstance(item, (Mapping, list)) for item in value):
                return [prefix + " " + ", ".join(self._value(item, key, currency) for item in value)]
            lines = [prefix]
            for index, item in enumerate(value, 1):
                lines.extend(self._lines(f"Item {index}", item, indent + 2, currency))
            return lines
        return [prefix + " " + self._value(value, key, currency)]

    def format(self, record: logging.LogRecord) -> str:
        payload = self.payload(record)
        sessions = " + ".join(payload["active_sessions"]) or "No active sessions"
        timestamp = payload["timestamp_utc"].replace("T", " ").removesuffix("+00:00")
        lines = [
            f'{timestamp} UTC | {payload["level"]} | Mode: {payload["mode"]} | '
            f'Session: {sessions} | {payload["message"]}'
        ]
        details = payload.get("details", {})
        account = details.get("account", {})
        currency = account.get("currency") if isinstance(account, Mapping) else None
        if not isinstance(currency, str):
            currency = details.get("currency")
        if not isinstance(currency, str):
            currency = None
        for key, value in details.items():
            lines.extend(self._lines(key, value, 2, currency))
        return "\n".join(lines)


class _DailyJsonLinesHandler(logging.Handler):
    """Open eagerly and propagate disk errors instead of logging.handleError."""

    def __init__(self, directory: Path):
        super().__init__(logging.DEBUG)
        self.directory = directory / "bijisatu"
        self.stream: TextIO | None = None
        self.day = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._rollover(_utc_now().date())
        except BaseException:
            self.close()
            raise

    def _rollover(self, day) -> None:
        if self.stream is not None and self.day == day:
            return
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.stream = (self.directory / f"{day.isoformat()}.jsonl").open(
            "a", encoding="utf-8", newline="\n"
        )
        self.day = day

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self._rollover(datetime.fromtimestamp(record.created, timezone.utc).date())
        self.stream.write(line + "\n")
        self.stream.flush()

    def flush(self) -> None:
        self.acquire()
        try:
            if self.stream is not None:
                self.stream.flush()
        finally:
            self.release()

    def close(self) -> None:
        self.acquire()
        try:
            stream, self.stream = self.stream, None
            if stream is not None:
                stream.close()
        finally:
            super().close()
            self.release()


@contextmanager
def configured_logging(
    log_dir: Path, *, verbose: bool, mode: str, broker_utc_offset_hours: float | None
) -> Iterator[logging.Logger]:
    """Install isolated robot handlers and restore prior configuration on exit.

    Daily UTC files are always DEBUG, append-only, flushed per line and never
    deleted. Disk errors propagate, including during setup. Details stay nested;
    common credential/config keys are redacted and unknown objects omitted as
    null. Account mappings expose only financial metrics. Console details use
    readable labels and currency amounts rounded to two decimal places; file
    values retain their original precision. Free-form messages must not contain
    secrets. Exception locals and
    arbitrary LogRecord extras are never attached automatically. Do not overlap
    scopes across threads; the logger configuration is process-wide.
    """
    run_id = str(uuid4())
    formatter = _StructuredFormatter(mode, run_id, broker_utc_offset_hours)
    file_handler = _DailyJsonLinesHandler(log_dir)
    console_handler = logging.StreamHandler()
    logger = logging.getLogger("bijisatu")
    previous = (logger.level, logger.handlers[:], logger.filters[:], logger.propagate, logger.disabled)
    try:
        file_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        console_handler.setFormatter(_ConsoleFormatter(mode, run_id, broker_utc_offset_hours))
        logger.handlers = [file_handler, console_handler]
        logger.filters = []
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.disabled = False
        yield logger
    finally:
        level, handlers, filters, propagate, disabled = previous
        logger.handlers = handlers
        logger.filters = filters
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
        try:
            file_handler.close()
        finally:
            console_handler.close()

