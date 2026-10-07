import argparse
import hashlib
import json
import logging
import os
import platform
import sys
import time
from pathlib import Path

from . import __version__
from .broker import BrokerError, MT5Broker
from .config import load_config
from .engine import Engine
from .observability import configured_logging
from .state import StateError, StateStore, exclusive_lock


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bijisatu", description="BijiSatu — experimental MT5 short-term trading; no profit guarantee")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check Python/MT5 package without opening a terminal or placing orders")
    run = commands.add_parser("run", help="Observe MT5 signals by default; no orders without --execute and explicit configuration")
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--once", action="store_true", help="Observe one cycle then exit")
    run.add_argument("--execute", action="store_true", help="Allow REAL orders, including on real-money cent accounts")
    run.add_argument("--verbose", action="store_true")
    backtest = commands.add_parser("backtest", help="Offline M1 CSV simulation (not MT5 Strategy Tester)")
    backtest.add_argument("csv", type=Path)
    backtest.add_argument("--config", type=Path, default=None, help="Use runtime strategy/risk settings; explicit flags override them")
    backtest.add_argument("--strategy-mode", choices=("scalping", "intraday"), default=None)
    backtest.add_argument("--stop-atr", type=float, default=None)
    backtest.add_argument("--reward-ratio", type=float, default=None)
    backtest.add_argument("--max-spread-atr", type=float, default=None)
    backtest.add_argument("--cooldown-seconds", type=int, default=None)
    backtest.add_argument("--risk-buffer", type=float, default=None)
    backtest.add_argument("--max-open-risk", type=float, default=None, help="Combined planned open-risk ceiling as a fraction of current equity")
    backtest.add_argument("--equity", type=float, default=10000)
    backtest.add_argument("--spread", type=float, required=True, help="Fixed spread in price units, not points")
    backtest.add_argument("--commission-per-lot", type=float, default=None, help="Round-trip commission in account currency; required unless configured")
    backtest.add_argument("--value-per-price-unit", type=float, required=True, help="Account-currency PnL per one price unit per one lot")
    backtest.add_argument("--slippage", type=float, default=0, help="Adverse fill slippage in price units")
    backtest.add_argument("--risk", type=float, default=None)
    backtest.add_argument("--daily-loss", type=float, default=None)
    backtest.add_argument("--volume-min", type=float, default=0.01)
    backtest.add_argument("--volume-max", type=float, default=100)
    backtest.add_argument("--volume-step", type=float, default=0.01)
    backtest.add_argument("--broker-utc-offset", type=float, default=None)
    return root


def doctor() -> int:
    try:
        import MetaTrader5
        mt5_version = MetaTrader5.__version__
    except ImportError:
        mt5_version = "not installed (offline backtest still available)"
    print(json.dumps({"bijisatu": __version__, "python": platform.python_version(),
                      "platform": platform.system(), "executable": sys.executable,
                      "mt5_package": mt5_version, "terminal_connected": False,
                      "note": "Offline diagnostics only. No terminal initialized and no orders sent."}, indent=2))
    return 0


def _observe(engine, config, log, once: bool) -> int:
    previous = None
    heartbeat_due = 0.0
    cycle = 0
    while True:
        result = engine.step(time.time())
        cycle += 1
        signature = json.dumps(result, sort_keys=True, allow_nan=False)
        current = time.monotonic()
        changed = signature != previous
        if changed or current >= heartbeat_due:
            event = "runtime_status" if changed else "heartbeat"
            level = logging.WARNING if result["status"] == "halted" else logging.INFO
            log.log(level, "Runtime status" if changed else "Heartbeat", extra={
                "event": event, "details": {"cycle": cycle, **result},
            })
            previous = signature
            heartbeat_due = current + config.heartbeat_seconds
        if once:
            return 0
        time.sleep(config.poll_seconds)


