# MT5 Robot

A workspace for developing, testing, and running Python trading robots connected to MetaTrader 5.

This README covers shared setup and repository conventions. Strategy rules, risk settings, configuration, and execution commands belong in each robot's documentation under [docs/robots](docs/robots). Adding a robot should not require expanding this README into a strategy catalog.

## Requirements

- 64-bit Python 3.11–3.13; development currently uses Python 3.12.
- Windows and the broker's MT5 terminal for terminal integration.
- An isolated virtual environment for project dependencies.

Offline backtests and core calculations do not require a running MT5 terminal.

## Setup

Run these commands from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"

```

For offline development without the MT5 integration package:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .

```

If `python` opens Microsoft Store after installation, close all VS Code windows and reopen the application to refresh PATH.

## Run a robot

Follow the selected robot's guide in [docs/robots](docs/robots) for its configuration, diagnostics, backtests, and execution commands. Commands are run from the repository root unless a guide says otherwise.

The current implementation has a single robot-specific executable. Shared multi-robot orchestration and coordinated portfolio risk management are not implemented. Do not run separate robots against the same account without explicit exposure coordination.

## Development

- Keep strategy-specific behavior and documentation separate from shared infrastructure.
- Add focused offline tests for strategy rules, risk calculations, execution safeguards, and persistence.
- Start in observation or simulation mode. Order execution must require explicit opt-in.
- Keep broker credentials in the terminal or an appropriate local secret store, never in source code.
- Keep local configuration, runtime state, logs, market data, and reports out of commits.

Run the offline test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

```

Tests do not require a terminal connection and never send orders. CI runs them on Windows only for pull requests targeting the default branch, `main`; pushes do not trigger the workflow.

## Trading risk

No strategy guarantees profit, including a daily profit target. BijiSatu supports selectable M15/H1 intraday and legacy M1/M5 scalping research; see its guide for rules and config-aware offline tests. Validate behavior with realistic transaction costs, out-of-sample data, and demo forward testing before considering real-money execution.

Cent accounts use real money. Gaps, slippage, spread changes, commission, and swap can cause losses beyond calculated limits. Stopping a local process does not necessarily close broker positions; consult the robot's documented shutdown behavior.

