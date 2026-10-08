# BijiSatu

An experimental **Python + MetaTrader 5** short-term trading robot for Windows, with selectable intraday and legacy scalping modes. It uses a testable, rule-based strategy with no guarantee of profit. An Exness cent account uses **real money**; it is not automatically a demo account.

## Strategy modes

`strategy_mode: "scalping"` preserves the original M1-entry/M5-trend default for existing configurations. `"intraday"` selects **closed M15 entries with H1 trend**. Slower candles may reduce sensitivity to minute-level noise and spread relative to ATR, but this is a hypothesis to test, not proven profitability or a guarantee of daily profit. No trade is required each day.

Both modes reuse the same rules, with no extra indicators:

1. Require at least 100 closed entry candles and 100 trend candles that closed no later than the entry candle's close. Intraday evaluates the latest 100 of each, matching the offline simulation; legacy live scalping retains its existing available history (up to 300), while offline uses 100.
2. For a buy, the last three trend closes must be above EMA20, EMA20 above EMA50, and both EMAs rising on each candle. A sell mirrors these conditions.
3. The previous entry close must be at/below its EMA20 for a buy, followed by a bullish candle closing above its own EMA20. A sell requires the opposite reclaim and a bearish candle.
4. ATR14 uses Wilder smoothing on the entry timeframe (M15 or M1). Spread, stop distance and sizing use that ATR; signals never use an unfinished H1/M5 candle.
5. Reject future or stale closed candles: entry close age must be within its duration plus 30 seconds, trend within its duration plus 90 seconds. Thus intraday limits are 930/3690 seconds and scalping 90/390 seconds. Quotes still have the separate 15-second default limit. Recheck entry signal and trend age before claiming an attempt; saved bar timestamps prevent duplicates and cooldown remains per symbol across restarts/mode changes.

Intraday describes the entry horizon, **not a forced same-day exit**. Positions remain until server SL/TP; no end-of-day liquidation or maximum holding-time rule is implemented. Consider overnight gaps and swap in research.

### Execution and risk controls

- EMA20/EMA50 trend filter, EMA20 pullback/reclaim entries, and ATR14. Signals use closed candles only.
- Initial stop loss at 1.5 × ATR, widened where required by broker limits and spread. Take profit targets 1.5 × the actual stop distance. No martingale, grid, averaging down, or trade-count targets.
- Discovers major-currency Forex pairs and gold from broker metadata, including broker suffixes. Scans up to 20 symbols by default, with one variant per pair. Set `symbols` to restrict trading to instruments you have tested.
- Ranks valid setups by spread/ATR, with a maximum spread of 15% of ATR. The compatibility default permits two positions and a five-minute per-symbol cooldown; the local intraday profile permits up to ten with a fifteen-minute cooldown. One position per symbol remains enforced.
- Sizes positions using MT5 profit/loss calculations in **account currency**, broker volume limits, remaining daily risk, estimated commission, deviation, and a 15% buffer. It does not assume cent-account lots are equivalent to USD-account lots.
- **Observation only (`dry-run`) by default**: no orders, virtual positions, or simulated profits. Use `backtest` for offline simulation.
- Profitability has not been validated against Exness data or broker forward tests. This is not a high-frequency trading system.

## Optional Ollama entry filter

The entry filter is **disabled by default** and must be explicitly selected separately from existing observation. It never authorizes execution by itself: `--execute`, the environment lock, account configuration and all broker/risk guards remain required. Use these mutually exclusive flags in the existing configuration:

```json
{
  "ollama_filter_enabled": true,
  "ollama_observation_enabled": false
}
```

Do not enable the filter in a running trading setup until offline mocked filter tests **and a synthetic, real structured response through the exact production client** pass. A healthy service or installed model alone is insufficient. Current CPU Qwen validation timed out at 30 seconds; local observation remains unchanged and filter activation is blocked. No MT5 connection, order, model download or live restart is part of this validation.

