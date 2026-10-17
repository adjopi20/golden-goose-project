# BTC MVRV dual-model daily service

**Current deployment (2026-10-17): signal monitor only** on `adjopi20@139.190.102.183` (`golden-goose`), project/service `btc-mvrv-signals`, image `signals-20261017`. This is the operator's selected mode while no trading capital is assigned. It has its own `signals.sqlite` volume, no Binance key/PEM, no accounts/orders/fills, and no invented portfolio. It reuses the frozen MVRV calculation and posts one dated daily Telegram status: BUY_ZONE / SELL_ZONE / WAIT for 0/7 and four-year P5/P95. These zones do not know manual holdings and are NOT executed trades.

Daily refresh starts at/after 08:00 WIB; unpublished/invalid data retries hourly with an error notification, never a fabricated fresh signal. Telegram outbox retries every minute; ambiguous delivery may duplicate a message, not a transaction. Startup fetches complete Coin Metrics history automatically. Source revisions/gaps halt new status acceptance for review. Bootstrap, native Telegram delivery, restart/idempotency and initial online/off-server backup passed; phone receipt still needs the operator's confirmation. All 12 pre-existing container IDs remained unchanged and healthy.

Use `sudo docker exec btc-mvrv-signals-btc-mvrv-signals-1 python -m live_mvrv.signal_monitor report` to inspect this monitor. Compose is `compose.signal-monitor.yaml`; it reads only Telegram values from the root ignored `binance-production.env`. Its CPU/RAM limits are 0.25 / 256 MiB. The production trading worker below is NOT running and its capital placeholder was deliberately not funded. Starting real trading remains a separate explicit decision.

**Production deployment (2026-10-16):** use [PRODUCTION_DEPLOY.md](PRODUCTION_DEPLOY.md) and `bash deploy-production.sh`. The Binance worker has a separate project/env/ledger and defaults to execution **OFF**. The paper/manual-alert commands below are not the production exchange worker.

Active selected runtime: **0/7 + four-year P5/P95**, initial **50:50**, one engine, independent accounts. Read `CONTRACT.md` before starting. Defaults: $1,000 simulated total capital, paper mode, Telegram disabled. No real exchange orders or credentials.

This folder builds independently. It does not package the old research folders or import accumulation/CVDD code. No browser, WebSocket, GPU or LLM API. Coin Metrics is the shared valuation source; no Binance feed is required in v1.

The separate Binance Spot adapter supports Testnet and explicitly gated production, native BTC/USDT fees and per-model compounding. Production is OFF in the prepared rollout. See `PRODUCTION_DEPLOY.md` for production and `BINANCE_ADAPTER.md` for historical Testnet setup. Never reuse another environment's env file or funded volume.

## Local Python

From repository root in PowerShell:

```powershell
$env:PYTHONPATH="$PWD\models\bitcoin_onchain_cycles"
.\.venv\Scripts\python.exe -B -m live_mvrv.main once --database models\bitcoin_onchain_cycles\live_mvrv\data\local.sqlite --capital 1000 --mode paper
.\.venv\Scripts\python.exe -B -m live_mvrv.main report --database models\bitcoin_onchain_cycles\live_mvrv\data\local.sqlite
```

Use `run` instead of `once` for a resident daily worker. `once` requires current complete prior-UTC-day inputs and may safely fail while the provider has not yet published them. Accounts start fresh in cash; historical data bootstraps indicators, never hypothetical holdings. `.env` is consumed by Compose, NOT automatically by the Python CLI; set local environment explicitly if testing Telegram without Docker.

Dependencies are in `requirements.txt`; use an existing venv or install into a new isolated venv. Runtime commands must retain the original capital/mode for an already initialized database; configuration changes are rejected, not silently reset.

## Docker — one extra independent service

```powershell
Set-Location models\bitcoin_onchain_cycles\live_mvrv
Copy-Item .env.example .env
# Edit .env locally: simulated capital, mode, optional Telegram credentials.
docker compose -p btc-mvrv-dual -f compose.yaml config --quiet
docker compose -p btc-mvrv-dual -f compose.yaml up -d --build
docker compose -p btc-mvrv-dual -f compose.yaml logs --tail 50 -f btc-mvrv-monitor
docker compose -p btc-mvrv-dual -f compose.yaml exec btc-mvrv-monitor python -m live_mvrv.main report
```

Linux/server uses the same Compose commands after copying this directory; see `SERVER_RUNBOOK.md`. Always specify `-p btc-mvrv-dual -f compose.yaml`: a shell can still have intraday `COMPOSE_FILE`/`COMPOSE_PROJECT_NAME` variables, and these explicit flags prevent accidentally selecting that project. First run initializes once; all subsequent restarts restore the persistent `mvrv_data` volume. This separate project never stops/recreates the two intraday services. No port needs to be opened. Do not run `down -v`: that removes funded paper/account history. Never change initial capital to force a reset.

