import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    risk_fraction: float = 0.03
    daily_loss_fraction: float = 0.05
    max_positions: int = 2
    max_symbols: int = 20
    poll_seconds: int = 5
    cooldown_seconds: int = 300
    max_tick_age_seconds: int = 15
    max_spread_atr: float = 0.15
    stop_atr: float = 1.5
    reward_ratio: float = 1.5
    risk_buffer: float = 1.15
    max_margin_fraction: float = 0.8
    deviation_points: int = 10
    magic: int = 810031
    symbols: tuple[str, ...] = ()
    account_login: int | None = None
    broker_utc_offset_hours: float | None = None
    commission_per_lot: float | None = None
    terminal_path: str | None = None
    state_dir: str = "state"
    log_dir: str = "logs"
    heartbeat_seconds: int = 60

    def validate(self, execute: bool = False) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        for name in ("risk_fraction", "daily_loss_fraction", "max_spread_atr", "max_margin_fraction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("max_positions", "max_symbols", "poll_seconds", "cooldown_seconds", "max_tick_age_seconds", "magic", "heartbeat_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.deviation_points) is not int or self.deviation_points < 0:
            raise ValueError("deviation_points must be a nonnegative integer")
        for name in ("stop_atr", "reward_ratio", "risk_buffer"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.risk_buffer < 1:
            raise ValueError("risk_buffer cannot be below 1")
        if self.risk_fraction > self.daily_loss_fraction:
            raise ValueError("risk_fraction cannot exceed daily_loss_fraction")
        if not isinstance(self.symbols, (list, tuple)) or any(not isinstance(s, str) or not s.strip() for s in self.symbols):
            raise ValueError("symbols must contain nonempty strings")
        if self.account_login is not None and (type(self.account_login) is not int or self.account_login <= 0):
            raise ValueError("account_login must be a positive integer")
        if self.broker_utc_offset_hours is not None:
            value = self.broker_utc_offset_hours
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not -14 <= value <= 14:
                raise ValueError("broker_utc_offset_hours must be between -14 and 14")
        if self.commission_per_lot is not None:
            value = self.commission_per_lot
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError("commission_per_lot must be nonnegative")
        for name in ("state_dir", "log_dir"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        if self.terminal_path is not None and not isinstance(self.terminal_path, str):
            raise ValueError("terminal_path must be a string")
        if execute and any(value is None for value in (self.account_login, self.broker_utc_offset_hours, self.commission_per_lot)):
            raise ValueError("Execution requires account_login, broker_utc_offset_hours and commission_per_lot (account currency, round trip)")


def load_config(path: Path | None) -> Config:
    data = {} if path is None else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object")
    unknown = data.keys() - {field.name for field in fields(Config)}
    if unknown:
        raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")
    config = Config(**data)
    config.validate()
    return config

