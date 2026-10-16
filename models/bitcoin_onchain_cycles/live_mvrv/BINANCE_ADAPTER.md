# Binance Spot adapter v1 — gated Testnet prototype

Historical v1 runbook. The 2026-10-16 production transport addition is documented in `PRODUCTION_DEPLOY.md`; production execution remains OFF in the prepared rollout. Earlier statements below that production POST is unavailable describe v1 before that addition. Do not mix environment credentials or volumes.

Status: implementation and offline tests, NOT live trading approval. Existing alerts containers, ledgers, .env and server are unchanged. No production order path exists. The only production action is `status`, which performs GET requests and never opens a trading database.

## Frozen strategy and execution contract

- Same hypothesis/rules as CONTRACT.md: fixed entry Z < 0 / exit Z >= 7; four-year past-only P5/P95; daily complete UTC observations from Coin Metrics. No CVDD, regression, leverage, new stop, rollover or tuning.
- One new Testnet ledger initialized once with explicit USDT capital, split 50:50. Testnet's faucet BTC and surplus USDT remain unallocated; no automatic sweeping or cross-branch transfer.
- BTCUSDT is the prototype pair, not a decision that production must use USDT. Stablecoin cash is not risk-free USD. Marks use the corresponding closed Binance UTC-day BTCUSDT candle; MVRV calculation continues using Coin Metrics market/realized-cap inputs.
- Orders are MARKET for Testnet mechanics, not zero-slippage/fee claims. Entry spends 99% of the branch's current cash, leaving a 1% reserve for fees. Exit sells the branch's rounded tradable BTC. Profit stays in the same branch and sizes the next purchase. Dust stays attributed and included in wealth; sub-minimum quantity dust does not block a subsequent entry.
- The existing future-UTC-close schedule is retained: a published daily signal targets the next UTC midnight (07:00 WIB), not a backdated fill at an already-known close. Worker checks every minute. New submissions more than one hour after the intended close expire. Actual fills use venue timestamps/prices; delays/partial fills/rejections are never reported as successful hypothetical fills.
- Evaluate MVRV daily at/after 01:00 UTC / 08:00 WIB, hourly retries until fresh input exists. Reconcile outstanding orders at startup, before evaluation, after POST, then on worker ticks. Once-daily-only reconciliation is NOT adequate for an outstanding order.
- Per-action durable intent is committed before POST. Stable client ID = `mv1_` + strategy action ID; venue order/trade IDs attach to that branch. A crash or timeout leaves an UNKNOWN intent. Query by client ID; never automatically resubmit even if the venue says not found. Binance can reuse a client ID after completion, so the client ID alone is not an exactly-once guarantee.
- Actual trades import atomically and idempotently. Cumulative qty/quote must match order response. Incomplete fill pages, stale responses, wrong attribution, negative branch balances and unknown status stop processing. Cancellation may retain a real partial position.
- BTC/USDT commissions debit their native assets. Average cost and realized quote PnL are tracked per branch, separate from mark-to-market return/DD. Nonzero BNB/other-asset commissions deliberately halt for accounting review; disable BNB fee payment for this prototype. `venue_trades` is the native-fee/quote-value source of truth, not the legacy `fills.fee` quote-only column. This is strategy accounting, not a tax report.
- Account inventory must equal both branch ledgers plus the recorded unallocated balance. New deposits/withdrawals/manual trades/Testnet resets produce a mismatch and pause new orders. No automatic allocation of extra deposits is implemented. Use a dedicated Testnet account; do not trade manually in it after funding.
- Key fingerprint, venue, capital and mode bind the new DB. A key rotation/reset needs reviewed migration/new test ledger; never silently reset/re-fund an old one. Existing alerts/paper DB conversion is refused.
- Transport explicitly allows only the used `/api/v3` routes. No `/sapi`, deposit, withdrawal or transfer path exists. `klines` is used directly; `uiKlines` is unnecessary and not allowed by the adapter.
- `exchangeInfo` refreshes filters and rate-limit definitions on each worker tick; order sizing uses those current filters. Transport retains used-weight/order-count response headers. HTTP 429/418 starts a no-request cooldown following `Retry-After`, with conservative fallback if missing/invalid (60 seconds / 24 hours). The ledger persists the worker cooldown across restarts. No automatic POST retries. This is not an account/IP-wide scheduler for other applications or simultaneous CLI processes: do not run another worker/once alongside this worker, or repeatedly run status during a ban.
- Completed attributed orders are checked against venue history once per UTC day, in addition to outstanding-order reconciliation and inventory checks. Missing/changed history pauses before any new submission without deleting/re-funding the ledger. It may indicate reset or history availability/retention issues; it does not prove the cause. If no orders exist and reset restores identical balances, reset is not observable from these endpoints. Never present historical Testnet returns across a reset as a continuous live record; retain the old ledger/backup and begin a separately reviewed Testnet epoch if needed.
- This does NOT add a drawdown cap or prove financial stability. Threshold-only exits can still experience large drawdowns. No live funds, deposits, withdrawals, margin, futures, API-key creation or server deployment is performed by these commands.

