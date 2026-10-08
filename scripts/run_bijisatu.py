"""Launch BijiSatu from its project environment without changing the parent shell."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Start BijiSatu in observation mode by default.")
    result.add_argument("--execute", action="store_true", help="Enable order submission after selecting option 1 to confirm")
    result.add_argument("--once", action="store_true", help="Run one scan instead of continuous monitoring")
    result.add_argument("--verbose", action="store_true", help="Show detailed symbol diagnostics in the terminal")
    result.add_argument("--config", type=Path, help="Configuration path; relative paths are resolved from the project root")
    return result


def configuration(path: Path | None) -> Path:
    target = ROOT / "config.local.json" if path is None else path
    if not target.is_absolute():
        target = ROOT / target
    if path is None and not target.exists():
        source = ROOT / "config.example.json"
        with source.open("rb") as example, target.open("xb") as local:
            shutil.copyfileobj(example, local)
        print("Created config.local.json from the example. Review account, symbols, timezone and costs before execution.")
    if not target.is_file():
        raise FileNotFoundError(f"Configuration file not found: {target}")
    return target


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        python = ROOT / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            raise FileNotFoundError("Project Python was not found. Complete the virtual-environment setup in README.md first.")
        config = configuration(args.config)
        environment = os.environ.copy()
        environment.pop("BIJISATU_ALLOW_ORDERS", None)
        command = [str(python), "-m", "bijisatu", "run", "--config", str(config)]
        if args.execute:
            print("Order execution may use REAL MONEY. Risk settings remain those in your configuration.")
            print("Stopping this process does not close existing broker positions; server-side SL/TP remain active.")
            print("1. Enable order submission")
            print("2. Cancel (default)")
            answer = input("Select an option [1/2, default 2]: ")
            if answer.strip() != "1":
                print("Cancelled. No robot process started.")
                return 0
            environment["BIJISATU_ALLOW_ORDERS"] = "YES"
            command.append("--execute")
        else:
            print("Observation mode: no orders will be submitted.")
        if args.once:
            command.append("--once")
        if args.verbose:
            command.append("--verbose")
        print("Keep MT5 open and connected. Press Ctrl+C to stop the robot.", flush=True)
        return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode
    except EOFError:
        print("Execution confirmation was not received. No robot process started.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped. Existing broker positions are not closed automatically.")
        return 130
    except OSError as exc:
        print(f"Launcher stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

