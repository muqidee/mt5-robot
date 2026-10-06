# BijiSatu

An experimental **Python + MetaTrader 5** scalping robot for Windows. It uses a testable, rule-based strategy with no guarantee of profit. An Exness cent account uses **real money**; it is not automatically a demo account.

## Initial release

- M5 EMA20/EMA50 trend filter, M1 EMA20 pullback/reclaim entries, and ATR14. Signals use closed candles only.
- Initial stop loss at 1.5 × ATR, widened where required by broker limits and spread. Take profit targets 1.5 × the actual stop distance. No martingale, grid, averaging down, or trade-count targets.
- Discovers major-currency Forex pairs and gold from broker metadata, including broker suffixes. Scans up to 20 symbols by default, with one variant per pair. Set `symbols` to restrict trading to instruments you have tested.
- Ranks valid setups by spread/ATR, with a maximum spread of 15% of ATR. Allows up to two positions, one per symbol, with a five-minute cooldown per symbol.
- Sizes positions using MT5 profit/loss calculations in **account currency**, broker volume limits, remaining daily risk, estimated commission, deviation, and a 15% buffer. It does not assume cent-account lots are equivalent to USD-account lots.
- **Observation only (`dry-run`) by default**: no orders, virtual positions, or simulated profits. Use `backtest` for offline simulation.
- Profitability has not been validated against Exness data or broker forward tests. This is not a high-frequency trading system.

## Risk settings

The requested defaults are **up to 3% of equity per entry and a 5% daily loss limit**. These are aggressive limits, not risk targets to exhaust. A risk of 0.25–0.5% per entry is more conservative for initial testing.

Daily loss measures the decline in **total account equity** from the broker-day baseline, including realized and floating PnL. Additional risk from open positions is reserved before sizing another entry. New entries are reduced or skipped when the remaining budget is insufficient; minimum lot sizes are never forced.

Once the limit is reached, the halt is saved and **no new entries are allowed until the next day**, even if equity recovers or the process restarts. Existing positions are **not forcibly closed** and retain their server-side SL/TP. This release does not trail or move stops.

**Gaps, slippage, widening spreads, commission, swap, and open positions can push actual losses beyond 5%.** A stop loss does not guarantee an execution price. Calculated risk is not an absolute loss cap.

### Broker day and persistent state

- Set `broker_utc_offset_hours` to the broker server's UTC offset, accounting for daylight-saving changes where applicable. The MT5 Python API does not reliably expose the broker timezone; do not infer it from the Windows clock.
- The baseline is the first equity observation of the day, not an exact reconstruction of equity at midnight.
- On initial startup or after being offline across midnight, entries are refused if existing positions or trades prevent a trustworthy baseline. Start on a clean trading day with no open positions.
- A continuously running process can establish a new baseline near midnight, including with open positions, when observations fall within 30 seconds before and after the day boundary.
- Deposits, withdrawals, or balance adjustments after the baseline halt new entries until the next day so that cash flows cannot mask losses. Account history must be available.
- Use a **dedicated BijiSatu account**. Manual positions, positions belonging to other strategies, pending orders, and positions without a stop loss block new entries.
- State is stored in `state/`, separately for each account and mode. Do not delete, edit, or relocate it, or change the timezone or magic number while trading. Account locks in `%LOCALAPPDATA%\BijiSatu\locks` prevent concurrent instances under the same Windows user on one host; they do not coordinate separate VPS instances or Windows users.
- Account/API failures that prevent safe operation stop the process without closing positions; individual symbol or entry failures may instead be skipped. Check the terminal before restarting. There is no automatic reconnection or order retry. A missing, unknown, or ambiguous order result halts entries until the next day and requires manual inspection; the robot does not assume that an unrecognized response means rejection.

## Setup and diagnostics

