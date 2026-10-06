import logging
import math
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .broker import BrokerError, OrderUncertain
from .config import Config
from .models import OrderPlan
from .risk import price_levels, size_volume
from .state import StateError, StateStore
from .strategy import evaluate

LOG = logging.getLogger("bijisatu")


def trading_day(now: float, offset_hours: float) -> tuple[str, float]:
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=offset_hours)))
    return local.date().isoformat(), local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Engine:
    def __init__(self, broker, config: Config, store: StateStore, execute: bool = False):
        config.validate(execute)
        self.broker = broker
        self.config = config
        self.store = store
        self.execute = execute
        self.symbols = broker.symbols()
        if not self.symbols:
            raise BrokerError("No eligible symbols; configure exact broker symbol names")

    def _begin_day(self, account, positions, now: float) -> None:
        day, start = trading_day(now, self.config.broker_utc_offset_hours or 0)
        state = self.store.data
        if state and state["last_seen"] > now + 5:
            raise StateError("Clock moved backwards; new entries disabled")
        if state and state["day"] == day:
            return
        traded, cashflow = self.broker.activity(start, now)
        continuous_rollover = (state is not None and 0 <= now - start <= 30
                               and 0 <= start - state["last_seen"] <= 30)
        if cashflow or ((positions or traded) and not continuous_rollover):
            raise StateError("No trusted daily baseline. Start on a clean flat trading day, or keep the robot running across midnight")
        self.store.start_day(day, account.equity, now)
        LOG.info("Daily risk baseline established", extra={"event": "daily_baseline", "details": {
            "equity": account.equity, "currency": account.currency, "day": day,
            "basis": "first_observed_equity", "broker_utc_offset_hours": self.config.broker_utc_offset_hours,
        }})

    def _open_risk(self, positions, now: float) -> float:
        started = time.monotonic()
        total = 0.0
        for position in positions:
            if position.magic != self.config.magic:
                raise BrokerError("Non-BijiSatu position detected; use a dedicated account")
            if not math.isfinite(position.sl) or position.sl <= 0 or not math.isfinite(position.volume) or position.volume <= 0:
                raise BrokerError("Position has no valid server-side stop; new entries disabled")
            quote = self.broker.tick(position.symbol)
            self._fresh_tick(quote, now + time.monotonic() - started)
            mark = quote.bid if position.side == "buy" else quote.ask
            loss = max(0.0, -self.broker.profit(position.side, position.symbol, position.volume, mark, position.sl))
            commission = (self.config.commission_per_lot or 0) * position.volume
            total += (loss + commission) * self.config.risk_buffer
        return total

    def _fresh_tick(self, tick, now: float) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (tick.bid, tick.ask)) or tick.ask < tick.bid:
            raise BrokerError("Invalid bid/ask quote")
        if not math.isfinite(tick.time) or not math.isfinite(now):
            raise BrokerError("Invalid quote timestamp; new entries disabled")
        age = now - tick.time
        if age < 0:
            raise BrokerError(f"Future tick: {-age:.3f}s ahead of the local UTC clock; check clock synchronization")
        if age > self.config.max_tick_age_seconds:
            raise BrokerError(f"Stale tick: age={age:.3f}s exceeds limit={self.config.max_tick_age_seconds}s; waiting for a fresh quote")

    def _skip(self, symbol: str, reason: str, **details) -> None:
        self._skip_counts[reason] = self._skip_counts.get(reason, 0) + 1
        LOG.debug("Symbol skipped", extra={"event": "symbol_skip", "details": {
            "symbol": symbol, "reason": reason, **details,
        }})

    def step(self, now: float) -> dict:
        self._snapshot = {}
        self._skip_counts = {}
        result = self._step(now)
        result["account"] = self._snapshot
        result["skipped"] = self._skip_counts
        LOG.debug("Scan completed", extra={"event": "scan_completed", "details": result})
        return result

    def _step(self, now: float) -> dict:
        started = time.monotonic()
        cycle_time = now
        account = self.broker.account()
        if self.execute and not account.trade_allowed:
            raise BrokerError("Enable algorithmic trading and the Python trading API in MT5 before execution")
        positions = self.broker.positions()
        self._begin_day(account, positions, now)
        state = self.store.data
        _, cashflow = self.broker.activity(state["baseline_at"], now)
        if cashflow and not state["halted"]:
            self.store.halt("Account cash flow detected; daily baseline requires next-day reset")
        loss = max(0.0, state["baseline"] - account.equity)
        limit = state["baseline"] * self.config.daily_loss_fraction
        self._snapshot = {"currency": account.currency, "equity": account.equity,
                          "balance": account.balance, "daily_baseline": state["baseline"],
                          "daily_loss": round(loss, 8), "daily_loss_limit": round(limit, 8),
                          "open_positions": len(positions)}
        if loss >= limit and not state["halted"]:
            self.store.halt("Daily equity loss limit reached")
        state["last_seen"] = now
        self.store.save()
        if state["halted"]:
            return {"status": "halted", "reason": state["reason"], "loss": loss, "limit": limit}
        if self.broker.has_orders():
            return {"status": "blocked", "reason": "Pending orders exist"}
        now = cycle_time + time.monotonic() - started
        reserved = self._open_risk(positions, now)
        budget = max(0.0, limit - loss - reserved)
        self._snapshot.update(reserved_open_risk=round(reserved, 8), remaining_risk=round(budget, 8))
        if budget <= 0 or len(positions) >= self.config.max_positions:
            return {"status": "blocked", "reason": "Open-position or remaining daily-risk limit", "remaining": budget}
        held_symbols = {p.symbol for p in positions}
        candidates = []
        for symbol in self.symbols:
            now = cycle_time + time.monotonic() - started
            if symbol in held_symbols:
                self._skip(symbol, "position_already_open")
                continue
            attempt = state["attempts"].get(symbol)
            if attempt and now - attempt["at"] < self.config.cooldown_seconds:
                self._skip(symbol, "cooldown", remaining_seconds=round(self.config.cooldown_seconds - (now - attempt["at"]), 3))
                continue
            context = {}
            try:
                spec = self.broker.spec(symbol)
                quote = self.broker.tick(symbol)
                now = cycle_time + time.monotonic() - started
                context = {"bid": quote.bid, "ask": quote.ask,
                           "tick_age_seconds": round(now - quote.time, 3),
                           "max_tick_age_seconds": self.config.max_tick_age_seconds,
                           "spread_points": round((quote.ask - quote.bid) / spec.point, 3)}
                self._fresh_tick(quote, now)
                context["tick_time_utc"] = datetime.fromtimestamp(quote.time, timezone.utc).isoformat()
                m1, m5 = self.broker.bars(symbol, 1), self.broker.bars(symbol, 5)
                now = cycle_time + time.monotonic() - started
                context.update(m1_bars=len(m1), m5_bars=len(m5),
                               m1_close_age_seconds=round(now - (m1[-1].time + 60), 3) if m1 else None,
                               m5_close_age_seconds=round(now - (m5[-1].time + 300), 3) if m5 else None)
                if not m1 or not m5:
                    self._skip(symbol, "missing_candles", **context)
                    continue
                if not 0 <= now - (m1[-1].time + 60) <= 90 or not 0 <= now - (m5[-1].time + 300) <= 390:
                    self._skip(symbol, "stale_or_future_candles", **context)
                    continue
                signal = evaluate(m1, m5)
                if signal is None:
                    reason = "insufficient_history" if len(m1) < 100 or len(m5) < 100 else "no_trend_pullback_setup"
                    self._skip(symbol, reason, required_bars_per_timeframe=100, **context)
                    continue
                if attempt and signal.bar_time <= attempt["bar_time"]:
                    self._skip(symbol, "signal_already_processed", bar_time=signal.bar_time, **context)
                    continue
                spread_ratio = (quote.ask - quote.bid) / signal.atr
                context.update(atr=signal.atr, spread_atr=round(spread_ratio, 6),
                               max_spread_atr=self.config.max_spread_atr, side=signal.side)
                if spread_ratio > self.config.max_spread_atr:
                    self._skip(symbol, "spread_too_wide", **context)
                    continue
                LOG.debug("Signal candidate found", extra={"event": "signal_candidate", "details": {
                    "symbol": symbol, "signal_reason": signal.reason, "bar_time": signal.bar_time, **context,
                }})
                candidates.append((spread_ratio, symbol, signal, spec))
            except (BrokerError, ValueError) as exc:
                age = context.get("tick_age_seconds")
                reason = "market_data_rejected"
                if age is not None and age < 0:
                    reason = "future_tick"
                elif age is not None and age > self.config.max_tick_age_seconds:
                    reason = "stale_tick"
                self._skip(symbol, reason, error=str(exc), **context)
        candidates.sort(key=lambda candidate: candidate[0])
        for _, symbol, signal, spec in candidates:
            now = cycle_time + time.monotonic() - started
            if trading_day(now, self.config.broker_utc_offset_hours or 0)[0] != state["day"]:
                return {"status": "waiting", "reason": "Day changed during scan; rebaseline next cycle"}
            if now - (signal.bar_time + 60) > 90:
                self._skip(symbol, "signal_expired_during_scan", bar_time=signal.bar_time)
                continue
            try:
                # Refresh exposure and price after scanning; never size from a stale scan snapshot.
                account = self.broker.account()
                positions = self.broker.positions()
                if self.broker.has_orders() or len(positions) >= self.config.max_positions or any(p.symbol == symbol for p in positions):
                    return {"status": "blocked", "reason": "Exposure changed during scan"}
                loss = max(0.0, state["baseline"] - account.equity)
                if loss >= limit:
                    self.store.halt("Daily equity loss limit reached during scan")
                    return {"status": "halted", "reason": state["reason"]}
                now = cycle_time + time.monotonic() - started
                reserved = self._open_risk(positions, now)
                budget = max(0.0, limit - loss - reserved)
                self._snapshot.update(equity=account.equity, balance=account.balance,
                                      daily_loss=round(loss, 8), open_positions=len(positions),
                                      reserved_open_risk=round(reserved, 8), remaining_risk=round(budget, 8))
                quote = self.broker.tick(symbol)
                now = cycle_time + time.monotonic() - started
                self._fresh_tick(quote, now)
                if (quote.ask - quote.bid) / signal.atr > self.config.max_spread_atr:
                    self._skip(symbol, "spread_widened_before_entry", spread_atr=(quote.ask - quote.bid) / signal.atr)
                    continue
                entry, sl, tp = price_levels(signal.side, quote, spec, signal.atr,
                                             self.config.stop_atr, self.config.reward_ratio)
                deviation = self.config.deviation_points * spec.point
                adverse_entry = entry + deviation if signal.side == "buy" else entry - deviation
                adverse_stop = sl - deviation if signal.side == "buy" else sl + deviation
                if min(adverse_entry, adverse_stop) <= 0:
                    self._skip(symbol, "invalid_adverse_price", adverse_entry=adverse_entry, adverse_stop=adverse_stop)
                    continue
                loss_per_lot = -self.broker.profit(signal.side, symbol, spec.volume_min, adverse_entry, adverse_stop) / spec.volume_min
                loss_per_lot = (loss_per_lot + (self.config.commission_per_lot or 0)) * self.config.risk_buffer
                volume = size_volume(equity=account.equity, risk_fraction=self.config.risk_fraction,
                                     remaining_budget=budget, loss_per_lot=loss_per_lot,
                                     volume_min=spec.volume_min, volume_max=spec.volume_max, volume_step=spec.volume_step)
                if volume <= 0:
                    self._skip(symbol, "insufficient_risk_for_minimum_lot", remaining_risk=round(budget, 8),
                               loss_per_lot=loss_per_lot, volume_min=spec.volume_min, currency=account.currency)
                    continue
                plan = OrderPlan(symbol, signal.side, volume, entry, sl, tp, volume * loss_per_lot, signal.bar_time)
                # Durable claim precedes the external side effect, including uncertain acknowledgements.
                self.store.claim(symbol, signal.bar_time, now)
                if not self.execute:
                    return {"status": "dry_run", "plan": asdict(plan), "currency": account.currency,
                            "note": "No order sent; observation only, not simulated profit"}
                result = self.broker.send(plan)
                return {"status": "sent", "plan": asdict(plan), "result": result, "currency": account.currency}
            except OrderUncertain as exc:
                self.store.halt(str(exc))
                return {"status": "halted", "reason": str(exc)}
            except BrokerError as exc:
                LOG.warning("Entry skipped", extra={"event": "entry_rejected", "details": {
                    "symbol": symbol, "reason": str(exc),
                }})
                return {"status": "blocked", "reason": str(exc)}
        return {"status": "waiting", "symbols": len(self.symbols), "remaining": budget}