def _run_connected(broker, config, args, log, mode: str) -> int:
    broker.connect()
    account = broker.account()
    identity = hashlib.sha256(f"{account.login}:{account.server}:{account.currency}".encode()).hexdigest()[:24]
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BijiSatu" / "locks"
    with exclusive_lock(root / f"{identity}.lock"):
        store = StateStore(Path(config.state_dir) / f"{identity}-{mode}.json")
        engine = Engine(broker, config, store, args.execute)
        log.warning("Trading account connected", extra={"event": "account_connected", "details": {
            "account_type": "DEMO" if account.is_demo else "REAL MONEY", "currency": account.currency,
            "equity": account.equity, "risk_percent": config.risk_fraction * 100,
            "daily_loss_percent": config.daily_loss_fraction * 100,
            "max_open_risk_percent": config.max_open_risk_fraction * 100,
            "max_positions": config.max_positions,
            "symbols": engine.symbols, "poll_seconds": config.poll_seconds,
            "strategy_mode": config.strategy_mode, "entry_minutes": engine.entry_minutes,
            "trend_minutes": engine.trend_minutes,
            "heartbeat_seconds": config.heartbeat_seconds,
        }})
        if not args.execute:
            log.warning("Observation only: no orders, virtual positions or simulated profits; use backtest for offline simulation",
                        extra={"event": "observation_mode"})
        if config.broker_utc_offset_hours is None:
            log.warning("No broker UTC offset configured; daily risk uses UTC until configured. Session labels are indicative, not broker trading hours",
                        extra={"event": "broker_timezone_unset"})
        result = _observe(engine, config, log, args.once)
        log.info("Run cycle completed; shutdown does not close broker positions",
                 extra={"event": "shutdown", "details": {"reason": "once_completed"}})
        return result


def run(args) -> int:
    config = load_config(args.config)
    config.validate(args.execute)
    if args.execute and os.environ.get("BIJISATU_ALLOW_ORDERS") != "YES":
        raise ValueError("Execution locked: set BIJISATU_ALLOW_ORDERS=YES only after testing and reviewing the account")
    mode = "execute" if args.execute else "dry-run"
    with configured_logging(Path(config.log_dir), verbose=args.verbose, mode=mode,
                            broker_utc_offset_hours=config.broker_utc_offset_hours) as log:
        broker = MT5Broker(config)
        try:
            log.info("Daily logging enabled", extra={"event": "logging_started", "details": {
                "directory": str(Path(config.log_dir) / "bijisatu"), "rotation_timezone": "UTC",
                "automatic_deletion": False, "file_level": "DEBUG",
            }})
            return _run_connected(broker, config, args, log, mode)
        except KeyboardInterrupt:
            log.info("Stopped by user; existing broker positions are NOT closed and server SL/TP remain active",
                     extra={"event": "shutdown", "details": {"reason": "keyboard_interrupt"}})
            return 0
        except (BrokerError, StateError, ValueError, OSError) as exc:
            log.error("Runtime stopped; no further entries will be attempted", extra={
                "event": "runtime_error", "details": {"error_type": type(exc).__name__, "reason": str(exc)},
            })
            raise
        finally:
            broker.close()


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "run":
            return run(args)
        from .backtest import run_backtest
        config = load_config(args.config)

        def setting(argument, name):
            value = getattr(args, argument)
            return getattr(config, name) if value is None else value

        commission = setting("commission_per_lot", "commission_per_lot")
        if commission is None:
            raise ValueError("Backtest requires --commission-per-lot or a configured commission_per_lot")
        result = run_backtest(
            args.csv, initial_equity=args.equity, spread=args.spread,
            commission_per_lot=commission, value_per_price_unit=args.value_per_price_unit,
            slippage=args.slippage, risk_fraction=setting("risk", "risk_fraction"),
            daily_loss_fraction=setting("daily_loss", "daily_loss_fraction"),
            volume_min=args.volume_min, volume_max=args.volume_max, volume_step=args.volume_step,
            broker_utc_offset_hours=setting("broker_utc_offset", "broker_utc_offset_hours") or 0,
            strategy_mode=setting("strategy_mode", "strategy_mode"),
            stop_atr=setting("stop_atr", "stop_atr"), reward_ratio=setting("reward_ratio", "reward_ratio"),
            max_spread_atr=setting("max_spread_atr", "max_spread_atr"),
            cooldown_seconds=setting("cooldown_seconds", "cooldown_seconds"),
            max_open_risk_fraction=setting("max_open_risk", "max_open_risk_fraction"),
            risk_buffer=setting("risk_buffer", "risk_buffer") if args.config is not None or args.risk_buffer is not None else 1.0,
        )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print("Stopped. Existing broker positions are NOT closed; server-side SL/TP remain active.")
        return 0
    except (BrokerError, StateError, ValueError, OSError) as exc:
        print(f"BijiSatu stopped safely: {exc}", file=sys.stderr)
        return 2

