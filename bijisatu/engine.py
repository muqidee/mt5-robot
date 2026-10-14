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
from .strategy import assess, evaluate, fresh_candle, timeframe_label, timeframes

LOG = logging.getLogger("bijisatu")


class TickTimeError(BrokerError):
    """A quote cannot be used until its timestamp passes freshness checks."""


def trading_day(now: float, offset_hours: float) -> tuple[str, float]:
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=offset_hours)))
    return local.date().isoformat(), local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Engine:
    def __init__(self, broker, config: Config, store: StateStore, execute: bool = False, observer=None,
                 entry_filter=None, commentary=None):
        config.validate(execute)
        self.broker = broker
        self.config = config
        self.entry_minutes, self.trend_minutes = timeframes(config.strategy_mode)
        self.store = store
        self.execute = execute
        self.observer = observer
        self.entry_filter = entry_filter
        self.commentary = commentary
        self._assessment_latest = {}
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
            self._fresh_tick(quote, now + (time.monotonic() - started))
            mark = quote.bid if position.side == "buy" else quote.ask
            loss = max(0.0, -self.broker.profit(position.side, position.symbol, position.volume, mark, position.sl))
            commission = (self.config.commission_per_lot or 0) * position.volume
            total += (loss + commission) * self.config.risk_buffer
        return total

    def _entry_budget(self, equity: float, limit: float, loss: float, reserved: float) -> float:
        daily_remaining = max(0.0, limit - loss - reserved)
        open_limit = equity * self.config.max_open_risk_fraction
        open_remaining = max(0.0, open_limit - reserved)
        budget = min(daily_remaining, open_remaining)
        self._snapshot.update(
            reserved_open_risk=round(reserved, 8), open_risk_limit=round(open_limit, 8),
            remaining_daily_risk=round(daily_remaining, 8),
            remaining_open_risk=round(open_remaining, 8), remaining_risk=round(budget, 8),
        )
        return budget

    def _fresh_tick(self, tick, now: float) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (tick.bid, tick.ask)) or tick.ask < tick.bid:
            raise BrokerError("Invalid bid/ask quote")
        if not math.isfinite(tick.time) or not math.isfinite(now):
            raise BrokerError("Invalid quote timestamp; new entries disabled")
        age = now - tick.time
        if age < -self.config.max_tick_future_seconds:
            raise TickTimeError(
                f"Future tick: {-age:.3f}s ahead of the local UTC clock exceeds "
                f"tolerance={self.config.max_tick_future_seconds}s; check clock synchronization"
            )
        if age > self.config.max_tick_age_seconds:
            raise TickTimeError(f"Stale tick: age={age:.3f}s exceeds limit={self.config.max_tick_age_seconds}s; waiting for a fresh quote")

    def _skip(self, symbol: str, reason: str, **details) -> None:
        self._skip_counts[reason] = self._skip_counts.get(reason, 0) + 1
        LOG.debug("Symbol skipped", extra={"event": "symbol_skip", "details": {
            "symbol": symbol, "reason": reason, **details,
        }})

    def _assessment_log(self, symbol, bar_time, diagnostics):
        if not diagnostics or bar_time <= self._assessment_latest.get(symbol, -1):
            return
        self._assessment_latest[symbol] = bar_time
        reasons = diagnostics.get("rejected_reasons", [])
        trend = diagnostics.get("trend_candles", [])
        entry = diagnostics.get("entry", {})
        failures = {}
        for side, checks in diagnostics.get("conditions", {}).items():
            failures[side] = [name for name, passed in checks.items() if not passed and name != "trend"]
        LOG.info("Closed-candle setup " + ("rejected" if reasons else "valid"), extra={
            "event": "setup_assessment", "details": {
                "symbol": symbol, "bar_time": bar_time,
                "entry_timeframe": diagnostics.get("entry_timeframe"),
                "trend_timeframe": diagnostics.get("trend_timeframe"),
                "reason": ", ".join(reasons) or "strategy conditions met; separate filter/risk checks required",
                "failed_conditions": failures,
                "trend_latest": trend[-1] if trend else None,
                "entry_latest": entry or None,
            },
        })

    def _prepare_commentary(self, now):
        if not self.config.market_commentary_enabled or self.commentary is None:
            return
        for symbol in self.symbols:
            try:
                entry = self.broker.bars(symbol, self.entry_minutes)
                trend = self.broker.bars(symbol, self.trend_minutes)
                self._market_bars[symbol] = (entry, trend)
                if not entry or not fresh_candle(entry[-1], self.entry_minutes, now, 30):
                    continue
                cutoff = entry[-1].time + self.entry_minutes * 60
                closed = [bar for bar in trend if bar.time + self.trend_minutes * 60 <= cutoff]
                if not closed or not fresh_candle(closed[-1], self.trend_minutes, now, 90):
                    continue
                assessment = assess(entry[-100:], closed[-100:], entry_minutes=self.entry_minutes,
                                    trend_minutes=self.trend_minutes)
                self._assessment_log(symbol, entry[-1].time, assessment.diagnostics)
                if "conditions" in assessment.diagnostics:
                    self._pending_commentary.append((symbol, self.entry_minutes, self.trend_minutes,
                                                    entry, trend, now))
            except (BrokerError, ValueError):
                LOG.debug("Commentary market data unavailable", extra={"event": "market_commentary_data_rejected",
                          "details": {"symbol": symbol, "commentary_only": True}})

    def step(self, now: float) -> dict:
        self._snapshot = {}
        self._skip_counts = {}
        self._market_bars = {}
        self._pending_commentary = []
        try:
            result = self._step(now)
        except TickTimeError as exc:
            kind = str(exc).split(":", 1)[0]
            previous_kind, logged_at = getattr(self, "_quote_warning", (None, float("-inf")))
            if kind != previous_kind or now - logged_at >= self.config.heartbeat_seconds:
                LOG.warning("Entry paused until quote timestamps are valid", extra={
                    "event": "quote_time_blocked", "details": {
                        "reason": str(exc), "retry_seconds": self.config.poll_seconds,
                    },
                })
                self._quote_warning = (kind, now)
            result = {"status": "blocked", "reason": str(exc),
                      "retry_seconds": self.config.poll_seconds}
        # Submit only after strategy/filter scheduling: approvals take queue priority.
        for args in self._pending_commentary:
            try:
                self.commentary.submit(*args)
            except Exception:
                LOG.debug("Market commentary snapshot rejected; trading rules unchanged", extra={
                    "event": "market_commentary_data_rejected", "details": {"symbol": args[0]},
                })
        if result["status"] in ("sent", "dry_run"):
            LOG.info("Order sent" if result["status"] == "sent" else "Dry-run entry claimed",
                     extra={"event": "order_sent" if result["status"] == "sent" else "entry_claimed",
                            "details": result.copy()})
        halt_reason = result.get("reason") if result["status"] == "halted" else None
        if halt_reason is not None and halt_reason != getattr(self, "_last_halt_reason", None):
            LOG.warning("New entries halted", extra={"event": "risk_halted", "details": {"reason": halt_reason}})
        self._last_halt_reason = halt_reason
        if result["status"] != "blocked" or not result.get("reason", "").startswith(("Stale tick:", "Future tick:")):
            self._quote_warning = (None, float("-inf"))
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
        now = cycle_time + (time.monotonic() - started)
        reserved = self._open_risk(positions, now)
        self._prepare_commentary(cycle_time + (time.monotonic() - started))
        budget = self._entry_budget(account.equity, limit, loss, reserved)
        if budget <= 0 or len(positions) >= self.config.max_positions:
            return {"status": "blocked", "reason": "Open-position or remaining daily/open-risk limit", "remaining": budget}
        held_symbols = {p.symbol for p in positions}
        candidates = []
        for symbol in self.symbols:
            now = cycle_time + (time.monotonic() - started)
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
                now = cycle_time + (time.monotonic() - started)
                context = {"bid": quote.bid, "ask": quote.ask,
                           "tick_age_seconds": round(now - quote.time, 3),
                           "max_tick_age_seconds": self.config.max_tick_age_seconds,
                           "max_tick_future_seconds": self.config.max_tick_future_seconds,
                           "spread_points": round((quote.ask - quote.bid) / spec.point, 3)}
                self._fresh_tick(quote, now)
                context["tick_time_utc"] = datetime.fromtimestamp(quote.time, timezone.utc).isoformat()
                if symbol in self._market_bars:
                    entry_bars, trend_bars = self._market_bars[symbol]
                else:
                    entry_bars = self.broker.bars(symbol, self.entry_minutes)
                    trend_bars = self.broker.bars(symbol, self.trend_minutes)
                now = cycle_time + (time.monotonic() - started)
                context.update(strategy_mode=self.config.strategy_mode,
                               entry_minutes=self.entry_minutes, trend_minutes=self.trend_minutes)
                for minutes, bars in ((self.entry_minutes, entry_bars), (self.trend_minutes, trend_bars)):
                    context[f"m{minutes}_bars"] = len(bars)
                    context[f"m{minutes}_close_age_seconds"] = (
                        round(now - (bars[-1].time + minutes * 60), 3) if bars else None
                    )
                if not entry_bars or not trend_bars:
                    self._skip(symbol, "missing_candles", **context)
                    continue
                if (not fresh_candle(entry_bars[-1], self.entry_minutes, now, 30)
                        or not fresh_candle(trend_bars[-1], self.trend_minutes, now, 90)):
                    self._skip(symbol, "stale_or_future_candles", **context)
                    continue
                # A trend candle may close during a delayed scan, after the entry cutoff.
                cutoff = entry_bars[-1].time + self.entry_minutes * 60
                closed_trend = [bar for bar in trend_bars if bar.time + self.trend_minutes * 60 <= cutoff]
                if not closed_trend or not fresh_candle(closed_trend[-1], self.trend_minutes, now, 90):
                    self._skip(symbol, "stale_or_future_candles", **context)
                    continue
                diagnostics = {}
                if self.config.strategy_mode == "scalping":
                    signal = evaluate(entry_bars, trend_bars, diagnostics=diagnostics)
                else:
                    signal = evaluate(entry_bars[-100:], closed_trend[-100:], entry_minutes=self.entry_minutes,
                                      trend_minutes=self.trend_minutes, diagnostics=diagnostics)
                self._assessment_log(symbol, entry_bars[-1].time, diagnostics)
                context["assessment"] = diagnostics
                context.update(entry_timeframe=timeframe_label(self.entry_minutes),
                               trend_timeframe=timeframe_label(self.trend_minutes),
                               future_trend_bars_excluded=len(trend_bars) - len(closed_trend))
                LOG.debug("Closed-candle strategy assessment", extra={"event": "strategy_assessment", "details": {
                    "symbol": symbol, **context,
                }})
                if signal is None:
                    reason = "insufficient_history" if len(entry_bars) < 100 or len(closed_trend) < 100 else "no_trend_pullback_setup"
                    self._skip(symbol, reason, required_bars_per_timeframe=100, **context)
                    continue
                if self.observer is not None:
                    try:
                        self.observer.submit(symbol, signal, self.entry_minutes, self.trend_minutes,
                                             entry_bars, closed_trend, quote.ask - quote.bid)
                    except Exception:
                        LOG.info("Ollama observation unavailable; trading rules unchanged", extra={
                            "event": "ollama_observation_error", "details": {
                                "symbol": symbol, "bar_time": signal.bar_time,
                                "observation_only": True, "reason": "scheduling_failed",
                            },
                        })
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
                binding = None
                if self.config.ollama_filter_enabled:
                    status = "unavailable"
                    try:
                        if self.entry_filter is not None:
                            status, binding = self.entry_filter.check(
                                symbol, signal, self.entry_minutes, self.trend_minutes,
                                entry_bars, closed_trend, quote.ask - quote.bid)
                    except Exception:
                        LOG.info("Ollama entry filter unavailable", extra={
                            "event": "ollama_filter_unavailable", "details": {
                                "symbol": symbol, "bar_time": signal.bar_time,
                                "execution_filter": True, "reason": "scheduling_failed",
                            },
                        })
                    if status != "allowed" or binding is None:
                        self._skip(symbol, "ollama_filter_" + (status if status in (
                            "pending", "rejected", "unavailable") else "unavailable"))
                        continue
                candidates.append((spread_ratio, symbol, signal, spec, closed_trend[-1], binding))
            except (BrokerError, ValueError) as exc:
                age = context.get("tick_age_seconds")
                reason = "market_data_rejected"
                if age is not None and age < -self.config.max_tick_future_seconds:
                    reason = "future_tick"
                elif age is not None and age > self.config.max_tick_age_seconds:
                    reason = "stale_tick"
                self._skip(symbol, reason, error=str(exc), **context)
        candidates.sort(key=lambda candidate: candidate[0])
        for _, symbol, signal, spec, trend_bar, binding in candidates:
            now = cycle_time + (time.monotonic() - started)
            if trading_day(now, self.config.broker_utc_offset_hours or 0)[0] != state["day"]:
                return {"status": "waiting", "reason": "Day changed during scan; rebaseline next cycle"}
            if (not 0 <= now - (signal.bar_time + self.entry_minutes * 60) <= self.entry_minutes * 60 + 30
                    or not fresh_candle(trend_bar, self.trend_minutes, now, 90)):
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
                now = cycle_time + (time.monotonic() - started)
                reserved = self._open_risk(positions, now)
                budget = self._entry_budget(account.equity, limit, loss, reserved)
                self._snapshot.update(equity=account.equity, balance=account.balance,
                                      daily_loss=round(loss, 8), open_positions=len(positions))
                if budget <= 0:
                    return {"status": "blocked", "reason": "Daily/open-risk budget exhausted during scan", "remaining": budget}
                quote = self.broker.tick(symbol)
                now = cycle_time + (time.monotonic() - started)
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
                now = cycle_time + (time.monotonic() - started)
                if (not 0 <= now - (signal.bar_time + self.entry_minutes * 60) <= self.entry_minutes * 60 + 30
                        or not fresh_candle(trend_bar, self.trend_minutes, now, 90)):
                    self._skip(symbol, "signal_expired_during_scan", bar_time=signal.bar_time)
                    continue
                if trading_day(now, self.config.broker_utc_offset_hours or 0)[0] != state["day"]:
                    return {"status": "waiting", "reason": "Day changed before entry; rebaseline next cycle"}
                if self.execute and not account.trade_allowed:
                    raise BrokerError("Trading permissions changed before entry")
                if self.config.ollama_filter_enabled:
                    try:
                        allowed = self.entry_filter is not None and self.entry_filter.lookup(binding) == "allowed"
                    except Exception:
                        allowed = False
                    if not allowed:
                        self._skip(symbol, "ollama_filter_unavailable")
                        continue
                def claim_filtered_entry():
                    # Broker preflight has passed; refresh safety immediately before the durable claim.
                    current_time = cycle_time + (time.monotonic() - started)
                    live_account = self.broker.account()
                    live_positions = self.broker.positions()
                    if (not live_account.trade_allowed or self.broker.has_orders()
                            or len(live_positions) >= self.config.max_positions
                            or any(p.symbol == symbol for p in live_positions)):
                        raise BrokerError("Exposure or trading permissions changed before filtered entry")
                    if trading_day(current_time, self.config.broker_utc_offset_hours or 0)[0] != state["day"]:
                        raise BrokerError("Day changed before filtered entry")
                    _, cashflow = self.broker.activity(state["baseline_at"], current_time)
                    if cashflow:
                        self.store.halt("Account cash flow detected before filtered entry")
                        raise BrokerError("Daily baseline changed before filtered entry")
                    live_loss = max(0.0, state["baseline"] - live_account.equity)
                    if live_loss >= limit:
                        self.store.halt("Daily equity loss limit reached before filtered entry")
                        raise BrokerError("Daily equity loss limit reached before filtered entry")
                    live_reserved = self._open_risk(live_positions, current_time)
                    live_budget = self._entry_budget(live_account.equity, limit, live_loss, live_reserved)
                    if plan.risk_amount > min(live_budget, live_account.equity * self.config.risk_fraction):
                        raise BrokerError("Risk budget changed before filtered entry")
                    live_quote = self.broker.tick(symbol)
                    current_time = cycle_time + (time.monotonic() - started)
                    self._fresh_tick(live_quote, current_time)
                    live_entry = live_quote.ask if signal.side == "buy" else live_quote.bid
                    if (abs(live_entry - plan.entry) > deviation
                            or (live_quote.ask - live_quote.bid) / signal.atr > self.config.max_spread_atr):
                        raise BrokerError("Quote changed before filtered entry")
                    if (trading_day(current_time, self.config.broker_utc_offset_hours or 0)[0] != state["day"]
                            or not 0 <= current_time - (signal.bar_time + self.entry_minutes * 60) <= self.entry_minutes * 60 + 30
                            or not fresh_candle(trend_bar, self.trend_minutes, current_time, 90)
                            or self.entry_filter.lookup(binding) != "allowed"):
                        raise BrokerError("Filter approval, trading day or signal expired before entry")
                    self.store.claim(symbol, signal.bar_time, current_time)
                    LOG.info("Filtered entry claimed", extra={"event": "entry_claimed", "details": {
                        "symbol": symbol, "bar_time": signal.bar_time, "side": signal.side,
                    }})

                # Claim only after approval; filter execution defers it until broker preflight passes.
                if not self.execute or not self.config.ollama_filter_enabled:
                    self.store.claim(symbol, signal.bar_time, now)
                    LOG.info("Entry claimed", extra={"event": "entry_claimed", "details": {
                        "symbol": symbol, "bar_time": signal.bar_time, "side": signal.side,
                    }})
                if not self.execute:
                    return {"status": "dry_run", "plan": asdict(plan), "currency": account.currency,
                            "note": "No order sent; observation only, not simulated profit"}
                result = (self.broker.send(plan, before_send=claim_filtered_entry)
                          if self.config.ollama_filter_enabled else self.broker.send(plan))
                return {"status": "sent", "plan": asdict(plan), "result": result, "currency": account.currency}
            except OrderUncertain as exc:
                self.store.halt(str(exc))
                return {"status": "halted", "reason": str(exc)}
            except TickTimeError:
                raise
            except BrokerError as exc:
                LOG.warning("Entry skipped", extra={"event": "entry_rejected", "details": {
                    "symbol": symbol, "reason": str(exc),
                }})
                if state["halted"]:
                    return {"status": "halted", "reason": state["reason"]}
                return {"status": "blocked", "reason": str(exc)}
        return {"status": "waiting", "symbols": len(self.symbols), "remaining": budget}

