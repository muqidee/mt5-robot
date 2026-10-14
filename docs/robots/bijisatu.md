# BijiSatu

An experimental **Python + MetaTrader 5** short-term trading robot for Windows, with selectable short-term, intraday and legacy scalping modes. It uses a testable, rule-based strategy with no guarantee of profit. An Exness cent account uses **real money**; it is not automatically a demo account.

## Strategy modes

`strategy_mode: "scalping"` preserves the original M1-entry/M5-trend default for existing configurations. `"intraday"` selects **closed M15 entries with H1 trend**. `"short_term"` selects **closed H1 entries with H4 trend** and is the example profile; omitted mode still defaults to scalping. Slower candles may reduce sensitivity to minute-level noise and spread relative to ATR, but this is a hypothesis to test, not proven profitability or a guarantee of daily profit. No trade is required each day.

All modes reuse the same rules, with no extra indicators:

1. Require at least 100 closed entry candles and 100 trend candles that closed no later than the entry candle's close. Short-term and intraday evaluate the latest 100 of each, matching the offline simulation; legacy live scalping retains its existing available history (up to 300), while offline uses 100.
2. For a buy, the last three trend closes must be above EMA20, EMA20 above EMA50, and both EMAs rising on each candle. A sell mirrors these conditions.
3. The previous entry close must be at/below its EMA20 for a buy, followed by a bullish candle closing above its own EMA20. A sell requires the opposite reclaim and a bearish candle.
4. ATR14 uses Wilder smoothing on the entry timeframe (H1, M15 or M1). Spread, stop distance and sizing use that ATR; signals never use an unfinished H4/H1/M5 candle.
5. Reject future or stale closed candles: entry close age must be within its duration plus 30 seconds, trend within its duration plus 90 seconds. Thus short-term limits are 3630/14490 seconds, intraday 930/3690 seconds and scalping 90/390 seconds. Quotes still have the separate 15-second default limit. Recheck entry signal and trend age before claiming an attempt; saved bar timestamps prevent duplicates and cooldown remains per symbol across restarts/mode changes.

Short-term and intraday describe the entry horizon, **not a forced same-day exit**. Positions remain until server SL/TP; no end-of-day liquidation or maximum holding-time rule is implemented. Consider overnight gaps and swap in research.

### Execution and risk controls

- EMA20/EMA50 trend filter, EMA20 pullback/reclaim entries, and ATR14. Signals use closed candles only.
- Initial stop loss at 1.5 × ATR, widened where required by broker limits and spread. Take profit targets 1.5 × the actual stop distance. No martingale, grid, averaging down, or trade-count targets.
- Discovers major-currency Forex pairs and gold from broker metadata, including broker suffixes. Scans up to 20 symbols by default, with one variant per pair. Set `symbols` to restrict trading to instruments you have tested.
- Ranks valid setups by spread/ATR, with a maximum spread of 15% of ATR. The compatibility default permits two positions and a five-minute per-symbol cooldown; the user-owned local profile permits up to ten with a fifteen-minute cooldown. One position per symbol remains enforced.
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

Do not enable the filter in a running trading setup until offline mocked filter tests **and a synthetic, real structured response through the exact production client** pass. A healthy service or installed model alone is insufficient. An earlier CPU-only validation timed out at 30 seconds; that historical result did not verify a usable filter response. The currently selected local filter setting is preserved, not activated or restarted by these documentation changes. No MT5 connection, order, model download or live restart is part of this validation.

A valid strategy setup **AND** an exact Qwen `allow` are required. Qwen is veto-only: it cannot invent trades, change lot size, SL/TP, risk limits or execution settings. `skip`, pending, timeout, malformed response, service failure, full queue, absent client or expired verdict means **no new order**. Filter mode applies equally to dry-run plans and execution, but dry-run still never sends orders.

The engine never waits for inference. One bounded worker queues market-only snapshots, and later scans look up results. Requests are deduplicated per symbol/bar, including failed/saturated requests, without recording an order attempt. The approval binds symbol, side, bar, strategy mode/timeframes, model, endpoint and the exact canonical snapshot (including spread and bounded closed candles). Changed snapshots cannot reuse it. Approval expires **30 seconds from enqueue**, not from response completion; candle-age checks may invalidate it sooner. Result memory is capped by `max_symbols`, with one latest record per symbol; obsolete queued work cannot approve a new bar. A slow CPU can therefore prevent all entries rather than silently bypass the filter.

