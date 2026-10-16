# Deploy daily MVRV beside the two intraday services

This document describes the original paper/manual-alert rollout. For Binance production with execution OFF, use **PRODUCTION_DEPLOY.md** and **deploy-production.sh** instead. Existing Testnet acceptance is documented separately; never reuse its volume for production.

One NEW service/project, not a modification to Binance/Lighter. No real-order adapter. The local image/API/recreation/backup gates passed on 2026-10-03; VPS access/resources and actual phone delivery remain operator acceptance steps.

## 1. Transfer only the runtime directory

Install this `live_mvrv/` directory on the VPS, for example under:

`~/golden-goose-project/models/bitcoin_onchain_cycles/live_mvrv`

Its build is self-contained: no root `indicator/`, dataset, CVDD browser, other models or research artifacts are required. Do not transfer your local `.env` through Git or copy acceptance SQLite/volumes into production. If using Git, commit/push only reviewed new runtime files and documentation, then pull that commit on the server. Merely cloning the default branch will not deploy an uncommitted local folder.

## 2. Configure once before first funding

On the VPS:

```bash
cd ~/golden-goose-project/models/bitcoin_onchain_cycles/live_mvrv
test -f compose.yaml && test -f config.json
umask 077
test -f .env || cp .env.example .env
nano .env
```

Set `INITIAL_CAPITAL` to the total assigned capital, not one branch's half. Keep `MODE=paper` for automatic simulated fills, or select `MODE=alerts` before FIRST launch if you want only manual buy/sell alerts and operator-confirmed fills. Do not change mode or funding after the database exists. Both begin fresh in cash; earlier hypothetical BTC holdings are not imported.

Notifications default off. To enable them, configure `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` locally, and start the conversation with your bot. No token is needed for Coin Metrics. Real-money exchange credentials are not accepted. Treat the assigned capital as money exposed to Bitcoin-cycle risk, not a drawdown guarantee.

## 3. Validate and start only this project

First check disk/memory and the existing services read-only:

```bash
df -h
free -m
docker ps
docker stats --no-stream
```

Allow several hundred MB for image/build and headroom beyond the roughly 80 MiB idle RAM measured locally. Do not stop intraday services to make room without an explicit operational decision.

Always specify both project and file flags, even if intraday left `COMPOSE_FILE` exported:

```bash
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml config --quiet
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml up -d --build --wait --wait-timeout 180
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml ps
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml logs --tail 30 btc-mvrv-monitor
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml exec -T btc-mvrv-monitor python -m live_mvrv.main report
```

Use `config --quiet`; plain `config` can print secrets. If data are not published yet, fresh daily evaluation pauses and hourly retries continue. An unhealthy result does not mean erase state or reset accounts; inspect persisted `error` and logs. `WAIT` is a valid result, not a missed purchase. No past trades are reconstructed into a newly funded live account.

## 4. VPS restart and backup acceptance

Compare accounts, pending IDs, last decision date and fill count before/after:

```bash
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml restart btc-mvrv-monitor
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml exec -T btc-mvrv-monitor python -m live_mvrv.main report
```

No new decision/fill should appear just because of restart; a genuinely new accepted day can change state. Wait for `healthy`, confirm the two intraday services still run, and save a backup using a NEW filename:

```bash
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml exec -T btc-mvrv-monitor python -m live_mvrv.main backup --destination /data/operator-backup-UNIQUE.sqlite
```

Replace UNIQUE with your timestamp, copy that online backup off-server, and verify restoration in a separate test volume. Do not copy only an active `.sqlite` while ignoring its WAL. Do not use `down -v`, `docker system prune --volumes`, or mount an acceptance fixture as production.

## 5. Phone acceptance and monitoring

Follow `TELEGRAM_SETUP.md` and run the isolated `telegram-test` command before relying on phone alerts. Verify it arrives at the correct phone/chat and that failed sends remain retryable. API acknowledgement alone is not confirmation of a phone push notification. Do not invent a buy signal to test messaging or record a fill merely to clear an alert. The test command never opens the trading DB or starts its worker.

Daily reports must retain fresh accepted data and no persisted errors. Review any revision/gap warning, missed paper close, prolonged data outage or notification problem. Daily scheduling uses UTC: first attempt after 01:00 UTC (08:00 WIB), hourly retries as needed. The VPS can run unattended, but operational alerts require a reachable configured recipient.

To pause ONLY this worker while keeping all state:

```bash
docker compose -p btc-mvrv-dual --env-file .env -f compose.yaml stop btc-mvrv-monitor
```

This rollout adds one container and one dedicated named volume. It does not modify the trading rules, intraday feeds/databases or exchange account.