A valid strategy setup **AND** an exact Qwen `allow` are required. Qwen is veto-only: it cannot invent trades, change lot size, SL/TP, risk limits or execution settings. `skip`, pending, timeout, malformed response, service failure, full queue, absent client or expired verdict means **no new order**. Filter mode applies equally to dry-run plans and execution, but dry-run still never sends orders.

The engine never waits for inference. One bounded worker queues market-only snapshots, and later scans look up results. Requests are deduplicated per symbol/bar, including failed/saturated requests, without recording an order attempt. The approval binds symbol, side, bar, strategy mode/timeframes, model, endpoint and the exact canonical snapshot (including spread and bounded closed candles). Changed snapshots cannot reuse it. Approval expires **30 seconds from enqueue**, not from response completion; candle-age checks may invalidate it sooner. Result memory is capped by `max_symbols`, with one latest record per symbol; obsolete queued work cannot approve a new bar. A slow CPU can therefore prevent all entries rather than silently bypass the filter.

After lookup, the engine refreshes equity, positions, daily/open budgets and quotes. Before real execution, broker preflight (including margin and `order_check`) must pass; the engine then revalidates cash flow, day, signal/approval age, current exposure/budget and quote before the durable claim immediately preceding `order_send`. Uncertain acknowledgements still halt and cannot be retried using an approval. Pending/unavailable results do not halt or reset the daily baseline. Server-side SL/TP management remains unchanged.

Shared `ollama_observation_endpoint`, `ollama_observation_model`, `ollama_observation_timeout_seconds` and `ollama_observation_queue_capacity` configure either selected mode; their old names preserve compatibility. Both flags cannot be true. Endpoint restrictions, strict response schema, bounded request/response size, no proxies/redirects and bounded shutdown join remain enforced. Only market data is sent; no account identity, equity, credentials or lots. Filter reasons are sanitized structured fields; raw responses/exceptions are never logged.

Startup reports mode `off`, `observe` or `filter`. Filter events are `ollama_filter_started`, `ollama_filter_queued`, `ollama_filter_pending`, `ollama_filter_allowed`, `ollama_filter_rejected` and `ollama_filter_unavailable`. Missing startup client remains fail-closed, including when constructing `Engine` directly. Filter accuracy/profit benefits are **unproven**. Offline CSV strategy backtests **do not simulate Qwen or test this filter**; they cannot demonstrate filter performance.

## Optional Ollama observation only

Ollama commentary is **disabled by default** and independent of `--execute`. Opting in does not authorize orders or change dry-run behavior. Qwen judgements never filter a signal, alter exposure/risk, write order claims, or send/modify orders. Existing pre-entry exposure and freshness checks remain authoritative. Profit benefits are **unproven**; no live performance improvement is claimed.

The ignored `config.local.json` opts in with these settings only; existing account, symbols and risk settings are preserved:

```json
{
  "ollama_observation_enabled": true,
  "ollama_observation_endpoint": "http://127.0.0.1:11435",
  "ollama_observation_model": "qwen3:4b",
  "ollama_observation_timeout_seconds": 10.0,
  "ollama_observation_queue_capacity": 4
}

```

The example/schema defaults use the same values except `ollama_observation_enabled: false`. Settings load only at startup; no process or terminal was started/restarted for this integration. The endpoint must be an HTTP origin using `localhost` or a literal loopback address (including `[::1]`), optional valid port, and no credentials, API path, query or fragment. `localhost` connects directly to `127.0.0.1`; no DNS, environment proxies or redirects are used. The model must already be installed; the client does not download it.

One background worker posts `/api/chat` with `stream: false`, `think: false`, a JSON response schema, temperature 0, fixed seed and a modest fixed preset (`num_predict: 64`, `num_ctx: 2048`). Qwen versions must support these options; unsupported requests only produce an observation error. Each request contains symbol, side, entry-bar open timestamp, timeframe minutes, ATR, price spread, verified EMA trend/pullback context, and at most six closed OHLC candles per timeframe. Trend candles close no later than the entry cutoff. No account identifiers, credentials, equity, lot sizing, repository files, configuration or persistent state are sent. No tools or execution authority are provided.

