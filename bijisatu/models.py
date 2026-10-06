from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    side: Side
    bar_time: int
    atr: float
    reason: str


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    point: float
    digits: int
    tick_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int


@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float
    time: int


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_current: float
    sl: float
    magic: int


@dataclass(frozen=True)
class Account:
    login: int
    server: str
    currency: str
    equity: float
    balance: float
    is_demo: bool
    trade_allowed: bool


@dataclass(frozen=True)
class OrderPlan:
    symbol: str
    side: Side
    volume: float
    entry: float
    sl: float
    tp: float
    risk_amount: float
    bar_time: int