After lookup, the engine refreshes equity, positions, daily/open budgets and quotes. Before real execution, broker preflight (including margin and `order_check`) must pass; the engine then revalidates cash flow, day, signal/approval age, current exposure/budget and quote before the durable claim immediately preceding `order_send`. Uncertain acknowledgements still halt and cannot be retried using an approval. Pending/unavailable results do not halt or reset the daily baseline. Server-side SL/TP management remains unchanged.

Shared `ollama_observation_endpoint`, `ollama_observation_model`, `ollama_observation_timeout_seconds` and `ollama_observation_queue_capacity` configure either selected mode; their old names preserve compatibility. Both flags cannot be true. Endpoint restrictions, strict response schema, bounded request/response size, no proxies/redirects and bounded shutdown join remain enforced. Only market data is sent; no account identity, equity, credentials or lots. Filter reasons are sanitized structured fields; raw responses/exceptions are never logged.

Startup reports mode `off`, `observe` or `filter`. Filter events are `ollama_filter_started`, `ollama_filter_queued`, `ollama_filter_pending`, `ollama_filter_allowed`, `ollama_filter_rejected` and `ollama_filter_unavailable`. Missing startup client remains fail-closed, including when constructing `Engine` directly. Filter accuracy/profit benefits are **unproven**. Offline CSV strategy backtests **do not simulate Qwen or test this filter**; they cannot demonstrate filter performance.

## Optional Ollama observation only

Ollama commentary is **disabled by default** and independent of `--execute`. Opting in does not authorize orders or change dry-run behavior. Qwen judgements never filter a signal, alter exposure/risk, write order claims, or send/modify orders. Existing pre-entry exposure and freshness checks remain authoritative. Profit benefits are **unproven**; no live performance improvement is claimed.

The following is an optional legacy signal-observer example, not the current ignored local configuration. The current local observer remains disabled; per-candle market commentary is a separate setting:

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

One background worker posts `/api/chat` with `stream: false`, `think: false`, a JSON response schema, temperature 0, fixed seed and a bounded fixed preset (`num_predict: 128`, `num_ctx: 2048`, `num_thread: 4`). Qwen versions must support these options; unsupported requests only produce an observation error. Each request contains symbol, side, entry-bar open timestamp, timeframe minutes, ATR, price spread, verified EMA trend/pullback context, and at most six closed OHLC candles per timeframe. Trend candles close no later than the entry cutoff. No account identifiers, credentials, equity, lot sizing, repository files, configuration or persistent state are sent. No tools or execution authority are provided.

The configured queue holds at most four pending snapshots by default plus one in flight. Submission never waits for inference: queue saturation, timeouts, service outages, malformed replies and model `skip` decisions do not suppress valid orders or modify their plans. A process-local per-symbol bar watermark prevents repeated requests for the same or older signal, including failed/saturated observations; it is not a durable trading claim and resets on restart. Symbol tracking is capped at 1,024 without eviction; excess symbols only log unavailable. Signals are observed during scanning before spread/entry sizing, so a judgement does not imply an order was placed or even eligible. Account-wide blocks and cooldowns may prevent a symbol from reaching evaluation.

Structured events in the existing daily logs:

- `ollama_observation_started`: explicitly observation only, never an execution filter; account startup details also report enabled/active status.
- `ollama_observation_queued`: symbol, side and bar timestamp scheduled once.
- `ollama_observation_result`: `decision` (`allow`/`skip`), printable reason of at most 160 characters, and request latency. These are untrusted commentary, **not execution decisions**.
- `ollama_observation_error`: snapshot/scheduling/response rejected, with a fixed reason code, not raw response/exception text.
- `ollama_observation_unavailable`: connection failure, timeout, queue/symbol capacity, startup failure or shutdown cancellation. No retries for that bar.

