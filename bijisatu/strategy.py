"""Closed-bar EMA pullback entries; callers supply completed entry candles only."""

import math
from numbers import Real

from .models import Bar, Signal


def _ordered(bars: list[Bar]) -> bool:
    return all(
        isinstance(bar.time, int)
        and not isinstance(bar.time, bool)
        and bar.time >= 0
        and (index == 0 or bar.time > bars[index - 1].time)
        for index, bar in enumerate(bars)
    )


def _valid(bars: list[Bar]) -> bool:
    for bar in bars:
        values = (bar.open, bar.high, bar.low, bar.close, bar.tick_volume)
        try:
            finite = all(
                isinstance(value, Real)
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in values
            )
        except (OverflowError, ValueError):
            return False
        if not finite:
            return False
        if not (
            0 < bar.low <= min(bar.open, bar.close)
            <= max(bar.open, bar.close) <= bar.high
            and bar.tick_volume >= 0
        ):
            return False
    return True


def _ema(values: list[float], period: int) -> list[float]:
    # Seed with an SMA, retaining indices so candle/EMA comparisons align.
    result = [math.nan] * (period - 1)
    result.append(sum(value / period for value in values[:period]))
    alpha = 2.0 / (period + 1)
    for value in values[period:]:
        result.append((1.0 - alpha) * result[-1] + alpha * value)
    return result


def _atr(bars: list[Bar], period: int = 14) -> float:
    ranges = [bars[0].high - bars[0].low]
    for previous, current in zip(bars, bars[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    value = sum(item / period for item in ranges[:period])
    for item in ranges[period:]:
        value = value * ((period - 1) / period) + item / period
    return value


def timeframes(strategy_mode: str) -> tuple[int, int]:
    """Return entry and trend durations in minutes for a supported mode."""
    if strategy_mode == "scalping":
        return 1, 5
    if strategy_mode == "intraday":
        return 15, 60
    raise ValueError("strategy_mode must be scalping or intraday")


def fresh_candle(bar: Bar, minutes: int, now: float, grace_seconds: int) -> bool:
    """Allow at most one timeframe plus feed/scan grace since candle close."""
    return 0 <= now - (bar.time + minutes * 60) <= minutes * 60 + grace_seconds


def evaluate(
    m1: list[Bar], m5: list[Bar], *, entry_minutes: int = 1, trend_minutes: int = 5,
) -> Signal | None:
    """Return a signal on the latest closed entry candle, or None.

    The legacy argument names remain compatible with M1/M5 callers. Bar times
    denote opens. Trend candles closing after the latest entry close are
    excluded, including from the 100-candle warmup. No wall clock is consulted:
    callers must exclude still-forming entry candles and reject stale feeds.

    Trend requires three consecutive closes above/below EMA20, EMA20 above/
    below EMA50, and both EMAs rising/falling on each candle. Entry requires
    the previous close at/beyond EMA20 against the trend, followed by a
    directional candle closing back across its own EMA20. ATR14 uses Wilder
    smoothing seeded by 14 true ranges (first candle uses its own range).
    Invalid data fail closed; excluded future trend OHLC/volume is not used.
    """
    if (type(entry_minutes) is not int or type(trend_minutes) is not int
            or entry_minutes <= 0 or trend_minutes <= entry_minutes
            or trend_minutes % entry_minutes):
        raise ValueError("Timeframes must be positive integer minutes with trend a larger entry multiple")
    if len(m1) < 100 or len(m5) < 100:
        return None
    if not _ordered(m1) or not _ordered(m5):
        return None
    cutoff = m1[-1].time + entry_minutes * 60
    closed_m5 = [bar for bar in m5 if bar.time + trend_minutes * 60 <= cutoff]
    if len(closed_m5) < 100 or not _valid(m1) or not _valid(closed_m5):
        return None

    fast = _ema([bar.close for bar in closed_m5], 20)
    slow = _ema([bar.close for bar in closed_m5], 50)
    entry_ema = _ema([bar.close for bar in m1], 20)
    atr = _atr(m1)
    if not math.isfinite(atr) or atr <= 0:
        return None

    previous, current = m1[-2:]
    for side, direction in (("buy", 1), ("sell", -1)):
        trending = all(
            direction * (fast[index] - slow[index]) > 0
            and direction * (closed_m5[index].close - fast[index]) > 0
            and direction * (fast[index] - fast[index - 1]) > 0
            and direction * (slow[index] - slow[index - 1]) > 0
            for index in range(len(closed_m5) - 3, len(closed_m5))
        )
        pulled_back = direction * (previous.close - entry_ema[-2]) <= 0
        reclaimed = direction * (current.close - entry_ema[-1]) > 0
        directional_candle = direction * (current.close - current.open) > 0
        if trending and pulled_back and reclaimed and directional_candle:
            trend_label = "H1" if trend_minutes == 60 else f"M{trend_minutes}"
            return Signal(
                side=side,
                bar_time=current.time,
                atr=atr,
                reason=f"{trend_label} EMA20/50 {side} trend; M{entry_minutes} EMA20 pullback/reclaim",
            )
    return None

