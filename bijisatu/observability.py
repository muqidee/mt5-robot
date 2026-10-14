"""Scoped, append-only logging; callers must not log credentials or config dumps.

Session labels are indicative weekday windows, not holiday/broker calendars or
trading filters. Scopes configure the process-wide robot logger, not the root.
"""

import json
import logging
import math
import os
import textwrap
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

    def __init__(self, mode, run_id, offset, verbose=False, show_request_id=False):
        super().__init__(mode, run_id, offset)
        self.verbose = verbose
        self.show_request_id = show_request_id

    @staticmethod
    def _block(lines: list[str]) -> str:
        return "\n".join(
            textwrap.fill(line, width=96, subsequent_indent="    ")
            for line in lines
        )

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
        event = payload["event"]
        if not self.verbose and event in ("runtime_status", "heartbeat"):
            skips = "; ".join(
                f"{name.replace('_', ' ').capitalize()}: {count}"
                for name, count in details.get("skipped", {}).items()
            ) or "none"
            equity = self._value(account.get("equity"), "equity", currency)
            remaining = self._value(account.get("remaining_risk", details.get("remaining")), "remaining_risk", currency)
            short_time = payload["timestamp_utc"][:19].replace("T", " ")
            summary = [
                f'{short_time} UTC | {payload["level"]} | {payload["mode"]} | '
                f'{payload["message"]}: {details.get("status")} | Cycle: {details.get("cycle")}',
                f"  Equity: {equity} | Open positions: {account.get('open_positions', 0)} | Remaining risk: {remaining}",
                f"  Skipped: {skips}",
            ]
            if details.get("reason"):
                summary.append(f"  Reason: {details['reason']}")
            return self._block(summary)
        if not self.verbose and (event.startswith("ollama_") or event.startswith("market_commentary_")):
            short_time = payload["timestamp_utc"][:19].replace("T", " ")
            symbol = f" | {details['symbol']}" if details.get("symbol") else ""
            summary = [f'{short_time} UTC | {payload["level"]}{symbol} | {payload["message"]}']
            context = []
            if details.get("model"):
                context.append(f"Model: {details['model']}")
            if details.get("entry_timeframe") and details.get("trend_timeframe"):
                context.append(f"Timeframes: {details['entry_timeframe']}/{details['trend_timeframe']}")
            if "latency_seconds" in details:
                context.append(f"Latency: {self._value(details['latency_seconds'], 'latency_seconds', None)} s")
            if context:
                summary.append("  " + " | ".join(context))
            for key in ("decision", "bias"):
                if key in details:
                    summary.append(f"  {key.capitalize()}: {str(details[key]).upper()}")
            if "reason" in details:
                summary.append(f"  Reason: {details['reason']}")
            if self.show_request_id and details.get("request_id"):
                summary.append(f"  Request id: {details['request_id']}")
            return self._block(summary)
        if not self.verbose and event == "setup_assessment":
            failures = "; ".join(side + ": " + ", ".join(checks)
                                 for side, checks in details.get("failed_conditions", {}).items())
            trend = details.get("trend_latest") or {}
            entry = details.get("entry_latest") or {}
            return (lines[0] + f" | {details.get('symbol')} | {details.get('trend_timeframe')}/{details.get('entry_timeframe')}"
                    f" | {details.get('reason')} | Failed: {failures or 'none'}"
                    + (f" | Trend EMA20/50: {trend['ema20']:.6g}/{trend['ema50']:.6g}; close={trend['close']:.6g}" if trend else "")
                    + (f" | Entry open/close/EMA20: {entry['open']:.6g}/{entry['close']:.6g}/{entry['ema20']:.6g}" if entry else ""))
        for key, value in details.items():
            if key == "request_id" and not self.show_request_id:
                continue
            lines.extend(self._lines(key, value, 2, currency))
        return "\n".join(lines)


class _DailyJsonLinesHandler(logging.Handler):
    """Open eagerly and propagate disk errors instead of logging.handleError."""

    def __init__(self, directory: Path, suffix: str = ".jsonl"):
        super().__init__(logging.DEBUG)
        self.suffix = suffix
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
        self.stream = (self.directory / f"{day.isoformat()}{self.suffix}").open(
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

    Daily UTC JSONL and readable .log files default to INFO (DEBUG with verbose),
    append-only, flushed per
    event and never deleted. Readable files use the console formatter. Disk
    errors propagate, including during setup. Details stay nested;
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
    text_handler = None
    logger = logging.getLogger("bijisatu")
    previous = (logger.level, logger.handlers[:], logger.filters[:], logger.propagate, logger.disabled)
    try:
        text_handler = _DailyJsonLinesHandler(log_dir, suffix=".log")
        text_handler.setFormatter(_ConsoleFormatter(
            mode, run_id, broker_utc_offset_hours, verbose=verbose, show_request_id=True
        ))
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        text_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        console_handler.setFormatter(_ConsoleFormatter(mode, run_id, broker_utc_offset_hours, verbose=verbose))
        logger.handlers = [file_handler, text_handler, console_handler]
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
            try:
                if text_handler is not None:
                    text_handler.close()
            finally:
                console_handler.close()