Telegram: follow `TELEGRAM_SETUP.md` for local `telegram-discover` and `telegram-test` commands. These never open a trading DB. Create your own bot via BotFather, set `TELEGRAM_ENABLED=true`, token and your recipient chat ID in ignored `.env`; start the bot conversation first. Treat token as secret. API success is recorded; failed deliveries remain in an outbox. Ambiguous retries can repeat a message with the same action ID, never a fill. Avoid printing `docker compose config` without `--quiet` after configuring secrets.

Worker: first attempt at/after 01:00 UTC (08:00 WIB), retry hourly while fresh data unavailable, evaluate once per accepted day, heartbeat each minute. A paper proposal targets a future UTC close and expires if that close was skipped. UTC day labels, provider availability, evaluation, reference price and recording timestamps are distinct. This is not an exchange execution guarantee. Check status after starting; Docker unhealthy is not automatic restart.

## Alerts rather than automatic simulated holdings

For manual alerts use a NEW database/Compose volume initialized with `MODE=alerts`. There is no live-trading mode. BUY/SELL proposals leave cash/BTC unchanged until you confirm a fill. Pending actions block duplicate proposals.

```powershell
docker compose -p btc-mvrv-dual -f compose.yaml exec btc-mvrv-monitor python -m live_mvrv.main confirm-fill --action-id ID_FROM_ALERT --fill-id YOUR_UNIQUE_FILL_ID --units 0.001 --price 60000 --fee-usd 0.06 --filled-at 2026-10-04T09:00:00+07:00
```

Replace all example values/timestamp with the actual transaction. Add `--partial` if further fills are expected. Quote and fees are USD reference accounting only; USDT/USDC or BTC-paid fees must not be silently treated as USD fees. A venue accounting adapter is not implemented. To decline/abandon an action, use `cancel-action --action-id ID`; cancellation is explicit and logged. Report metrics are daily reference marks, not live exchange valuation. Recording an intraday manual fill affects next day's mark; current balances are separately shown at the latest reference price.

## Backup and recovery

```powershell
docker compose -p btc-mvrv-dual -f compose.yaml exec btc-mvrv-monitor python -m live_mvrv.main backup --destination /data/backup-20261003.sqlite
```

Use a fresh backup filename. Copy the resulting backup off-server separately. Do not copy only an active WAL `.sqlite` file and ignore its WAL. Restore offline into a separate verified volume, retaining contract, mode, initial capital and ledger. No backfilled missed trades after downtime.

## Tests and offline replay

```powershell
.\.venv\Scripts\python.exe -B -m pytest models\bitcoin_onchain_cycles\live_mvrv\tests -q -p no:cacheprovider
```

Run this from repo root. Offline replay is separate from live-state commands:

```powershell
$env:PYTHONPATH="$PWD\models\bitcoin_onchain_cycles"
.\.venv\Scripts\python.exe -B -m live_mvrv.replay --input FULL_COINMETRICS_RAW.csv --start 2021-01-01 --end 2026-10-02 --capital 10000 --output NEW_RESEARCH_FOLDER
```

Raw data includes origin 2010-07-18, not just the requested trading period. `--tail` can append a disjoint raw extension; duplicates/revisions/gaps fail. Replay uses historical next-close fills, not archived publication timing. It never opens a live database. Results are retrospective, not new OOS. Account and combined DD must be measured from their own curves.

For a synthetic Docker restart/backup probe, use a NEW empty test volume, never your production account. From this directory in PowerShell (Docker running):

```powershell
$probeVolume = "btc-mvrv-state-probe-" + (Get-Date -Format yyyyMMddHHmmss)
docker build -t golden-goose/btc-mvrv-dual:state-probe .
if ($LASTEXITCODE -ne 0) { throw "Probe image build failed" }
$probeMount = "type=bind,source=$PWD/tests,target=/checks,readonly"
foreach ($phase in @("seed", "resume", "verify")) {
  docker run --rm --network none --read-only --tmpfs /tmp:size=32m --cap-drop ALL --security-opt no-new-privileges --env PYTHONPATH=/app --env TELEGRAM_ENABLED=false --mount "type=volume,source=$probeVolume,target=/data" --mount $probeMount --entrypoint python golden-goose/btc-mvrv-dual:state-probe /checks/docker_restart_probe.py $phase
  if ($LASTEXITCODE -ne 0) { throw "Probe failed: $phase" }
}
```

This fixture has invented 2020 signals and no network. It checks container persistence/anti-duplicate fills/backup, NOT performance or real-market signals. The named test volume is retained for review; do not mount it as production data. A separate fresh paper worker/API/health test is still required.

## Release gate

Required: unit/data/state/notification tests, research curve/fill parity, current source smoke, Docker build and persistent-volume restart test, configured phone delivery check, then paper/alerts deployment only. A working worker does not prove the financial edge. Actual server resources/container inventory and backup policy must be checked before deployment. See `VALIDATION.md` for verified results and remaining gates, and root `LEGACY.md` for preserved research code.