Response bodies are capped at 8 KiB, headers/transport reads are bounded, and judgement keys/types/enums are strictly checked; duplicate keys, nonfinite numbers, tool calls and control characters are rejected. Shutdown cancels pending snapshots, waits at most the request deadline plus 0.2 seconds for the single worker, and disables its logging before logging handlers close. It never waits for the whole queue. A one-cycle run may cancel pending observations rather than report every queued result.

Historical CPU-only inference and model warmup can exceed the unchanged default 10-second total deadline (allowed 0.1–30 seconds). Requests retain the bounded CPU-thread preset `num_thread: 4`, `num_ctx: 2048`, `num_predict: 128`, deterministic sampling, no thinking and no streaming. An independent synthetic local check returned complete parseable commentary JSON in approximately 3.4 seconds with four CPU threads; earlier default-thread checks timed out at 30/90 seconds. A 32-token synthetic check produced truncated invalid JSON, so that reduced preset was not adopted. A later exact-client truncation at the original 64-token limit is addressed by the bounded 128-token preset described below. These checks used invented data and establish neither realistic prompt latency nor market accuracy. Real candle prompts may still time out or yield invalid/nonsensical text. No model response or profit is guaranteed; automated tests use mocked transport only.

### GPU deployment and container memory cap

GPU activation was independently requested after the historical CPU-only measurements above. `nvidia-smi` inside the container verified the NVIDIA RTX 5070 Ti Laptop GPU with approximately 12 GB VRAM (host report: 12,227 MiB, driver 610.74). After the first synthetic GPU request timed out at 30 seconds, `/api/ps` reported `qwen3:4b` fully GPU-resident: `size=2874062929`, `size_vram=2874062929`, context 2048. The CUDA0 KV cache was reported as 288 MiB. **Offload is verified; this first cold request did not produce a usable response.** Service health, model residency and a complete schema-valid production-client reply are distinct checks.

The explicitly selected container RAM hard cap is **6 GiB**, below 40% of Docker's reported 15.34 GiB. Deployment settings are `mem_limit: 6g` and `memswap_limit: 6g`, with no additional swap allowance; effective HostConfig `Memory` and `MemorySwap` were verified after restart, both `6442450944` bytes. A point-in-time memory sample was 4.702 GiB of the 6 GiB limit, not a maximum-use guarantee. This is a limit on this container's host RAM, not global Windows/Docker RAM and not GPU VRAM. It cannot guarantee that a BSOD will not occur. Exceeding the cap can trigger an out-of-memory kill of the model/container and failed requests; filters remain fail-closed.

Service defaults are `OLLAMA_CONTEXT_LENGTH=4096`, `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS=1`. These defaults reduce concurrent load but do not prevent clients from requesting a larger context. An explicit 64,000-token request still risks OOM. A concurrent JetBrains client was observed at context 64,000, approximately 11.73 GB memory and 1,807% CPU; it may contend for resources and is not a benchmark of the bounded robot request. Prefer JetBrains context **4096–8192**, starting at 4096 under this cap; larger contexts and even these suggested values require memory monitoring. The robot keeps its explicit `num_ctx: 2048`, 10-second request deadline and 30-second filter approval lifetime.

Container configuration/restart and synthetic GPU verification are handled separately. The Windows trading robot is **not restarted** by that operation. No live-market request, order or market-accuracy claim is part of synthetic verification. GPU residency or a faster synthetic response does not establish market accuracy, reliable trading latency or profitability.

### Newer-model synthetic validation before the authorized switch

The separately authorized `qwen3.5:4b` download completed (approximately 3.324 GB persisted in the model volume). `/api/ps` reported full GPU residency with `size=size_vram=3060803828` bytes (approximately 2.85 GiB). An independent generic-schema synthetic check completed cold in 49.77 seconds and warm in 0.63 seconds, with parseable bias/reason JSON. Those generic-schema results do not validate the robot's distinct filter schema, and the cold latency exceeds the robot's unchanged deadline.

Initially, exactly one commentary request and one filter request were tested through the actual production `OllamaClient` against `qwen3.5:4b`, using only invented 100-bar H1/H4 OHLC histories, a synthetic symbol and a synthetic strategy signal. No broker, account, repository contents, private state, engine or order path was used. Options remained context 2048, prediction limit 64, CPU-thread preset 4, no thinking/streaming, and the **10-second** request deadline.

