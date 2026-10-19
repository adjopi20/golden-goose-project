# Operator-run real-money execution diagnostic

This is NOT paper trading, a strategy signal, or a profitability test. The user
must run it. The assistant has not run these commands or sent an entry.
Normal strategy execution/new-entry switches remain OFF. Existing paper services
and on-chain services must remain unchanged. No leverage/margin settings change.

## Fixed bounds

- ETHUSDC only; operator explicitly chooses long or short.
- Allocation 200 USDC; planned price risk at most 0.25% = 0.50 USDC per scenario.
- Notional capped by the shared engine at 5x allocation (1,000 USDC), venue limits,
  available margin, and configured exchange leverage. Rounding never raises risk.
- Fees, funding, slippage, and stop gaps are additional. A very tight stop can
  produce a much smaller price-risk budget than 0.50 while still incurring fees.
- Two separate invocations can create TWO trades, with combined planned price
  risk up to 1.00 USDC plus costs. They are not one combined 0.50 budget.
- One saved entry per scenario/direction, 90-second deadline from preparation.
  A repeated identical command only reconciles the original trade; never delete
  its database to retry. No strategy qualification, NY window, POC, EMA sizing,
  or actual 1R target is being tested by this diagnostic.
- Native close-all STOP stays in place while closing. If protection cannot be
  confirmed, shared emergency reduce-only closure runs. This cannot guarantee
  closure during exchange/network failure. Monitor Binance directly.

## Scenarios

`partial-time`: initial stop 10 bps from executable quote. One 10% partial LIMIT
GTC is placed just outside current best ask/bid, not a distant profit target.
After 15 seconds, any unfilled portion is replaced ONLY after confirmed
cancellation with a marketable reduce-only LIMIT GTC, priced two ticks through
the current bid/ask. This can still remain unfilled if the book moves. Market
reduce-only closes the remaining position at the 90-second deadline.

`stop`: initial stop 2 bps from executable quote; no TP is sent. Observe native
STOP triggering/fill. If the market does not reach it, time exit closes at 90
seconds and `native_stop_fill_observed` remains false. STOP rejection may instead
cause emergency close. None of these outcomes is falsely reported as a STOP fill.

MARKET entry/time exit ordinarily remove liquidity. Ordinary GTC LIMIT can be
maker, taker, or partly both. We intentionally keep production's GTC semantics,
not substitute post-only GTX. Actual classification uses each Binance `userTrades`
`maker` flag; no order-type inference. An unfilled maker-intent order is NOT proof
of a working maker fill. Official definitions:
[order parameters](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order),
[actual trade records](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Trade-List).

## Deploy code, without activating the worker

Commit/push ONLY these reviewed diagnostic files from laptop PowerShell first
(do not use `git add .`; unrelated research edits exist):

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$files = @(
  'live_engine/adapters/binance_futures_execution.py',
  'live_engine/futures_execution.py',
  'live_engine/Dockerfile.futures-executor',
  'live_engine/Dockerfile.futures-executor.dockerignore',
  'models/trend_following_preny_profile_15m/pilot_order_test.py',
  'models/trend_following_preny_profile_15m/pilot_stop_probe.py',
  'models/trend_following_preny_profile_15m/pilot_live_smoke.py',
  'models/trend_following_preny_profile_15m/deploy/ETH_PILOT_EXECUTOR.md',
  'models/trend_following_preny_profile_15m/deploy/ETH_LIVE_SMOKE.md',
  'tests/test_futures_execution.py',
  'tests/test_pilot_order_test.py',
  'tests/test_pilot_stop_probe.py',
  'tests/test_pilot_live_smoke.py'
)
git add -- $files
git diff --cached --stat
# Verify the staged list contains ONLY the files above before committing.
git commit -m "Add bounded operator-run ETH execution smoke diagnostics"
git push origin deploy/orb-live-agent
```

On NEW VPS
139.190.102.183, as root in `/root/golden-goose-project`:

```bash
git pull --ff-only origin deploy/orb-live-agent
export GG_PILOT_COMMIT=$(git rev-parse HEAD)
pilot=models/trend_following_preny_profile_15m/deploy/pilot.executor.yaml
credentials=models/trend_following_preny_profile_15m/deploy/pilot.executor.credentials.yaml
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" build eth-executor
```

Do NOT run `up` or arm production policies. Building a new image does not replace
the existing OFF worker. Do not print expanded Compose credentials or key files.

## Run first scenario (keep SSH connected)

Choose direction solely for this mechanical test; the command does not predict
market direction. It will spend real money on fees and can lose price risk.

```bash
read -r -p 'Diagnostic direction (long/short): ' direction
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" \
  run --rm --no-deps -T --entrypoint python eth-executor \
  -m models.trend_following_preny_profile_15m.pilot_live_smoke \
  --acknowledge-real-smoke I_AUTHORIZE_REAL_ETH_SMOKE_200_USDC \
  --scenario partial-time --direction "$direction"
