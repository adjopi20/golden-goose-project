# BTC MVRV production deployment — execution OFF

Prepared 2026-10-16. One independent container and volume; frozen 0/7 and past-only 1460-day P5/P95 branches, initially 50:50. No CVDD, LLM/API model, contributions, leverage or periodic rebalancing. This is preparation, not evidence that the VPS or production account has been tested.

## What OFF means

The worker reads the REAL Binance Spot account, assigns only the configured initial USDT budget in its own ledger, computes daily signals and sends configured Telegram proposals. It does NOT submit buy/sell orders while OFF. It does not simulate fills either. Real balance changes appear only after actual attributed venue fills.

Missed proposals expire after their planned execution time while OFF. They are not queued for backfilled purchases on later activation. No historical BTC position is imported. Existing BTC/excess USDT stay unallocated. Current contract uses BTCUSDT; USDT is not risk-free USD.

## Required before first launch

1. Choose TOTAL strategy capital in USDT; each branch receives half once. Do not change this value after initialization. Future gains remain in their original branch; no cross-branch transfer.
2. Use a dedicated Binance account/subaccount for this engine's BTC/USDT activity. A different API key on the same account does NOT isolate balances. Concurrent manual/intraday trading, deposits or withdrawals in BTC/USDT will trigger inventory mismatch and pause new orders. Deposit assigned capital BEFORE first launch. Later top-ups require reviewed accounting, not changing INITIAL_CAPITAL.
3. Register a separate Ed25519 public key on the PRODUCTION Binance account. The registered Testnet key will not authenticate production. Keep the PEM outside the repository/image; never send key contents/token to chat. Minimum needed initially is account read access. Do not enable withdrawals. Use a server-IP restriction; trading permission is a later explicit activation step.
4. This Compose file expects an unencrypted PEM readable by container UID 1000. If using an encrypted PEM, stop and prepare/test a separate read-only passphrase mount; this template does not mount one.
5. Configure Telegram locally in the new production env file. Do not automatically copy the old Testnet env/key/ledger. No published port is needed.

## Server layout

```text
/opt/golden-goose/btc-mvrv/                 app files + ignored binance-production.env
/opt/golden-goose-secrets/                 restricted secrets directory
  btc_onchain_binance_production.pem       read-only bind into container
Docker volume btc-mvrv-production_production_data:
  binance-production.sqlite               new real-account ledger
```

Transfer/extract the provided code-only archive into a new application directory. Keep previous releases for rollback; do not overwrite a running installation or mount Testnet volumes. Inspect existing containers, disk and RAM before adding this service:

```bash
docker ps
docker stats --no-stream
df -h
free -m
```

After separately transferring the production PEM to the secrets directory, give the file only its reader's permissions. For the proposed exact path:

```bash
chown 1000:1000 /opt/golden-goose-secrets/btc_onchain_binance_production.pem
chmod 600 /opt/golden-goose-secrets/btc_onchain_binance_production.pem
```

Do not change SSH keys or recursive ownership of the server/repo. Docker can bind the individual PEM from a root-restricted parent directory; the image runs as UID 1000.

## Configure and deploy OFF

Inside the application directory:

```bash
umask 077
test -e binance-production.env || cp binance-production.env.example binance-production.env
chmod 600 binance-production.env
nano binance-production.env
```

Fill API key, exact PEM path, total USDT capital, and Telegram settings. Keep these unchanged:

```ini
BINANCE_PRODUCTION_EXECUTION_ENABLED=false
BINANCE_PRODUCTION_ACK=
BINANCE_PRODUCTION_MAX_ORDER_USDT=0
```

Do not source the env file as a shell script. Unset any shell overrides for these settings/credentials before deployment: Compose shell values take precedence over --env-file. Never print expanded Compose configuration containing secrets.

Run one script:

```bash
bash deploy-production.sh
```