| Exact production-client test | Result | Measured latency | Required response schema |
| --- | --- | --- | --- |
| Market commentary | Validated `bias=buy` and bounded plain-text reason | 1.250 seconds | Exactly `bias` (`buy`, `sell`, `neutral`) and `reason` (1–160 printable characters) |
| Entry filter | Rejected with `JSONDecodeError`; no validated decision or approval | 1.250 seconds | Exactly `decision` (`allow`, `skip`) and `reason` (1–160 printable characters) |

There were no retries, raw response/chain-of-thought logs, broker connections or orders. Parseable commentary may still contain inaccurate or nonsensical statements; these tests establish no market accuracy, profitability or reliable future latency. **This initial exact filter validation did not pass; the follow-up below resolves the formatting issue without accepting malformed responses.** At that validation stage, the local robot model remained `qwen3:4b` and the robot was not restarted. The subsequent user-authorized selection is documented below. Neither a generic synthetic response nor commentary bias can authorize a trade or substitute for the independent filter approval.

### Bounded generation reliability follow-up

A synthetic-only diagnostic of the original filter request confirmed `done=true`, `done_reason=length`, `eval_count=64`, and 197 content characters without a closing JSON object. Only safe envelope metadata/lengths were inspected, not raw output or chain-of-thought. The 64-token generation budget truncated the structured response; relaxing enums or parsing partial JSON is not a fix.

The production budget is now **128 tokens**; 256 was not needed. All modes request a concise reason under 120 characters, while the unchanged strict schema allows 1–160 printable characters and no extra keys. Requests with `done_reason=length` are explicitly rejected even if their prefix looks like valid JSON. INFO failures explain truncation without echoing the response. Context 2048, four CPU threads, 10-second deadline, 30-second filter approval lifetime, model choice, queue/resource bounds and order/risk controls are unchanged.

The filter snapshot now supplies measured EMA20/50 alignment/slopes and pullback/reclaim diagnostics from the same strategy history window, rather than relying solely on six OHLC candles or an assumed trend side. Its prompt clarifies that the preceding opposite/pullback candle is expected, not itself a direction conflict. These facts are market data, not automatic approval: a fresh exact model `allow`, valid strategy and risk checks are still required. The additional factual context is included in the existing canonical snapshot digest, so an approval cannot bind to a different snapshot. Validated but conservative `skip` results from earlier synthetic samples were not converted to approval.

The final actual-client tests used invented 400-bar H1 data aggregated into 100 coherent H4 candles, with a strategy-verified buy pullback/reclaim; the conflicting-side case supplied sell against that measured buy trend. No engine, broker, private data or orders were used.

| Final exact-client test (`qwen3.5:4b`, 128-token limit) | Validated result | Latency | Schema |
| --- | --- | --- | --- |
| Strategy-aligned synthetic filter case | `decision=allow` | 1.391 seconds | Exactly `decision`, `reason` |
| Conflicting-side synthetic filter case | `decision=skip` | 1.125 seconds | Exactly `decision`, `reason` |
| Synthetic market commentary | `bias=buy` | 0.531 seconds | Exactly `bias`, `reason` |

All three completed within 10 seconds and passed the unchanged enums, bounded printable reason and strict envelope/model validation. Mocked regression tests still reject truncated replies, including a complete-looking JSON body marked generation-length exhausted, and assert the 128-token request preset. These observations do not establish correctness on real markets, future latency, a win rate or profitability. The local model remained `qwen3:4b` throughout these tests; no automatic model switch or robot restart was performed. The subsequent explicit selection of `qwen3.5:4b` does not change these historical test results.

### Current model selection and compatibility defaults

After the synthetic verification, the user explicitly selected **`qwen3.5:4b`**. The ignored local configuration now sets `ollama_observation_model="qwen3.5:4b"`; the Compose model initializer also uses **`qwen3.5:4b`** as its `OLLAMA_MODEL` fallback. Initialization checks the selected model and downloads it into the persistent model volume only when missing. An explicit `OLLAMA_MODEL` environment value overrides the Compose fallback.

