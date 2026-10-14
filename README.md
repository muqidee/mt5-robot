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

## Optional local Ollama service

[Dockerfile](Dockerfile) and [compose.yaml](compose.yaml) run Ollama separately from the Windows robot. Docker Desktop must be running with Linux containers (for example, the WSL 2 backend). The initial deployment was CPU-only. GPU visibility is now verified inside the container: NVIDIA RTX 5070 Ti Laptop GPU, 12,227 MiB VRAM, driver 610.74. Ollama reported `qwen3:4b` fully GPU-resident at context 2048 (`size` and `size_vram` both 2,874,062,929 bytes). The first cold `qwen3:4b` GPU request nevertheless timed out at 30 seconds. The newer `qwen3.5:4b` is downloaded, fully GPU-resident and now explicitly selected in the local robot configuration; synthetic validation results and their limits are documented in the guide. Container changes do not restart the Windows robot or change trading behavior. BijiSatu optionally supports **observation-only** commentary or an explicit **entry filter**, both disabled by default, loopback-only, bounded and asynchronous. Observation never changes orders. Filter mode requires a valid strategy setup **and** an exact, fresh Qwen approval; failures block new entries, never change sizing or risk controls. Neither mode guarantees accuracy or profit. Offline strategy backtests do not simulate Qwen. See the [BijiSatu guide](docs/robots/bijisatu.md#optional-ollama-entry-filter) for configuration, validation requirements and log events.

Start the service from the repository root and check its API:

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod -Uri "http://localhost:11435/api/tags"

```

The API is published only on `127.0.0.1:11435`, not on external network interfaces; Ollama still listens on port 11434 inside the container. No broker credentials, source code, local configuration, or trading state are copied into the image; the build context is restricted by [.dockerignore](.dockerignore). MT5 and the Python robot remain on Windows.

The server container is named `ollama`. The one-shot `ollama-model-init` container waits for server health, checks for `qwen3.5:4b` by default, and downloads it only if missing. `OLLAMA_MODEL` can override this Compose initialization default. It then exits successfully; an exited init container is expected. Models are stored in the server's `ollama-models` named volume, not baked into the image. This avoids adding gigabytes to image builds and preserves downloads across rebuilds and container recreation. An init failure does not mark the server unhealthy; inspect its logs and installed models:

```powershell
docker compose logs ollama-model-init
docker compose exec ollama ollama list

```

The original `qwen3:4b` remains supported and can be pulled manually or selected as the initialization override. Choose a different model before startup by setting `$env:OLLAMA_MODEL = 'qwen3:4b'` to the desired installed/downloadable name, then running Compose. Keep the robot's model setting consistent; changing Compose does not change robot configuration. Fixed container names must not already be owned by another deployment.

The Compose model-init fallback and the ignored local configuration now select **`qwen3.5:4b`**. For compatibility, Python `Config`, `config.schema.json` and `config.example.json` still default to **`qwen3:4b`**; they were not globally changed. Set `ollama_observation_model` to `"qwen3.5:4b"` in your robot configuration to match the Compose download. Compose does not rewrite that setting, and the robot client never downloads models. Configuration loads at robot startup; changing the selected model does not update or restart an already running robot.

`docker compose up -d --build` invokes the builder, but reuses cached layers when their inputs have not changed; it does not force a full rebuild. For ordinary starts after the first build, use `docker compose up -d --no-build`. A changed Dockerfile or base image may invalidate relevant layers. Neither command deletes the model volume; if the configured model already exists, init skips the pull rather than checking for upstream model updates.

The health check verifies that the service responds, not that a model is installed or trading decisions are usable. Model downloads require internet access and can consume several gigabytes. Historical CPU-only inference sometimes exceeded the entry-filter deadline. Verify container GPU offload and measure the exact production client with synthetic data before relying on latency; other model clients and large contexts can still contend for resources. GPU access does not establish market accuracy or profitability.

The selected container RAM hard cap is **6 GiB**, below 40% of Docker's reported 15.34 GiB. The deployment applies `mem_limit: 6g` and `memswap_limit: 6g` (no additional swap). Effective HostConfig `Memory` and `MemorySwap` were both verified as `6442450944` bytes after restart; a point-in-time memory sample was 4.702 GiB of 6 GiB. This caps this container's host RAM, **not** global Windows/Docker RAM or GPU VRAM, and cannot guarantee protection against a BSOD. Exceeding the limit may kill the model/container through an out-of-memory condition.

Service defaults are `OLLAMA_CONTEXT_LENGTH=4096`, `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS=1`. A client can override the default context; an explicit 64,000-token context remains an OOM/resource-contention risk. Configure JetBrains around **4096–8192** tokens instead of 64,000, starting at 4096 under the RAM cap; even these settings are not a memory guarantee. The robot explicitly requests context 2048, so its preset is unchanged. Container restart/limit verification is separate from restarting the Windows robot.


Stop the service with `docker compose down`. Models remain in the volume; adding `--volumes` deletes them. Host port 11435 must be free before startup; it avoids conflicts with an existing Ollama service on port 11434. The Dockerfile currently follows the upstream `ollama/ollama:latest` tag; pin a tested version or digest before relying on reproducible deployments.

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

No strategy guarantees profit, including a daily profit target. BijiSatu supports selectable H1-entry/H4-trend short-term, M15/H1 intraday and legacy M1/M5 scalping research; see its guide for rules and config-aware offline tests. Validate behavior with realistic transaction costs, out-of-sample data, and demo forward testing before considering real-money execution.

Cent accounts use real money. Gaps, slippage, spread changes, commission, and swap can cause losses beyond calculated limits. Stopping a local process does not necessarily close broker positions; consult the robot's documented shutdown behavior.


Runtime logs include daily readable `logs\bijisatu\YYYY-MM-DD.log` and structured `.jsonl` files. Local model request starts, validated decisions/reasons and failures also appear on the default console. See the robot guide for closed-candle diagnostics and fixed-risk warnings.

Market commentary is separately opt-in with `market_commentary_enabled=false` by default. It describes each fresh completed entry candle, including startup and safely checked held symbols, without originating signals or authorizing orders. The existing entry filter remains an independent exact-snapshot approval gate. See [commentary and logging](docs/robots/bijisatu.md#per-closed-candle-market-commentary).

Daily `.log`, `.jsonl` and console default to INFO; status/heartbeat and model events are compact in readable outputs. Stable status repeats approximately once per minute, not for every equity/quote fluctuation. New-bar setup reasons, claims/orders, halts and errors remain recorded. `--verbose` enables full DEBUG per-scan detail in files and console and can quickly increase disk usage; existing logs are appended by UTC day, never rewritten or automatically removed.

Initial exact-client `qwen3.5:4b` testing exposed truncated filter JSON at the original 64-token limit. The bounded generation budget is now 128, reasons are prompted under 120 characters (the strict 160-character validation cap remains), and measured EMA/pullback context clarifies that an earlier opposite pullback candle is expected. With coherently aggregated invented H1/H4 data, the actual client validated a filter **allow** in 1.391 seconds, a conflicting-side **skip** in 1.125 seconds and commentary `bias=buy` in 0.531 seconds, each within the unchanged 10-second deadline. Exhausted generation budgets and malformed replies remain rejected. The local model remained `qwen3:4b` during these tests; the user subsequently authorized selecting `qwen3.5:4b` in the local configuration and Compose initializer. This documentation change does not restart the robot or perform a market request or order. These synthetic results are not evidence of market accuracy or reliable future latency.