The script validates Compose, builds, checks the resolved execution-OFF values, performs read-only authenticated status, and starts ONLY project `btc-mvrv-production`. It refuses an armed first rollout. All capital allocation is accounting within this engine, not a Binance transfer or purchase. If initial funding/data/permissions fail, investigate; never erase the volume to make it healthy. Full-origin Coin Metrics warmup is fetched automatically; no need to wait years for a signal to test mechanics.

## Acceptance and daily operation

```bash
docker compose --env-file binance-production.env -p btc-mvrv-production -f compose.binance-production.yaml ps
docker compose --env-file binance-production.env -p btc-mvrv-production -f compose.binance-production.yaml exec -T btc-mvrv-production python -m live_mvrv.binance_main report
docker compose --env-file binance-production.env -p btc-mvrv-production -f compose.binance-production.yaml logs --tail 30 btc-mvrv-production
```

Expect `execution=binance_spot_production`, `real_money=true`, `execution_enabled=false`, two accounts each holding half assigned cash, and zero fabricated orders/fills. WAIT is normal when entry is not satisfied. Fresh successful data clears only the data error; successful exchange reconciliation clears exchange error; Telegram retries clear notification error. Unknown orders and mismatched balances do not clear merely on a timer. No blind POST retry.

Data evaluation begins at/after 01:00 UTC (08:00 WIB), hourly retry if unavailable. Reconciliation/heartbeat are every minute. After a signal's actual publication, orders are planned for the next eligible UTC midnight (07:00 WIB), not backdated. These timings preserve the tested operational variant; they are not guaranteed optimal execution prices.

Test Telegram without evaluating signals or opening the trading ledger:

```bash
docker compose --env-file binance-production.env -p btc-mvrv-production -f compose.binance-production.yaml run --rm --no-deps --entrypoint python btc-mvrv-production -m live_mvrv.main telegram-test
```

Confirm the explicitly labelled TEST on your phone. This is not an interactive Telegram command bot. Reports are CLI/ledger based; proposals and fills go to the configured chat. Compare report balances, pending IDs and counts before/after a scoped restart; no order/fill should be created merely because of restart.

## Backup and pause

```bash
docker compose --env-file binance-production.env -p btc-mvrv-production -f compose.binance-production.yaml exec -T btc-mvrv-production python -m live_mvrv.binance_main backup --destination /data/backup-UNIQUE.sqlite
```

Replace UNIQUE with a fresh timestamp. Copy this completed online backup off-server (use `docker compose ... cp btc-mvrv-production:/data/backup-UNIQUE.sqlite ./backup-UNIQUE.sqlite`, then transfer it separately). Verify restore into a NEW isolated volume with execution OFF. Never copy only the active SQLite file while ignoring WAL; never use `down -v` or prune volumes. Pause just this service with the same Compose flags followed by `stop btc-mvrv-production`.

## Later activation is a separate decision

No real transaction is authorized by this preparation. The code requires production execution true, exact acknowledgement `I_UNDERSTAND_REAL_MONEY`, positive MAX_ORDER_USDT and the venue's trading permission. The cap applies per order (sell value estimated at the current bid); an appreciated position can exceed it and block its exit until an operator reviews the cap. It is NOT a maximum-drawdown stop or a guaranteed realized sale-price ceiling. MARKET execution has spread/slippage; buy spends 99% of branch cash and sell rounds attributed BTC to the venue lot grid. Do not silently clip orders to the cap, because that would change the strategy.

Before enabling, review pending proposals, assigned budget, production filters, fee settings, phone delivery, backup/restore and live account reconciliation. Disable BNB/third-asset fee discounts: the native fee ledger supports BTC and USDT only. Current production commission settings are checked before any production intent; missing settings or enabled third-asset discount block submission. Key rotation requires reviewed ledger migration because the ledger is bound to the key fingerprint. Do not point production at another environment's DB.

The frozen models can both hold BTC simultaneously; 50:50 models is not 50% maximum BTC exposure. No new stop-loss was added; historical large drawdowns remain possible. Preparing the software does not prove the future edge.

Official references: [Spot REST security and timeout handling](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/request-security), [commission settings](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints), [Spot orders](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints).
