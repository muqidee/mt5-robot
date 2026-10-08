import importlib
import math
from datetime import datetime, timezone

from .config import Config
from .models import Account, Bar, OrderPlan, Position, SymbolSpec, Tick

# MQL5 symbol capability bitmasks are not exported by the Python package.
SYMBOL_ORDER_MARKET, SYMBOL_ORDER_SL, SYMBOL_ORDER_TP = 1, 16, 32
SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC = 1, 2


class BrokerError(RuntimeError):
    pass


class OrderUncertain(BrokerError):
    pass


class MT5Broker:
    def __init__(self, config: Config):
        self.config = config
        self.mt5 = None
        self.identity = None

    def connect(self) -> None:
        try:
            self.mt5 = importlib.import_module("MetaTrader5")
        except ImportError as exc:
            raise BrokerError('Install the Windows MT5 extra: python -m pip install -e ".[mt5]"') from exc
        ok = self.mt5.initialize(self.config.terminal_path) if self.config.terminal_path else self.mt5.initialize()
        if not ok:
            raise BrokerError(f"MT5 initialize failed: {self.mt5.last_error()}")
        account = self.account()
        self.identity = (account.login, account.server)

    def close(self) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()

    def account(self) -> Account:
        terminal = self.mt5.terminal_info()
        raw = self.mt5.account_info()
        if terminal is None or not terminal.connected or raw is None:
            raise BrokerError("MT5 terminal/account is disconnected")
        if self.identity is not None and (raw.login, raw.server) != self.identity:
            raise BrokerError("MT5 account changed; restart with the intended account")
        if self.config.account_login is not None and raw.login != self.config.account_login:
            raise BrokerError("MT5 account does not match account_login")
        if not all(math.isfinite(v) for v in (raw.equity, raw.balance, raw.margin_free)) or raw.equity <= 0:
            raise BrokerError("Invalid or nonpositive account equity")
        allowed = bool(raw.trade_allowed and raw.trade_expert and terminal.trade_allowed and not terminal.tradeapi_disabled)
        return Account(raw.login, raw.server, raw.currency, raw.equity, raw.balance,
                       raw.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO, allowed)

    def positions(self) -> list[Position]:
        raw = self.mt5.positions_get()
        if raw is None:
            raise BrokerError(f"Cannot read positions: {self.mt5.last_error()}")
        return [Position(p.ticket, p.symbol, "buy" if p.type == self.mt5.POSITION_TYPE_BUY else "sell",
                         p.volume, p.price_current, p.sl, p.magic) for p in raw]

    def has_orders(self) -> bool:
        orders = self.mt5.orders_get()
        if orders is None:
            raise BrokerError("Cannot read pending orders")
        return bool(orders)

    def activity(self, start: float, end: float) -> tuple[bool, bool]:
        deals = self.mt5.history_deals_get(datetime.fromtimestamp(start, timezone.utc),
                                           datetime.fromtimestamp(end, timezone.utc))
        if deals is None:
            raise BrokerError("Cannot read account deal history; refusing new entries")
        trades = {self.mt5.DEAL_TYPE_BUY, self.mt5.DEAL_TYPE_SELL}
        return (any(d.type in trades for d in deals),
                any(d.type not in trades and (d.profit or d.commission or d.swap or d.fee)
                    and getattr(d, "time_msc", d.time * 1000) > start * 1000 for d in deals))

    def symbols(self) -> list[str]:
        if self.config.symbols:
            return list(dict.fromkeys(self.config.symbols))[:self.config.max_symbols]
        rows = self.mt5.symbols_get()
        if rows is None:
            raise BrokerError("Cannot discover symbols")
        currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}
        candidates = [s for s in rows if s.currency_base in currencies | {"XAU"}
                      and s.currency_profit in currencies
                      and s.trade_mode == self.mt5.SYMBOL_TRADE_MODE_FULL]
        candidates.sort(key=lambda s: (not s.visible, s.currency_profit != "USD", s.name))
        selected = {}
        for symbol in candidates:
            selected.setdefault((symbol.currency_base, symbol.currency_profit), symbol.name)
        return list(selected.values())[:self.config.max_symbols]

    def spec(self, symbol: str) -> SymbolSpec:
        if not self.mt5.symbol_select(symbol, True):
            raise BrokerError(f"Cannot select symbol {symbol}")
        raw = self.mt5.symbol_info(symbol)
        if raw is None or raw.trade_mode != self.mt5.SYMBOL_TRADE_MODE_FULL:
            raise BrokerError(f"Symbol not fully tradable: {symbol}")
        if not raw.order_mode & SYMBOL_ORDER_MARKET or not raw.order_mode & SYMBOL_ORDER_SL or not raw.order_mode & SYMBOL_ORDER_TP:
            raise BrokerError(f"Market orders with server SL/TP unsupported: {symbol}")
        values = (raw.point, raw.trade_tick_size, raw.volume_min, raw.volume_max, raw.volume_step)
        if not all(math.isfinite(v) and v > 0 for v in values) or raw.volume_max < raw.volume_min:
            raise BrokerError(f"Invalid symbol specifications: {symbol}")
        return SymbolSpec(symbol, raw.point, raw.digits, raw.trade_tick_size, raw.volume_min,
                          raw.volume_max, raw.volume_step, raw.trade_stops_level)

    def tick(self, symbol: str) -> Tick:
        raw = self.mt5.symbol_info_tick(symbol)
        if raw is None or not all(math.isfinite(v) and v > 0 for v in (raw.bid, raw.ask)) or raw.ask < raw.bid:
            raise BrokerError(f"Invalid quote: {symbol}")
        return Tick(raw.bid, raw.ask, raw.time)

    def bars(self, symbol: str, minutes: int) -> list[Bar]:
        names = {1: "TIMEFRAME_M1", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15", 60: "TIMEFRAME_H1"}
        if type(minutes) is not int or minutes not in names:
            raise ValueError("Unsupported candle timeframe")
        timeframe = getattr(self.mt5, names[minutes])
        rows = self.mt5.copy_rates_from_pos(symbol, timeframe, 1, 300)
        if rows is None:
            raise BrokerError(f"Cannot load M{minutes} bars: {symbol}")
        return [Bar(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["tick_volume"])) for r in rows]

    def profit(self, side: str, symbol: str, volume: float, entry: float, exit_price: float) -> float:
        order_type = self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL
        value = self.mt5.order_calc_profit(order_type, symbol, volume, entry, exit_price)
        if value is None or not math.isfinite(value):
            raise BrokerError(f"Cannot calculate account-currency risk: {symbol}")
        return value

    def send(self, plan: OrderPlan, *, before_send=None) -> str:
        account = self.account()
        if not account.trade_allowed:
            raise BrokerError("Automated trading is not permitted by terminal/account")
        positions = self.positions()
        if len(positions) >= self.config.max_positions or any(
            p.symbol == plan.symbol or p.magic != self.config.magic
            or not math.isfinite(p.sl) or p.sl <= 0
            or not math.isfinite(p.volume) or p.volume <= 0
            for p in positions
        ):
            raise BrokerError("Position exposure changed; new entry refused")
        if self.has_orders():
            raise BrokerError("Pending orders exist; entry refused")
        quote = self.tick(plan.symbol)
        spec = self.spec(plan.symbol)
        current = quote.ask if plan.side == "buy" else quote.bid
        if abs(current - plan.entry) > self.config.deviation_points * spec.point:
            raise BrokerError("Quote moved beyond deviation budget; wait for another signal")
        raw = self.mt5.symbol_info(plan.symbol)
        # Prefer all-or-nothing fills; never retry a send with a different policy.
        if raw.filling_mode & SYMBOL_FILLING_FOK:
            filling = self.mt5.ORDER_FILLING_FOK
        elif raw.filling_mode & SYMBOL_FILLING_IOC:
            filling = self.mt5.ORDER_FILLING_IOC
        elif raw.trade_exemode != self.mt5.SYMBOL_TRADE_EXECUTION_MARKET:
            filling = self.mt5.ORDER_FILLING_RETURN
        else:
            raise BrokerError("No supported market-order filling policy")
        order_type = self.mt5.ORDER_TYPE_BUY if plan.side == "buy" else self.mt5.ORDER_TYPE_SELL
        margin = self.mt5.order_calc_margin(order_type, plan.symbol, plan.volume, current)
        live_account = self.mt5.account_info()
        if margin is None or not math.isfinite(margin) or margin < 0 or live_account is None:
            raise BrokerError("Cannot calculate required margin")
        if margin > live_account.margin_free * self.config.max_margin_fraction:
            raise BrokerError("Insufficient free-margin safety buffer")
        request = {"action": self.mt5.TRADE_ACTION_DEAL, "symbol": plan.symbol, "volume": plan.volume,
                   "type": order_type, "price": current, "sl": plan.sl, "tp": plan.tp,
                   "deviation": self.config.deviation_points, "magic": self.config.magic,
                   "comment": "BijiSatu", "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": filling}
        check = self.mt5.order_check(request)
        if check is None or check.retcode != 0:
            raise BrokerError(f"Order check rejected: {getattr(check, 'retcode', 'unavailable')}")
        self.account()
        if before_send is not None:
            before_send()
        try:
            result = self.mt5.order_send(request)
        except Exception as exc:
            raise OrderUncertain("Order-send exception; verify terminal manually, no automatic retry") from exc
        if result is None:
            raise OrderUncertain("No order acknowledgement; verify terminal manually, no automatic retry")
        retcode = getattr(result, "retcode", None)
        if retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL):
            return f"retcode={retcode} order={result.order} deal={result.deal} volume={result.volume}"
        rejected_codes = (
            self.mt5.TRADE_RETCODE_REQUOTE, self.mt5.TRADE_RETCODE_REJECT,
            self.mt5.TRADE_RETCODE_CANCEL, self.mt5.TRADE_RETCODE_INVALID,
            self.mt5.TRADE_RETCODE_INVALID_VOLUME, self.mt5.TRADE_RETCODE_INVALID_PRICE,
            self.mt5.TRADE_RETCODE_INVALID_STOPS, self.mt5.TRADE_RETCODE_TRADE_DISABLED,
            self.mt5.TRADE_RETCODE_MARKET_CLOSED, self.mt5.TRADE_RETCODE_NO_MONEY,
            self.mt5.TRADE_RETCODE_PRICE_CHANGED, self.mt5.TRADE_RETCODE_PRICE_OFF,
            self.mt5.TRADE_RETCODE_INVALID_EXPIRATION, self.mt5.TRADE_RETCODE_TOO_MANY_REQUESTS,
            self.mt5.TRADE_RETCODE_SERVER_DISABLES_AT, self.mt5.TRADE_RETCODE_CLIENT_DISABLES_AT,
            self.mt5.TRADE_RETCODE_INVALID_FILL, self.mt5.TRADE_RETCODE_INVALID_ORDER,
        )
        if retcode in rejected_codes:
            raise BrokerError(f"Order rejected: retcode={retcode}")
        raise OrderUncertain(f"Uncertain order status {retcode}; verify terminal manually, no automatic retry")