The configured queue holds at most four pending snapshots by default plus one in flight. Submission never waits for inference: queue saturation, timeouts, service outages, malformed replies and model `skip` decisions do not suppress valid orders or modify their plans. A process-local per-symbol bar watermark prevents repeated requests for the same or older signal, including failed/saturated observations; it is not a durable trading claim and resets on restart. Symbol tracking is capped at 1,024 without eviction; excess symbols only log unavailable. Signals are observed during scanning before spread/entry sizing, so a judgement does not imply an order was placed or even eligible. Account-wide blocks and cooldowns may prevent a symbol from reaching evaluation.

Structured events in the existing daily logs:

- `ollama_observation_started`: explicitly observation only, never an execution filter; account startup details also report enabled/active status.
- `ollama_observation_queued`: symbol, side and bar timestamp scheduled once.
- `ollama_observation_result`: `decision` (`allow`/`skip`), printable reason of at most 160 characters, and request latency. These are untrusted commentary, **not execution decisions**.
- `ollama_observation_error`: snapshot/scheduling/response rejected, with a fixed reason code, not raw response/exception text.
- `ollama_observation_unavailable`: connection failure, timeout, queue/symbol capacity, startup failure or shutdown cancellation. No retries for that bar.

Response bodies are capped at 8 KiB, headers/transport reads are bounded, and judgement keys/types/enums are strictly checked; duplicate keys, nonfinite numbers, tool calls and control characters are rejected. Shutdown cancels pending snapshots, waits at most the request deadline plus 0.2 seconds for the single worker, and disables its logging before logging handlers close. It never waits for the whole queue. A one-cycle run may cancel pending observations rather than report every queued result.

CPU inference and model warmup can exceed the default 10-second total request deadline (allowed 0.1–30 seconds). An independent local synthetic `/api/chat` check with `qwen3:4b`, thinking/streaming disabled, temperature 0, `num_predict: 96` and `num_ctx: 2048` timed out after 90.05 seconds; the data were entirely invented, with no market/account data. A second warm-model request with only a minimal invented JSON instruction, `think: false`, `num_predict: 48` and `num_ctx: 2048` also timed out at 30 seconds. `/api/ps` reported `qwen3:4b` loaded with Q4_K_M quantization, context 2048 and `size_vram: 0` (CPU); `/api/version` remained responsive and reported version `0.40.0`. These checks establish service reachability and model loading only: **no usable model judgement has been verified**. Automated integration tests use mocked HTTP/inference, not successful live generation. Repeated expensive synthetic generation calls were not pursued. The smaller 64-token preset is a bound, not a verified speed improvement; truncated/malformed replies are rejected. The observation timeout is not increased to accommodate this measurement. Queue saturation and timeouts are expected with observation enabled; scanning and order calculations never wait for inference, and logs can arrive after scanning or an order event. This observation path does not establish an entry-time performance benefit, trading accuracy or profitability.

## Risk settings

The compatibility defaults remain **up to 3% of equity per entry and a 5% daily loss limit**. These are aggressive limits, not risk targets to exhaust. The example JSON intentionally retains these defaults.

The ignored local configuration has been changed to a **research/test profile**, not approved live settings: `strategy_mode="intraday"`, `risk_fraction=0.01` (1% per entry), `max_open_risk_fraction=0.05` (5% of current equity combined open risk), `max_positions=10`, `daily_loss_fraction=0.05` (5% of the saved daily baseline), `cooldown_seconds=900`, `max_spread_atr=0.15`, `stop_atr=1.5`, `reward_ratio=1.5`. Other account, broker, symbol, commission and persistence settings are unchanged. No running process was restarted and no terminal connection or orders were made as part of this change. Configuration is read only at startup; a running instance still uses its old settings.