Python `Config`, `config.schema.json` and `config.example.json` retain the original **`qwen3:4b`** library defaults for backward compatibility. These are distinct from the Compose initialization default and the current ignored local selection. Set `ollama_observation_model` to `"qwen3.5:4b"` in the robot configuration to match the default Compose download; the runtime does not infer its model from Compose, rewrite configuration or download models. The original `qwen3:4b` remains supported and can be pulled manually or selected through the Compose initialization override.

The model setting is read at robot startup. Updating local configuration does not change an already running process; any robot restart is a separate operation and was not performed for this documentation update. The chosen model is not a guarantee of valid future responses, market accuracy or profit. Filter/observer/commentary flags, risk settings and persistent state are unaffected by model selection.

### Per-closed-candle market commentary

`market_commentary_enabled` defaults to **false**, independently of `ollama_filter_enabled` and `ollama_observation_enabled`. When enabled, startup processes the latest completed entry candle and each subsequent new candle (H1 with H4 context in `short_term`). Validated, warmed-up market-only snapshots include factual EMA20/50 slopes/alignment, ATR14, OHLC and failed strategy conditions. There is no fabricated executable signal, account identity, equity, credentials, sizing or order data. Symbols with existing positions are included after safe exposure checks, even if the position/risk budget prevents new entries. Halts, pending orders and failed exposure checks may prevent commentary; they are never weakened for commentary.

The separate response is exactly `{"bias":"buy|sell|neutral","reason":"short plain text"}`. This is **discussion, never authorization**: a buy bias cannot produce an order. Strategy, risk and the exact fresh entry-filter approval remain required when the filter is enabled. Commentary errors/neutral bias cannot veto an otherwise approved strategy.

At most one request is attempted per symbol/entry candle/strategy mode per process, including startup. Queue-full, timeout and invalid responses consume that candle without polling retries. A restart may revisit the latest candle. Queues and symbol watermarks are bounded. Commentary reserves a startup batch slot per configured symbol, using `min(32, max(queue_capacity, max_symbols))` pending slots; the filter/legacy observer keep the configured queue capacity. This prevents a five-symbol batch from systematically dropping its last symbol before the worker is scheduled. Symbol watermarks are capped by `max_symbols`. With the filter enabled, commentary uses the existing filter worker: queued filter requests take priority, and only one model request runs at a time. An already running commentary may delay a newly arriving filter request by at most its existing deadline; filter approvals still expire after 30 seconds. Commentary jobs waiting more than 30 seconds are discarded. No inference/network polling is added to the engine. Shutdown cancels queued jobs; `--once` may exit before queued commentary runs.

INFO `ollama_request_started` is emitted at the actual HTTP send, not submission. Correlated results include validated bias/reason and latency; failures explain timeout/model/schema rejection without raw response or chain-of-thought. Daily `.jsonl` retains structured context; console and `.log` show compact, wrapped request/result blocks. Request IDs are retained in files, not shown on the console. `setup_assessment` records human-readable failed H4 alignment/slopes/closes or H1 pullback/reclaim/direction once per entry bar, with finite EMA and last-candle context. Existing `no_trend_pullback_setup` summary codes are retained.

## Risk settings

The compatibility defaults remain **up to 3% of equity per entry and a 5% daily loss limit**. These are aggressive limits, not risk targets to exhaust. The example JSON intentionally retains these defaults.

An earlier ignored local configuration used a **research/test profile**, not approved live settings: `strategy_mode="intraday"`, `risk_fraction=0.01` (1% per entry), `max_open_risk_fraction=0.05` (5% of current equity combined open risk), `max_positions=10`, `daily_loss_fraction=0.05` (5% of the saved daily baseline), `cooldown_seconds=900`, `max_spread_atr=0.15`, `stop_atr=1.5`, `reward_ratio=1.5`. Other account, broker, symbol, commission and persistence settings are unchanged. No running process was restarted and no terminal connection or orders were made as part of this change. Configuration is read only at startup; a running instance still uses its old settings.