## 1. Obtain separate TESTNET credentials

For an Ed25519 key already registered on Testnet, follow [BINANCE_ED25519.md](BINANCE_ED25519.md) instead of the HMAC credential examples below. The existing HMAC path remains supported.

Use the official [Binance Spot Test Network](https://testnet.binance.vision/). Create Testnet API credentials there, not production exchange keys. Testnet funds are virtual and resets can occur. Never send either key/secret in chat.

From this folder, copy the example ONLY if the destination does not exist:

```powershell
if (-not (Test-Path -LiteralPath 'binance-testnet.env')) {
    Copy-Item -LiteralPath 'binance-testnet.env.example' -Destination 'binance-testnet.env'
}
```

Edit that file locally. Leave the existing `.env` alone:

```env
BINANCE_ENV=testnet
BINANCE_API_KEY=YOUR_TESTNET_KEY
BINANCE_API_SECRET=YOUR_TESTNET_SECRET
BINANCE_TESTNET_EXECUTION_ENABLED=false
INITIAL_CAPITAL=1000
TELEGRAM_ENABLED=false
```

Optional Telegram settings can use the verified bot, but remain disabled during initial checks to avoid confusing TESTNET alerts with the separate real-intent alerts worker.

## 2. Read-only authenticated check

```powershell
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose configuration failed' }
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml build
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml run --rm btc-mvrv-testnet status
```

Expected environment `testnet`, BTCUSDT, sufficient free USDT and no unrelated open orders. `status` has no POST, DB or Telegram send. Never print expanded Compose configuration with secrets.

Operator-confirmed local Ed25519 status and `once` on 2026-10-03 accepted authentication, virtual balances (1 BTC / 10,000 USDT), reserved 1,000 USDT 50:50, and returned WAIT for both branches using 2026-10-02 observations. There were no fills/orders and execution remained disabled. One funding notification remained queued while Telegram was disabled. This confirms initialization/evaluation, not exchange fill acceptance.

For a separate production READ-ONLY inspection, use a read-only production API key in another ignored env file and `docker run --rm --env-file YOUR_READONLY.env --entrypoint python golden-goose/btc-mvrv-dual:binance-testnet-v1 -m live_mvrv.binance_main status`. Set `BINANCE_ENV=production_readonly`; do NOT pass that file to the Testnet Compose service, which pins the Testnet host. There is no production execution option.

## 3. Initialize and evaluate with execution STILL OFF

```powershell
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml run --rm btc-mvrv-testnet once
```

This reserves virtual capital once, fetches full MVRV history and a current closed Binance candle, and evaluates the two frozen rules. It does not submit orders while the switch is false. An existing signal can create a pending future-close action; it is not a fill. Report currency must be USDT, execution Testnet, `real_money=false`. Do not run `once` alongside a resident worker.

## 4. Testnet worker only after read-only checks pass

Deliberately set `BINANCE_TESTNET_EXECUTION_ENABLED=true` in the separate Testnet file, then:

```powershell
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml up -d
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml logs --tail 50 -f btc-mvrv-testnet
```

Status/report:

```powershell
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml exec btc-mvrv-testnet python -m live_mvrv.binance_main report
```

Pause the whole worker with the same scoped Compose `stop`. To disable only new orders, change the switch to false and recreate with scoped `up -d`; reconciliation continues. Unknown intents, external inventory changes and unsupported fees require investigation, not deletion/reposting. Never run `down -v` to "fix" an account. No automatic cancel/retry, deposit reallocation or manual ledger repair command exists in this prototype.

## Acceptance still required before any live adapter

Offline tests are not authenticated Testnet acceptance. Need keys and actual read-only check, current-source/mark parity, fill lifecycle, partial/cancel, unknown-POST recovery, restarts, native fees, inventory audit, backups and Telegram attribution. Real MVRV signals are sparse: do not change thresholds or inject historical/synthetic signals into the daily worker just to force an exchange trade. Synthetic offline fixtures are isolated tests, not OOS evidence. A separate explicitly authorized Testnet execution probe can test mechanics later without changing strategy rules.

Deployment on the existing server and production POST remain out of scope. Production would need separate approval, selected pair/budget, commission support, monitored reconciliation, backup/restore, order-size/slippage policy and a verified dedicated account inventory.

Sources: [Spot Testnet](https://developers.binance.com/docs/binance-spot-api-docs/testnet/rest-api), [Spot orders](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints), [account trades](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints), [filters](https://developers.binance.com/docs/binance-spot-api-docs/filters).