Changing mode/risk must **never** delete/reset state to bypass a daily halt. The configured daily threshold is applied against the existing saved baseline on the next scan after settings are loaded; previously latched halts remain latched. Stops on existing positions are not modified. Test out-of-sample with realistic spread, adverse slippage and account-currency commission before any demo forward evaluation. Neither wider ATR stops nor reduced risk establish a profitable edge.

The local **5% daily loss protection is a chosen safeguard**, not an additional user-selected portfolio target. It replaces the earlier 2% test setting so that a hidden 2% daily allocation does not defeat the requested 5% open-risk ceiling. Daily limits still take priority when losses consume the day's budget.

The engine sends at most **one order per scan**, not one transaction overall or per day. Other qualifying symbols can enter on subsequent scans after exposure is refreshed; there are no simultaneous/batch sends. Up to ten positions is a hard upper bound, not a target or a count derived simply from account size. Their affordable number depends on current equity, lot minimums, stop distances, costs and existing exposure; with five full 1%-risk entries the 5% ceiling may already be exhausted. Smaller entries or reduced reserved exposure can allow more, still never exceeding ten or one per symbol.

Existing open-risk reservations sum loss from current bid/ask marks to server stops, estimated round-trip commission and the configured buffer. Candidate sizing also includes allowed adverse deviation. On both the initial scan and pre-entry refresh, the candidate budget is the smaller of:

- `max(0, saved_daily_baseline * daily_loss_fraction - current_daily_equity_loss - reserved_open_risk)`;
- `max(0, current_equity * max_open_risk_fraction - reserved_open_risk)`.

Entry sizing is additionally capped by `current_equity * risk_fraction`. If equity falls and existing risk exceeds the open-risk ceiling, new entries stop; existing positions/stops are not resized, widened or closed. This is a planned-risk gate, not a guaranteed realized-loss bound. Status reports expose the open-risk limit, remaining daily/open capacities and their effective minimum.

Daily loss measures the decline in **total account equity** from the broker-day baseline, including realized and floating PnL. Additional risk from open positions is reserved before sizing another entry. New entries are reduced or skipped when the remaining budget is insufficient; minimum lot sizes are never forced.

Once the limit is reached, the halt is saved and **no new entries are allowed until the next day**, even if equity recovers or the process restarts. Existing positions are **not forcibly closed** and retain their server-side SL/TP. This release does not trail or move stops.

**Gaps, slippage, widening spreads, commission, swap, and open positions can push actual losses beyond the configured daily threshold.** A stop loss does not guarantee an execution price. Calculated risk is not an absolute loss cap.

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

## Configuration reference

In VS Code, open [config.example.json](../../config.example.json) or your local configuration and **hover over a key** to see its meaning, units, default, and caveats. Press Ctrl+Space for completion. The workspace associates both files with [config.schema.json](../../config.schema.json), which also highlights unknown keys and invalid types or ranges. The runtime still performs the authoritative checks, including cross-field and execution requirements.

Keep configuration as standard JSON: do not add `//` comments, comment keys, or a `$schema` field to the runtime files. A key names a setting; its value supplies the chosen value. Missing keys use defaults. `null` means unset, `[]` is an empty list, and `0.03` is a fraction equal to 3%, not a lot size. Edit the ignored local configuration rather than the example. Changes take effect only after restarting the process.

### Risk and order sizing

