"""Educational, offline, single-symbol bid-OHLC backtest (not a tick simulator)."""

import csv
from datetime import datetime
import math
from numbers import Real
from pathlib import Path
import re

from . import risk, strategy
from .models import Bar, Signal


def _finite(value: float) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _timestamp(text: str) -> int:
    text = text.strip()
    if re.fullmatch(r"[+-]?\d+", text):
        result = int(text)
    else:
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("ISO8601 timestamps require a timezone")
        seconds = date.timestamp()
        if not seconds.is_integer():
            raise ValueError("Timestamps must be minute aligned")
        result = int(seconds)
    if result < 0 or result % 60:
        raise ValueError("Timestamps must be nonnegative, minute-aligned UTC seconds")
    return result


def _load_bars(path: Path) -> list[Bar]:
    bars = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if not {"time", "open", "high", "low", "close"}.issubset(headers) or len(set(headers)) != len(headers):
            raise ValueError("CSV requires unique time,open,high,low,close headers")
        try:
            for row in reader:
                try:
                    if None in row:
                        raise ValueError("Too many columns")
                    bar = Bar(
                        _timestamp(row["time"]),
                        *(float(row[name]) for name in ("open", "high", "low", "close")),
                        tick_volume=float(row.get("tick_volume", "0")),
                    )
                    if not all(_finite(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.tick_volume)):
                        raise ValueError("Nonfinite OHLC/volume")
                    if not (0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
                        raise ValueError("Inconsistent or nonpositive OHLC")
                    if bar.tick_volume < 0 or (bars and bar.time <= bars[-1].time):
                        raise ValueError("Negative volume or timestamps not strictly increasing")
                except (ValueError, TypeError, AttributeError, OverflowError) as exc:
                    raise ValueError(f"Invalid CSV row {reader.line_num}: {exc}") from exc
                bars.append(bar)
        except csv.Error as exc:
            raise ValueError(f"Invalid CSV: {exc}") from exc
    if not bars:
        raise ValueError("CSV contains no bars")
    return bars


def run_backtest(
    path: Path, *, initial_equity: float = 10000, risk_fraction: float = 0.03,
    daily_loss_fraction: float = 0.05, spread: float, commission_per_lot: float,
    value_per_price_unit: float, volume_min: float = 0.01,
    volume_max: float = 100.0, volume_step: float = 0.01,
    broker_utc_offset_hours: float = 0.0, slippage: float = 0.0,
) -> dict:
    """Return JSON-safe summary, parameters, assumptions, trades and daily records.

    Prices are bid quotes; account amounts all use the caller's currency units
    (including cents). Invalid CSV/configuration raises ValueError; I/O errors
    propagate. Daily loss gates entries, never promises a maximum realized loss.
    """
    parameters = dict(
        initial_equity=initial_equity, risk_fraction=risk_fraction,
        daily_loss_fraction=daily_loss_fraction, spread=spread,
        commission_per_lot=commission_per_lot, value_per_price_unit=value_per_price_unit,
        volume_min=volume_min, volume_max=volume_max, volume_step=volume_step,
        broker_utc_offset_hours=broker_utc_offset_hours, slippage=slippage,
    )
    if not all(_finite(value) for value in parameters.values()):
        raise ValueError("All backtest parameters must be finite numbers")
    if (min(initial_equity, value_per_price_unit, volume_min, volume_max, volume_step) <= 0
            or not 0 < risk_fraction <= 1 or not 0 < daily_loss_fraction <= 1
            or min(spread, commission_per_lot, slippage) < 0 or volume_max < volume_min
            or abs(broker_utc_offset_hours) > 24):
        raise ValueError("Invalid equity, risk, costs, volume limits or UTC offset")

    bars = _load_bars(path)
    balance = float(initial_equity)
    equity = balance
    peak = balance
    max_drawdown = max_drawdown_fraction = 0.0
    position = None
    pending = None
    last_entry = None
    m1: list[Bar] = []
    m5: list[Bar] = []
    trades = []
    days = []
    day = None

    def mark(bid: float) -> float:
        if position is None:
            return balance
        quote = bid if position["side"] == "buy" else bid + spread
        exit_price = quote - position["direction"] * slippage
        return balance + (
            position["direction"] * (exit_price - position["entry_price"])
            * value_per_price_unit - commission_per_lot
        ) * position["volume"]

    def observe(value: float) -> None:
        nonlocal equity, peak, max_drawdown, max_drawdown_fraction
        if not math.isfinite(value):
            raise ValueError("Account arithmetic overflow; rescale prices/currency inputs")
        equity = value
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
        max_drawdown_fraction = max(max_drawdown_fraction, (peak - value) / peak)
        day["lowest_equity"] = min(day["lowest_equity"], value)
        if value <= day["starting_equity"] * (1 - daily_loss_fraction):
            day["limit_reached"] = True
        day["ending_equity"] = value

    def close(exit_price: float, exit_time: int, reason: str) -> None:
        nonlocal balance, position
        gross = (position["direction"] * (exit_price - position["entry_price"])
                 * value_per_price_unit * position["volume"])
        commission = commission_per_lot * position["volume"]
        pnl = gross - commission
        balance += pnl
        trades.append({
            key: value for key, value in position.items() if key != "direction"
        } | dict(exit_time=exit_time, exit_price=exit_price, exit_reason=reason,
                 forced_exit=reason == "forced_exit", gross_pnl=gross,
                 commission=commission, pnl=pnl))
        position = None
        observe(balance)

    for bar in bars:
        day_id = math.floor((bar.time + broker_utc_offset_hours * 3600) / 86400)
        if day is None or day["day_id"] != day_id:
            # Carry the last observed close across midnight, so opening gaps count.
            day = dict(day_id=day_id, starting_equity=equity, ending_equity=equity,
                       lowest_equity=equity, limit_reached=False)
            days.append(day)
        observe(mark(bar.open))

        if pending is not None and position is None and not day["limit_reached"] and balance > 0:
            if last_entry is None or bar.time - last_entry >= 300:
                direction = 1 if pending.side == "buy" else -1
                entry = bar.open + (spread if direction == 1 else 0) + direction * slippage
                distance = max(1.5 * pending.atr, spread + slippage)
                stop = entry - direction * distance
                target = entry + direction * distance * 1.5
                loss_per_lot = (distance + slippage) * value_per_price_unit + commission_per_lot
                remaining = equity - day["starting_equity"] * (1 - daily_loss_fraction)
                volume = risk.size_volume(
                    equity=equity, risk_fraction=risk_fraction, remaining_budget=remaining,
                    loss_per_lot=loss_per_lot, volume_min=volume_min,
                    volume_max=volume_max, volume_step=volume_step,
                )
                if (volume > 0 and all(math.isfinite(p) and p > 0 for p in (entry, stop, target))
                        and (stop < entry < target if direction == 1 else target < entry < stop)):
                    position = dict(side=pending.side, direction=direction, volume=volume,
                                    signal_time=pending.bar_time, entry_time=bar.time,
                                    entry_price=entry, sl=stop, tp=target)
                    last_entry = bar.time
                    observe(mark(bar.open))
        pending = None

        if position is not None:
            direction = position["direction"]
            adjustment = spread if direction == -1 else 0
            opening = bar.open + adjustment
            adverse = (bar.low if direction == 1 else bar.high) + adjustment
            favorable = (bar.high if direction == 1 else bar.low) + adjustment
            stop, target = position["sl"], position["tp"]
            if direction * (opening - stop) <= 0:
                close(opening - direction * slippage, bar.time, "stop_gap")
            elif direction * (opening - target) >= 0:
                # No favorable gap improvement: credit only the limit price.
                close(target - direction * slippage, bar.time, "take_profit")
            elif direction * (adverse - stop) <= 0:
                close(stop - direction * slippage, bar.time + 60, "stop_loss")
            else:
                # Unknown intrabar path: assume adverse excursion precedes TP.
                adverse_bid = adverse - adjustment
                observe(mark(adverse_bid))
                if direction * (favorable - target) >= 0:
                    close(target - direction * slippage, bar.time + 60, "take_profit")
        observe(mark(bar.close))

        m1.append(bar)
        m1 = m1[-100:]
        if bar.time % 300 == 240 and len(m1) >= 5:
            group = m1[-5:]
            if all(item.time == bar.time - 240 + index * 60 for index, item in enumerate(group)):
                m5.append(Bar(group[0].time, group[0].open, max(item.high for item in group),
                              min(item.low for item in group), group[-1].close,
                              sum(item.tick_volume for item in group)))
                m5 = m5[-100:]
        if position is None and not day["limit_reached"] and len(m1) == len(m5) == 100:
            signal = strategy.evaluate(list(m1), list(m5))
            if (isinstance(signal, Signal) and signal.side in ("buy", "sell")
                    and signal.bar_time == bar.time and _finite(signal.atr)
                    and signal.atr > 0 and spread / signal.atr <= 0.15):
                pending = signal

    if position is not None:
        quote = bars[-1].close + (spread if position["side"] == "sell" else 0)
        close(quote - position["direction"] * slippage, bars[-1].time + 60, "forced_exit")

    return dict(
        initial_equity=float(initial_equity), final_equity=balance,
        net_profit=balance - initial_equity, return_fraction=(balance - initial_equity) / initial_equity,
        trade_count=len(trades), wins=sum(trade["pnl"] > 0 for trade in trades),
        losses=sum(trade["pnl"] < 0 for trade in trades),
        total_commission=sum(trade["commission"] for trade in trades),
        forced_exits=sum(trade["forced_exit"] for trade in trades),
        max_drawdown=max_drawdown, max_drawdown_fraction=max_drawdown_fraction,
        daily_limit_days=sum(item["limit_reached"] for item in days),
        bars=len(bars), start_time=bars[0].time, end_time=bars[-1].time + 60,
        parameters=parameters, trades=trades, days=days,
        assumptions=[
            "Educational offline single-symbol simulation; no profitability claims or live execution guarantee.",
            "CSV times are UTC bar opens; OHLC are positive bid prices. Ask = bid + constant spread.",
            "Only complete, boundary-aligned contiguous M1 groups form M5 bars. Evaluate latest 100 closed bars of each timeframe.",
            "Signals enter at the next observed M1 open, even after missing minutes; no fills are invented in gaps. One position; 300-second entry-to-entry cooldown.",
            "Entry requires spread/ATR <= 0.15. SL distance = max(1.5*ATR, spread+slippage); TP distance = 1.5*SL distance. No broker tick-grid/stops metadata is modeled.",
            "Adverse slippage applies at entry and every exit. Roundtrip commission is charged once on exit and reserved in floating equity and lot sizing.",
            "Volume is floored by risk.size_volume using stop loss plus exit slippage and commission, capped by remaining daily loss budget; spread is embedded in execution prices.",
            "Opening gaps are processed before candle extremes. Stop gaps fill at the adverse opening quote plus slippage; favorable TP gaps receive no price improvement. Otherwise simultaneous SL/TP touches select SL.",
            "Intrabar adverse excursion is assumed first, bounded by a triggered stop. Daily limits and drawdown use realized plus conservative liquidation-valued floating equity; favorable intrabar peaks are not sampled.",
            "Fixed broker UTC offset defines days (no DST). New-day baseline is previous observed close equity, including overnight opening gap losses. A breached daily limit latches: no new entries that day; existing positions retain SL/TP.",
            "Gaps, costs and slippage can exceed risk/daily limits, even producing negative equity. Drawdown is an OHLC-sampled estimate, not a tick-level bound.",
            "Final open position is liquidated at last close with exit costs and marked forced_exit. No swaps, funding, latency, liquidity or partial fills are modeled; amounts and conversion value must share account currency units (including cents).",
        ],
    )

