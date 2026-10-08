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

[Dockerfile](Dockerfile) and [compose.yaml](compose.yaml) run Ollama separately from the Windows robot. Docker Desktop must be running with Linux containers (for example, the WSL 2 backend). This CPU-only setup does not enable GPU access or change trading behavior. BijiSatu optionally supports **observation-only** commentary or an explicit **entry filter**, both disabled by default, loopback-only, bounded and asynchronous. Observation never changes orders. Filter mode requires a valid strategy setup **and** an exact, fresh Qwen approval; failures block new entries, never change sizing or risk controls. Neither mode guarantees accuracy or profit. Offline strategy backtests do not simulate Qwen. See the [BijiSatu guide](docs/robots/bijisatu.md#optional-ollama-entry-filter) for configuration, validation requirements and log events.

Start the service from the repository root and check its API:

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod -Uri "http://localhost:11435/api/tags"

```

The API is published only on `127.0.0.1:11435`, not on external network interfaces; Ollama still listens on port 11434 inside the container. No broker credentials, source code, local configuration, or trading state are copied into the image; the build context is restricted by [.dockerignore](.dockerignore). MT5 and the Python robot remain on Windows.

The server container is named `ollama`. The one-shot `ollama-model-init` container waits for server health, checks for `qwen3:4b`, and downloads it only if missing. It then exits successfully; an exited init container is expected. Models are stored in the server's `ollama-models` named volume, not baked into the image. This avoids adding gigabytes to image builds and preserves downloads across rebuilds and container recreation. An init failure does not mark the server unhealthy; inspect its logs and installed models:

```powershell
docker compose logs ollama-model-init
docker compose exec ollama ollama list

```

Choose a different model before startup by setting `$env:OLLAMA_MODEL = 'qwen3:4b'` to the desired installed/downloadable name, then running Compose. Keep the robot's model setting consistent; changing Compose does not change robot configuration. Fixed container names must not already be owned by another deployment.

`docker compose up -d --build` invokes the builder, but reuses cached layers when their inputs have not changed; it does not force a full rebuild. For ordinary starts after the first build, use `docker compose up -d --no-build`. A changed Dockerfile or base image may invalidate relevant layers. Neither command deletes the model volume; if the configured model already exists, init skips the pull rather than checking for upstream model updates.

The health check verifies that the service responds, not that a model is installed or trading decisions are usable. Model downloads require internet access and can consume several gigabytes. CPU inference may be too slow for an entry-time filter; measure latency before integrating it.

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

No strategy guarantees profit, including a daily profit target. BijiSatu supports selectable M15/H1 intraday and legacy M1/M5 scalping research; see its guide for rules and config-aware offline tests. Validate behavior with realistic transaction costs, out-of-sample data, and demo forward testing before considering real-money execution.

Cent accounts use real money. Gaps, slippage, spread changes, commission, and swap can cause losses beyond calculated limits. Stopping a local process does not necessarily close broker positions; consult the robot's documented shutdown behavior.