| Key | Default | Meaning |
| --- | --- | --- |
| `risk_fraction` | `0.03` | Maximum planned entry risk as a fraction of current equity (3%). Must not exceed the daily fraction; remaining budget can reduce it. |
| `daily_loss_fraction` | `0.05` | Daily account-equity loss threshold (5% of the saved baseline). Stops new entries, not existing positions. |
| `max_open_risk_fraction` | `0.05` | Combined buffered risk to stops plus commission capped at 5% of current equity, independently of daily loss protection. Unused capacity caps each candidate. |
| `max_positions` | `2` | Maximum simultaneous positions, an integer from 1 to the absolute cap of 10; also limited to one per symbol and remaining daily/open-risk capacity. |
| `stop_atr` | `1.5` | Initial SL distance as a multiple of entry-timeframe ATR14 (M1 scalping, M15 intraday). Broker rules, spread and rounding may widen it. |
| `reward_ratio` | `1.5` | TP distance divided by actual entry-to-SL distance, before costs. Not a guaranteed return. |
| `risk_buffer` | `1.15` | Applies a 15% cushion to estimated risk when sizing/reserving exposure; a larger value generally reduces lot size. |
| `max_margin_fraction` | `0.8` | Rejects an order needing more than 80% of available free margin. Not an 80% loss allowance. |
| `deviation_points` | `10` | Entry deviation allowance in broker points, also used in the risk estimate. Ten points equal one pip on five-decimal EURUSD; execution is not guaranteed at that deviation. |
| `commission_per_lot` | `null` | Total opening plus closing commission per lot in account-currency units. Explicit `0` assumes no commission; spread/swap remain separate. An explicit nonnegative value is required for execution. |

### Scanning and entry filters

| Key | Default | Meaning |
| --- | --- | --- |
| `strategy_mode` | `"scalping"` | `scalping`: M1 entry/M5 trend; `intraday`: M15 entry/H1 trend. No guaranteed daily profit or same-day exit. |
| `symbols` | `[]` | Exact broker names to scan. Empty enables metadata-based discovery. A selected list makes the tested instruments explicit. |
| `max_symbols` | `20` | Scan-list limit, also applied to explicit symbol lists. Not a number of required trades. |
| `poll_seconds` | `5` | Sleep after each scan; processing adds to the actual interval. Not a timeframe or trade-frequency target. |
| `cooldown_seconds` | `300` | Five-minute wait between attempts on the same symbol, including saved dry-run or failed attempts. Not a maximum holding time. |
| `max_tick_age_seconds` | `15` | Rejects quotes older than 15 seconds; unaffected by the future-tick tolerance. |
| `max_tick_future_seconds` | `1.0` | Accepts quotes up to one second ahead of the local UTC clock to tolerate small clock differences. Allowed range is 0–1; use 0 for strict rejection. Larger leads are rejected; closed-candle validation is unchanged. |
| `max_spread_atr` | `0.15` | Requires spread / entry-timeframe ATR14 to be at most 15%. This is a ratio, not a pip amount. |

For example, a 0.8-pip spread with 1.6-pip ATR produces a ratio of `0.50`. A `max_spread_atr` value of `0.15` rejects that entry, even if the spread would otherwise look small. Increasing the limit admits higher relative trading costs; test changes offline rather than assuming that more entries will be more profitable.

### Account and terminal

| Key | Default | Meaning |
| --- | --- | --- |
| `account_login` | `null` | Expected MT5 account number, not a password. Required for execution; keep the real number in local configuration only. |
| `broker_utc_offset_hours` | `null` | Server offset used for daily risk resets: `0` means UTC, `2` means UTC+2. Observation falls back to UTC when unset; execution requires a value. |
| `terminal_path` | `null` | Optional MT5 executable path to select a specific terminal. Windows backslashes must be escaped in JSON. Unset uses default terminal discovery. |
| `magic` | `810031` | Identifier attached to this robot's orders and positions. Keep stable while positions remain open. |

### State and logging