Changing mode/risk must **never** delete/reset state to bypass a daily halt. The configured daily threshold is applied against the existing saved baseline on the next scan after settings are loaded; previously latched halts remain latched. Stops on existing positions are not modified. Test out-of-sample with realistic spread, adverse slippage and account-currency commission before any demo forward evaluation. Neither wider ATR stops nor reduced risk establish a profitable edge.

That earlier local **5% daily loss protection was a chosen safeguard**, not an additional user-selected portfolio target. It replaces the earlier 2% test setting so that a hidden 2% daily allocation does not defeat the requested 5% open-risk ceiling. Daily limits still take priority when losses consume the day's budget.

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
| `strategy_mode` | `scalping` (omitted default; example: `short_term`) | `scalping`: M1/M5; `intraday`: M15/H1; `short_term`: H1/H4. Closed EMA trend/pullback rules; no daily exit guarantee. |
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
| `log_dir` | `"logs"` | Daily INFO JSONL/readable .log directory under `bijisatu/`; DEBUG/full detail requires --verbose. UTC append-only, no automatic deletion. |
| `heartbeat_seconds` | `60` | Repeats unchanged status approximately once per minute, checked after scans. Does not alter trading rules. |
| `market_commentary_enabled` | `false` | Describe every new completed entry candle (and latest at startup), including safely checked held symbols; never grants entry approval. Shared bounded worker prioritizes the independent filter. |
| `ollama_filter_enabled` | `false` | Opt in to fail-closed strategy AND exact fresh Qwen approval. Mutually exclusive with observation; never grants execute permission. Validate synthetic production responses before activation. |
| `ollama_observation_enabled` | `false` | Opt in to local asynchronous commentary only. Never filters entries or authorizes orders; independent of execute/dry-run. Cannot be true with filter enabled. |
| `ollama_observation_endpoint` | `"http://127.0.0.1:11435"` | Loopback HTTP origin only; no credentials, path, query, fragment, DNS, proxies or redirects. `/api/chat` is appended internally. |
| `ollama_observation_model` | `"qwen3:4b"` | Backward-compatible library default; Compose/local selection now uses `qwen3.5:4b`, so set this key explicitly to match. Installed local model; 1–128 characters matching letters/digits then letters/digits/dot/underscore/colon/slash/hyphen. No automatic download. |
| `ollama_observation_timeout_seconds` | `10.0` | Shared total request deadline, 0.1–30 seconds. CPU latency may exceed it: observation only logs failures; filter blocks entries. Filter approval expires 30 seconds from enqueue. |
| `ollama_observation_queue_capacity` | `4` | Filter/legacy observer pending cap, integer 1–32, plus one active request. Commentary reserves max(queue capacity, max_symbols) slots capped at 32 for startup batches. Full queues do not block or retry that candle. |

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

The terminal and daily `.log` use compact, wrapped runtime/heartbeat blocks: a status header, one financial summary and readable skip counts. Request/reply blocks place model/timeframes/latency together, followed by a separate decision or bias and a wrapped reason. Default blocks wrap at 96 columns and omit microseconds and raw candle timestamps. The console hides request IDs, including with `--verbose`; full IDs remain in `.log` and JSONL for correlation. Generic detailed headers include UTC, mode and session labels. State/reason/skip changes are immediate; equity, quote and remaining-budget fluctuations alone do not create status events. Full account snapshots remain in structured JSONL at status/heartbeat events. The default heartbeat is 60 seconds (approximately one event per minute while stable), plus new-bar assessments, request/result and distinct transaction/error events. Frequency depends on actual state changes and scans; there is no fixed daily byte estimate. Every claim and sent order has a distinct INFO event, even when the runtime status is unchanged.

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

Detailed records are written automatically to `logs/bijisatu/YYYY-MM-DD.jsonl`, using the **UTC date**. Each line is a JSON object for later analysis; JSON is used in the file only, not the terminal. Default disk level is INFO. Debug-level per-scan diagnostics and full readable blocks require `--verbose`; this can quickly fill logs. The daily readable `.log` uses the same compact blocks as the console, with request IDs added for correlation.

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

