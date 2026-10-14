"""Closed-bar EMA pullback entries; callers supply completed entry candles only."""

import math
from numbers import Real
from dataclasses import dataclass

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


STRATEGY_TIMEFRAMES = {"scalping": (1, 5), "intraday": (15, 60), "short_term": (60, 240)}
HISTORY_BARS = 100


def timeframe_label(minutes: int) -> str:
    return f"H{minutes // 60}" if minutes >= 60 and minutes % 60 == 0 else f"M{minutes}"


def timeframes(strategy_mode: str) -> tuple[int, int]:
    """Return entry and trend durations in minutes for a supported mode."""
    if not isinstance(strategy_mode, str) or strategy_mode not in STRATEGY_TIMEFRAMES:
        raise ValueError("strategy_mode must be scalping, intraday or short_term")
    return STRATEGY_TIMEFRAMES[strategy_mode]


def fresh_candle(bar: Bar, minutes: int, now: float, grace_seconds: int) -> bool:
    """Allow at most one timeframe plus feed/scan grace since candle close."""
    return 0 <= now - (bar.time + minutes * 60) <= minutes * 60 + grace_seconds


@dataclass(frozen=True)
class Assessment:
    signal: Signal | None
    diagnostics: dict


def assess(
    m1: list[Bar], m5: list[Bar], *, entry_minutes: int = 1, trend_minutes: int = 5,
) -> Assessment:
    """Assess closed entry bars and exclude trend closes beyond the entry cutoff.

    EMA20/50 alignment, slopes and three trend closes must agree. Entry requires
    a previous close at/beyond EMA20 followed by a directional reclaim candle.
    No wall clock is consulted: callers reject forming or stale entry candles.
    Diagnostics are conditions, not confidence or profitability estimates.
    """
    if (type(entry_minutes) is not int or type(trend_minutes) is not int
            or entry_minutes <= 0 or trend_minutes <= entry_minutes
            or trend_minutes % entry_minutes):
        raise ValueError("Timeframes must be positive integer minutes with trend a larger entry multiple")
    details = {
        "entry_timeframe": timeframe_label(entry_minutes),
        "trend_timeframe": timeframe_label(trend_minutes),
        "entry_minutes": entry_minutes, "trend_minutes": trend_minutes,
        "required_bars_per_timeframe": HISTORY_BARS,
        "entry_bars": len(m1), "trend_bars_received": len(m5),
        "closed_trend_bars": 0, "future_trend_bars_excluded": 0,
        "sufficient_history": False,
    }

    def rejected(reason):
        details["rejected_reasons"] = [reason]
        return Assessment(None, details)

    if not _ordered(m1) or not _ordered(m5):
        return rejected("invalid_candle_order")
    cutoff = m1[-1].time + entry_minutes * 60 if m1 else None
    closed = [bar for bar in m5 if cutoff is not None and bar.time + trend_minutes * 60 <= cutoff]
    details.update(entry_close_cutoff=cutoff, closed_trend_bars=len(closed),
                   future_trend_bars_excluded=len(m5) - len(closed))
    if len(m1) < HISTORY_BARS or len(closed) < HISTORY_BARS:
        return rejected("insufficient_history")
    details["sufficient_history"] = True
    if not _valid(m1) or not _valid(closed):
        return rejected("invalid_candle_values")
    fast = _ema([bar.close for bar in closed], 20)
    slow = _ema([bar.close for bar in closed], 50)
    entry_ema = _ema([bar.close for bar in m1], 20)
    atr = _atr(m1)
    details["atr14"] = atr if math.isfinite(atr) else None
    if not math.isfinite(atr) or atr <= 0:
        return rejected("invalid_atr")
    previous, current = m1[-2:]
    details["trend_candles"] = [
        {"bar_time": closed[i].time, "close": closed[i].close,
         "ema20": fast[i], "ema50": slow[i],
         "ema20_slope": fast[i] - fast[i - 1], "ema50_slope": slow[i] - slow[i - 1]}
        for i in range(len(closed) - 3, len(closed))
    ]
    details["entry"] = {
        "previous_bar_time": previous.time, "previous_close": previous.close,
        "previous_ema20": entry_ema[-2], "previous_vs_ema20": previous.close - entry_ema[-2],
        "bar_time": current.time, "open": current.open, "close": current.close,
        "ema20": entry_ema[-1], "current_vs_ema20": current.close - entry_ema[-1],
    }
    conditions = {}
    signal = None
    for side, direction in (("buy", 1), ("sell", -1)):
        checks = {
            "ema_alignment": all(direction * (row["ema20"] - row["ema50"]) > 0 for row in details["trend_candles"]),
            "three_trend_closes": all(direction * (row["close"] - row["ema20"]) > 0 for row in details["trend_candles"]),
            "ema20_slopes": all(direction * row["ema20_slope"] > 0 for row in details["trend_candles"]),
            "ema50_slopes": all(direction * row["ema50_slope"] > 0 for row in details["trend_candles"]),
            "pullback": direction * (previous.close - entry_ema[-2]) <= 0,
            "reclaim": direction * (current.close - entry_ema[-1]) > 0,
            "directional_candle": direction * (current.close - current.open) > 0,
        }
        checks["trend"] = all(checks[name] for name in ("ema_alignment", "three_trend_closes", "ema20_slopes", "ema50_slopes"))
        conditions[side] = checks
        if all(checks.values()):
            signal = Signal(side, current.time, atr,
                            f"{timeframe_label(trend_minutes)} EMA20/50 {side} trend; "
                            f"{timeframe_label(entry_minutes)} EMA20 pullback/reclaim")
    details["conditions"] = conditions
    trend_side = next((side for side, checks in conditions.items() if checks["trend"]), None)
    details["trend_direction"] = trend_side or "none"
    reasons = []
    if trend_side is None:
        reasons.append("no_trend")
    else:
        for name in ("pullback", "reclaim", "directional_candle"):
            if not conditions[trend_side][name]:
                reasons.append("no_" + name)
    details["rejected_reasons"] = reasons
    return Assessment(signal, details)


def evaluate(
    m1: list[Bar], m5: list[Bar], *, entry_minutes: int = 1, trend_minutes: int = 5,
    diagnostics: dict | None = None,
) -> Signal | None:
    """Backward-compatible signal API with optional single-assessment details."""
    result = assess(m1, m5, entry_minutes=entry_minutes, trend_minutes=trend_minutes)
    if diagnostics is not None:
        diagnostics.update(result.diagnostics)
    return result.signal