| Key | Default | Meaning |
| --- | --- | --- |
| `state_dir` | `"state"` | Durable baselines, daily halt flags and previous attempts. Do not remove or relocate it to reset limits. |
| `log_dir` | `"logs"` | Daily JSONL base directory. Files are saved under `bijisatu/` and are not automatically deleted. |
| `heartbeat_seconds` | `60` | Repeats unchanged status approximately once per minute, checked after scans. Does not alter trading rules. |
| `ollama_filter_enabled` | `false` | Opt in to fail-closed strategy AND exact fresh Qwen approval. Mutually exclusive with observation; never grants execute permission. Validate synthetic production responses before activation. |
| `ollama_observation_enabled` | `false` | Opt in to local asynchronous commentary only. Never filters entries or authorizes orders; independent of execute/dry-run. Cannot be true with filter enabled. |
| `ollama_observation_endpoint` | `"http://127.0.0.1:11435"` | Loopback HTTP origin only; no credentials, path, query, fragment, DNS, proxies or redirects. `/api/chat` is appended internally. |
| `ollama_observation_model` | `"qwen3:4b"` | Installed local model; 1–128 characters matching letters/digits then letters/digits/dot/underscore/colon/slash/hyphen. No automatic download. |
| `ollama_observation_timeout_seconds` | `10.0` | Shared total request deadline, 0.1–30 seconds. CPU latency may exceed it: observation only logs failures; filter blocks entries. Filter approval expires 30 seconds from enqueue. |
| `ollama_observation_queue_capacity` | `4` | Pending snapshot cap, integer 1–32, plus one active request. Full queues log unavailable without blocking or retrying that signal. |

Relative directory paths use the process working directory; the launcher sets it to the repository root. If directories are customized, update Git exclusions too. These descriptions do not change configured values, trading permissions, or risk limits. Gaps and slippage can still exceed calculated loss thresholds.

## Launcher

Use [run-bijisatu.ps1](../../run-bijisatu.ps1) as the PowerShell entry point, equivalent to a small shell script on Linux. It forwards arguments to [scripts/run_bijisatu.py](../../scripts/run_bijisatu.py), which handles confirmation and execution permission. Run these commands from the repository root. The launcher always uses the project's virtual environment and resolves relative configuration paths from the project root.

Continuous observation, with no orders:

```powershell
.\run-bijisatu.ps1 --verbose

```

Order execution, requiring you to select `1` at the numbered confirmation prompt (`2`, Enter, or any other input cancels):

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
4. Keep MT5 open and connected, with sufficient history for the selected timeframes (M15/H1 intraday, M1/M5 scalping). You do not need to open a chart for every symbol: the robot selects configured symbols in Market Watch and requests those timeframes through the terminal API. The strategy needs at least 100 closed candles per timeframe; the runtime requests 300. A newly selected symbol may need time to synchronize quotes and history. If history remains missing, open that symbol's selected timeframe charts to load it. The active chart timeframe does not control the robot.

```powershell
.\.venv\Scripts\python.exe -m bijisatu run --config config.local.json --once
.\.venv\Scripts\python.exe -m bijisatu run --config config.local.json
```

Without `--execute`, no orders are sent. Use `--verbose` to inspect skipped symbols. Stale quotes, incomplete data, closed markets, expensive spreads, or missing setups may prevent entries; that is expected.

`commission_per_lot` estimates the **total opening and closing commission per lot in account-currency units**. Set it to `0` only when there is genuinely no commission. If costs differ by symbol, use a conservative estimate or restrict the symbol list. Do not apply USD commission figures directly to a USC account. MT5 calculates account-currency PnL for runtime sizing, but does not provide every commission charge before an order is placed.

## Logs and market sessions

The terminal uses readable headings and labelled details, not JSON. Every header includes UTC time, the execution mode, and all currently active session labels. Status reports show equity, balance, daily loss, remaining risk, open positions, and skipped-entry reasons. A heartbeat is printed every 60 seconds even when the status has not changed.

Use `--verbose` to show per-symbol diagnostics in the terminal, including bid/ask, spread in points, tick age and its limits, candle counts and ages, signal candidates, cooldowns, and rejected entries. Stale quotes and future timestamps have separate explanations. A missing trend/pullback setup is not a connection error.