Complete the shared [repository setup](../../README.md#setup) first. Run all commands below from the repository root.

```powershell
Copy-Item config.example.json config.local.json
.\.venv\Scripts\python.exe -m bijisatu doctor

```

`doctor` checks the environment only. It **does not** open MT5 or send orders.

## Launcher

Use [run-bijisatu.ps1](../../run-bijisatu.ps1) as the PowerShell entry point, equivalent to a small shell script on Linux. It forwards arguments to [scripts/run_bijisatu.py](../../scripts/run_bijisatu.py), which handles confirmation and execution permission. Run these commands from the repository root. The launcher always uses the project's virtual environment and resolves relative configuration paths from the project root.

Continuous observation, with no orders:

```powershell
.\run-bijisatu.ps1 --verbose

```

Order execution, requiring you to type `TRADE` at the confirmation prompt:

```powershell
.\run-bijisatu.ps1 --execute --verbose

```

The second command can place real-money trades. It does not change risk settings or bypass the robot's configuration checks. Execution permission is passed only to the child process; the parent PowerShell environment is not modified. In observation mode, inherited execution permission is explicitly removed from the child environment.

Add `--once` for a single scan. Use `--config path` for a different configuration file. The default local configuration is copied from the example only when absent; an existing configuration is never overwritten. A missing custom configuration or virtual environment stops the launcher with an error.

Press Ctrl+C to stop. Existing broker positions are not automatically closed. Do not start another copy while a robot is already running on the same account.

## Connect MT5 for observation

1. Install the broker's MT5 terminal and sign in to the intended account there. Keep passwords in the terminal, never in the robot's source or configuration.
2. Copy [config.example.json](../../config.example.json) to `config.local.json`, which Git ignores. Set `account_login`, the broker timezone, and actual trading costs. Set `terminal_path` when multiple terminals are installed.
3. `symbols: []` enables automatic discovery. To restrict instruments, use the broker's **exact** symbol names, including the appropriate cent-account suffix where applicable. Suffixes vary between accounts.
4. Keep MT5 open and connected, with sufficient M1/M5 history available. You do not need to open a chart for every symbol: the robot selects configured symbols in Market Watch and requests M1/M5 data through the terminal API. The strategy needs at least 100 closed candles per timeframe; the runtime requests 300. A newly selected symbol may need time to synchronize quotes and history. If history remains missing, open that symbol's M1/M5 charts temporarily to load it. The active chart timeframe does not control the robot.

```powershell
.\.venv\Scripts\python.exe -m bijisatu run --config config.local.json --once
.\.venv\Scripts\python.exe -m bijisatu run --config config.local.json
```

Without `--execute`, no orders are sent. Use `--verbose` to inspect skipped symbols. Stale quotes, incomplete data, closed markets, expensive spreads, or missing setups may prevent entries; that is expected.

`commission_per_lot` estimates the **total opening and closing commission per lot in account-currency units**. Set it to `0` only when there is genuinely no commission. If costs differ by symbol, use a conservative estimate or restrict the symbol list. Do not apply USD commission figures directly to a USC account. MT5 calculates account-currency PnL for runtime sizing, but does not provide every commission charge before an order is placed.

## Logs and market sessions

The terminal uses readable headings and labelled details, not JSON. Every header includes UTC time, the execution mode, and all currently active session labels. Status reports show equity, balance, daily loss, remaining risk, open positions, and skipped-entry reasons. A heartbeat is printed every 60 seconds even when the status has not changed.

Use `--verbose` to show per-symbol diagnostics in the terminal, including bid/ask, spread in points, tick age and its limit, candle counts and ages, signal candidates, cooldowns, and rejected entries. Stale quotes and future timestamps have separate explanations. A missing trend/pullback setup is not a connection error.

Example terminal output:

```text
2026-10-06 04:56:05 UTC | INFO | Mode: dry-run | Session: Sydney + Tokyo | Runtime status
  Status: waiting
  Symbols scanned: 1
  Skipped reasons:
    No trend pullback setup: 1
  Account:
    Currency: USC
    Equity: 4,060.65 USC
    Daily loss: 0.00 USC
    Daily loss limit: 203.03 USC
    Remaining risk: 203.03 USC
    Open positions: 0

```

The example is illustrative, not a performance report. Terminal timestamps now explicitly use **UTC**, which may differ from the Windows clock. The configured broker offset is recorded separately and continues to control daily risk accounting.

### Daily files

Detailed records are written automatically to `logs/bijisatu/YYYY-MM-DD.jsonl`, using the **UTC date**. Each line is a JSON object for later analysis; JSON is used in the file only, not the terminal. Debug-level diagnostics are saved even without `--verbose`.

- Files are append-only across restarts, rotate at UTC midnight, and are flushed after each record.
- **No automatic deletion.** Monitor disk usage and archive old files manually when appropriate.
- Every record identifies the run, process, mode, UTC time, session labels, and event. Account identifiers and credentials are not deliberately recorded; avoid adding secrets to log messages.
- Relevant events include `account_connected`, `daily_baseline`, `symbol_skip`, `signal_candidate`, `scan_completed`, `runtime_status`, `heartbeat`, `runtime_error`, and `shutdown`.
- Dry-run plans are proposed trades, not fills or realized profits. These logs cannot establish profitability by themselves.
- Logging failures stop the process rather than silently continuing without an audit trail. Existing server-side SL/TP are not removed.
- The default `logs/` directory is excluded from Git. If you choose a different directory, exclude that location as well.

These optional settings can be added to local configuration; existing configurations inherit the defaults:

```json
{
  "log_dir": "logs",
  "heartbeat_seconds": 60
}

```

These are additional fields, not a replacement for the rest of the configuration. Restart the running process after changing configuration or updating the code.

### Session labels

Session labels follow these indicative local weekday hours, with IANA timezone rules handling daylight-saving changes:

| Session | Local hours | Timezone |
| --- | --- | --- |
| Sydney | 08:00–17:00 | Australia/Sydney |
| Tokyo | 09:00–18:00 | Asia/Tokyo |
| London | 08:00–17:00 | Europe/London |
| New York | 08:00–17:00 | America/New_York |

Overlaps display all active labels. These labels are **informational only**: they do not change entry rules, account for holidays, or guarantee that a broker symbol is trading. Quote freshness and broker checks remain in force. Keep timezone data current when daylight-saving rules change.

## Order execution (opt-in)

Run out-of-sample backtests and demo forward tests first. Cent accounts reduce nominal exposure, not the existence of risk. A few winning trades are not sufficient grounds to switch to a USD account.

Execution requires **all three fields** `account_login`, `broker_utc_offset_hours`, and `commission_per_lot`, the `--execute` flag, and the environment variable below. Enable algorithmic trading and trading through the external Python API in MT5. Both the terminal and Python must remain running for new entries; accepted server-side SL/TP remain active if Python stops.

```powershell
$env:BIJISATU_ALLOW_ORDERS = "YES"
.\.venv\Scripts\python.exe -m bijisatu run --config config.local.json --execute
```

This command sends actual orders to the configured account. To remove permission from the current PowerShell session:

```powershell
Remove-Item Env:BIJISATU_ALLOW_ORDERS -ErrorAction SilentlyContinue
```

Press Ctrl+C to stop the robot. Removing the environment variable in another shell does not stop an already running process. **Stopping the robot does not close broker positions.**

## Offline CSV backtesting

Provide **M1 bid OHLC** candles with the CSV header `time,open,high,low,close`. Use UNIX seconds in UTC or ISO8601 timestamps with an explicit timezone. Timestamps must fall on minute boundaries, be unique, and increase strictly. M5 candles are built only from five complete, consecutive M1 candles. Market data is not downloaded automatically.

Example data format, not a dataset for evaluating profitability:

```csv
time,open,high,low,close
2026-01-05T00:00:00Z,1.1700,1.1702,1.1699,1.1701
2026-01-05T00:01:00Z,1.1701,1.1703,1.1700,1.1702
```

The following parameters are **illustrative**, not Exness account specifications. Match spread, slippage, commission, volume limits, currency, and contract value to the actual symbol and account:

```powershell
.\.venv\Scripts\python.exe -m bijisatu backtest .\data\EURUSD-M1.csv --equity 10000 --spread 0.00010 --slippage 0.00002 --commission-per-lot 0 --value-per-price-unit 100000 --broker-utc-offset 0
```

- Spread and slippage are expressed in **price units**, not points or pips. `value-per-price-unit` is account-currency PnL for a 1.0 price move on one lot.
- Signals use closed candles, with entry at the next candle's open. If both SL and TP are touched in the same candle, the simulator assumes SL first. Stop gaps fill at the worse price.
- The final open position is liquidated for reporting and marked as a forced exit. JSON output includes trades, statistics, and assumptions.
- This is a single-symbol simulator, not a multi-pair portfolio backtest. It assumes constant spread, slippage, and contract-value conversion. It does not model tick-by-tick execution, liquidity, partial fills, news calendars, actual latency, swap, or broker margin. Results are **not identical** to MT5 runtime behavior and do not establish live profitability.
- Evaluate profit after costs, drawdown, profit factor, sample size, stability across market periods, and out-of-sample performance. Trade count or win rate alone is insufficient.

## Tests

Tests do not require a terminal connection and never send orders.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

CI runs offline tests on Windows. Local configuration, state, logs, market data, and reports are excluded from commits. Never store trading passwords or tokens in this repository.