```

Wait for its JSON report. Do not start the next scenario unless `flat` and
`ledger_reconciled` are true and BOTH open-order counts are zero. If not, inspect
Binance immediately and rerun the EXACT same command to reconcile. Never delete
the DB, cancel unrelated orders, or start a fresh scenario to work around failure.

## Optional second scenario, only after reviewing the first report

This is a second real trade and incurs another set of costs:

```bash
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" \
  run --rm --no-deps -T --entrypoint python eth-executor \
  -m models.trend_following_preny_profile_15m.pilot_live_smoke \
  --acknowledge-real-smoke I_AUTHORIZE_REAL_ETH_SMOKE_200_USDC \
  --scenario stop --direction "$direction"
```

## Records, interpretation, recovery

Persistent volume files:

- `/data/eth-live-smoke-partial-time-<direction>-v1.sqlite`
- `/data/eth-live-smoke-stop-<direction>-v1.sqlite`
- Existing `/data/execution-200.sqlite` is read-only to the diagnostic guard,
  not the destination for test trades. The normal worker remains OFF.

SQLite FULL/WAL stores the request before HTTP, order statuses, every actual fill,
actual commissions by asset, realized PnL, funding records, wallet reconciliation,
request durations, native timestamps, quote snapshots and safe venue errors.
No raw headers, signatures, API key, PEM, or signed URL is logged.

Report quote metrics include bid/ask spread, estimated half-spread exposure,
signed implementation shortfall versus mid, and slippage versus executable quote.
These estimates overlap: they are NOT separate exchange charges. Do not add them
to actual commissions or subtract them again from realized PnL. Quote age is
reported; old snapshots are not contemporaneous execution-cost measurements.
Funding may be zero simply because the short test crosses no funding event.

`native_stop_ack_observed`, `partial_fill_observed`, `time_exit_observed`,
`native_stop_fill_observed`, and actual maker/taker counts describe what happened.
A zero process exit indicates clean/reconciled completion, NOT proof every
scenario/branch filled. No full live claim until actual reports are inspected.

Restart the exact same scenario/direction after disconnect/crash: no re-POST of
uncertain entry, original deadline unchanged, normal protection and closure
recovery apply. SIGINT/SIGTERM request cleanup, but power loss/SIGKILL/REST failure
can leave only native STOP protection; the native STOP cannot enforce time exit.
The diagnostic's foreground runtime is bounded (~150 seconds plus bounded HTTP
requests); unresolved final state needs operator attention. Do not treat timeout
as proof of rejection. Keep the session and DB; do not leave an uncertain trade.

Offline validation: 115 focused tests passed before delivery, including the shared
executor/preflight, probes, partial cancel race, restart, native STOP rejection,
fees/funding, and log-redaction checks. No real-money lifecycle was run by the
assistant as part of developing these tools.