A small quote timestamp lead (for example 0.312 seconds) is accepted within `max_tick_future_seconds`, defaulting to one second. This applies when scanning, refreshing an entry, and reserving risk for existing positions. Larger leads and stale quotes are still rejected. If a timestamp fails while reserving open-position risk or refreshing an entry, the scan returns `blocked` with a `quote_time_blocked` warning and retries after `poll_seconds`; it does not use that quote, submit an order, claim an attempt, or reset the daily baseline. The process keeps running and recalculates exposure when valid data return. Other broker failures, invalid prices, missing stops, and uncertain order results retain their existing safeguards. Keep Windows time synchronized; this tolerance does not correct clock drift or relax the 15-second stale-quote limit. UK daylight-saving display time (BST, UTC+1) does not change UTC timestamp comparisons. If Windows Time is stopped, an administrator can start the service with `Start-Service W32Time`, request `w32tm /resync`, and inspect `w32tm /query /status`. Changes require a process restart; existing broker positions and saved daily state must not be reset.

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

Provide **M1 bid OHLC** candles with the CSV header `time,open,high,low,close`. Use UNIX seconds in UTC or ISO8601 timestamps with an explicit timezone. Timestamps must fall on minute boundaries, be unique, and increase strictly. The selected entry/trend candles are built only from complete, contiguous, UTC-boundary-aligned M1 groups (1/5 minutes scalping, 15/60 minutes intraday). Missing minutes invalidate their group, and incomplete higher-timeframe candles do not count toward warmup. Intraday needs at least 100 complete H1 candles (6,000 contiguous M1 rows when aligned), not merely 100 M1 rows. Market data is not downloaded automatically.

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
Use `--config config.local.json` to test the chosen local strategy and risk profile:

```powershell
.\.venv\Scripts\python.exe -m bijisatu backtest .\data\EURUSD-M1.csv --config config.local.json --equity 10000 --spread 0.00010 --slippage 0.00002 --value-per-price-unit 100000

```

`--config` supplies `strategy_mode`, `stop_atr`, `reward_ratio`, `max_spread_atr`, `cooldown_seconds`, `risk_fraction`, `daily_loss_fraction`, `max_open_risk_fraction`, `risk_buffer`, configured commission and broker UTC offset. Explicit CLI flags override those settings (`--strategy-mode`, `--stop-atr`, `--reward-ratio`, `--max-spread-atr`, `--cooldown-seconds`, `--risk-buffer`, `--risk`, `--daily-loss`, `--max-open-risk`, `--commission-per-lot`, `--broker-utc-offset`). Commission must be configured or supplied explicitly. Spread, slippage and contract conversion remain explicit simulation assumptions, not downloaded broker data. Without config, legacy backtest defaults remain scalping with 1.5 ATR stop, 1.5 reward, 0.15 spread cap, 300-second cooldown and an unbuffered sizing estimate (`risk_buffer=1`). The JSON report records resolved parameters.

- Signals use selected closed entry/trend candles, with entry at the next observed M1 open only while both signal and trend remain fresh. Large gaps discard pending entries; no missing fills are invented. The configured cooldown applies between successful entries (live also claims dry-run/rejected/uncertain attempts). If both SL and TP are touched in the same M1 candle, the simulator assumes SL first. Stop gaps fill at the worse price.
- The final open position is liquidated for reporting and marked as a forced exit. JSON output includes trades, statistics, and assumptions.
- This is a single-symbol, one-position simulator, not a multi-pair portfolio backtest. `max_open_risk_fraction` still caps each entry against current equity, but other-symbol reservations and a ten-position portfolio are not simulated. It assumes constant spread, slippage, and contract-value conversion. It does not model tick-by-tick execution, liquidity, partial fills, news calendars, actual latency, swap, or broker margin. Results are **not identical** to MT5 runtime behavior and do not establish live profitability.
- Evaluate profit after costs, drawdown, profit factor, sample size, stability across market periods, and out-of-sample performance. Trade count or win rate alone is insufficient.

## Tests

Tests do not require a terminal connection and never send orders.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

CI runs offline tests on Windows only for pull requests targeting `main`, the default branch. Pushes do not trigger the workflow. Local configuration, state, logs, market data, and reports are excluded from commits. Never store trading passwords or tokens in this repository.