Provide **M1 bid OHLC** candles with the CSV header `time,open,high,low,close`. Use UNIX seconds in UTC or ISO8601 timestamps with an explicit timezone. Timestamps must fall on minute boundaries, be unique, and increase strictly. The selected entry/trend candles are built only from complete, contiguous, fixed broker-local boundary-aligned M1 groups (1/5 minutes scalping, 15/60 minutes intraday, 60/240 minutes short-term). The configured fixed UTC offset aligns higher-timeframe closes; no DST schedule is inferred. Missing minutes invalidate their group, and incomplete higher-timeframe candles do not count toward warmup. Intraday needs at least 100 complete H1 candles (6,000 contiguous M1 rows when aligned); short-term needs 100 complete H4 candles (24,000 contiguous M1 rows), not merely 100 M1 rows. Market data is not downloaded automatically.

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


## H4 trend / H1 entry research and diagnostics

Select `"strategy_mode": "short_term"` to compare slower timeframes without changing the rules or risk. At least **100 closed H4 bars** (400 candle-hours; more calendar time across market closures) and 100 H1 bars are required. Trend bars closing after the H1 close are excluded even from warmup. This implements the existing EMA trend and EMA20 pullback/reclaim, **not breakout/retest or reversal**. No score, confidence probability or 5/10/15% risk tiers are implemented.

The local user-selected fixed risk remains **15% per entry** (`risk_fraction: 0.15`), with existing daily/open-risk caps unchanged. This is high: a stop can lose roughly 15% before further losses from gaps/costs; budgets, lot rounding and the configured buffer can reduce actual sizing. Limits do not guarantee a maximum loss. A wider H1 ATR stop generally means a **smaller lot at the same risk**, not automatically larger profit. Trades can remain open overnight or for days until SL/TP; there is no daily profit guarantee or forced daily exit.

`strategy_assessment` DEBUG events show selected timeframe labels, required/available closed history, excluded future bars, ATR14, three trend closes and EMA20/50 values/slopes, previous/current entry close versus EMA20, alignment and directional-candle checks. `rejected_reasons` distinguishes `no_trend`, `no_pullback`, `no_reclaim`, `no_directional_candle`, insufficient history or invalid candles. The legacy scan summary still uses `no_trend_pullback_setup`. Full per-scan details require `--verbose` on both daily files and console; default INFO records compact `setup_assessment` once per bar without changing entry rules.

Readable runtime files are appended at `logs\bijisatu\YYYY-MM-DD.log`, beside existing `YYYY-MM-DD.jsonl`, rotated by UTC date. The readable file uses the same labelled blocks as the console, but retains request IDs that the console hides. In both observation and filter mode, **actual HTTP sends**, not queue submissions, emit INFO `ollama_request_started`; validated responses emit `ollama_request_result` with decision, short reason and latency; timeout/connection/invalid responses emit `ollama_request_failed`. All share a request ID, model, local endpoint, symbol/side, closed candle/mode/timeframes, bounded latest market candles and rule context. These events appear on console **without `--verbose`**. No account identity, equity, credentials, raw replies or reasoning traces are included in model request events or prompts.

Illustrative log labels (not an actual model verdict or market result):

```text
Ollama request started
  Request id: <same-id>
  Strategy mode: short_term
  Entry timeframe: H1
  Trend timeframe: H4
Ollama request result
  Request id: <same-id>
  Decision: allow
  Reason: <validated short reason>
  Latency seconds: <elapsed>
Ollama request failed
  Request id: <same-id>
  Reason: Request timed out; no validated reply

```

Config-aware offline backtesting aggregates complete contiguous M1 CSV groups into H1/H4, uses the latest 100 bars of each and the configured stop/reward/spread/cooldown/risk settings. It remains deterministic and does not call Qwen. No real exported historical dataset is currently available here: synthetic offline tests verify behavior only, **not win rate, profitability or live results**. Configuration changes do not alter a running process or reset saved halts/positions; apply a reviewed profile only at a separately authorized startup.

This follow-up changes only the ignored local `market_commentary_enabled` field to true after offline tests. The already selected short-term H1/H4 profile, enabled entry filter, disabled signal observer, 15% risk settings, symbols and state are preserved. The running process is not restarted by this change.
