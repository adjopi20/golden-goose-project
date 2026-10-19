# ETHUSDC monitored pilot activation — operator action only

The assistant prepared and offline-tested configuration; it has NOT activated
trading or placed a strategy entry. Running the activation command below enables
real-money autonomous ETHUSDC orders on qualifying future signals. Do not run it
on the old VPS or in a laptop environment.

## Scope and verified evidence

New VPS: 139.190.102.183. One live writer/service `eth-executor`; ETHUSDC only.
Old server, Binance/Lighter paper accounts, MVRV signals, collectors, strategies
and broker stay unchanged. No manual trading or other live model may share this
dedicated Futures account during the pilot. Do not add collateral/withdraw while
an active ledger is reconciling; unexplained balance changes block new entries.

Read-only preflight at 2026-10-19 17:11 WIB: wallet/available 200.73562479 USDC,
zero positions/regular/algo orders, no GET-only blockers, CROSSED leverage20
(inspected, not changed), maker rate0 and taker4bps. Source config/code identities
and execution source identity matched; source/worker heartbeats were fresh,
cursor22, no source entries pending, no production orders/fills, initial wallet
not initialized. These are timestamped observations, not permanent guarantees.

The two operator-run live diagnostics proved entry, actual maker partial,
market time exit, native STOP creation/trigger/child-fill and wallet reconciliation.
See [actual results](ETH_LIVE_SMOKE_20261019_RESULTS.md). Not all failures were
tested live: short side, forced crash, exchange/network outage, marketable-limit
fallback, and funding across settlement remain unobserved live. Do not call the
pilot bug-free or production-grade unattended operation. Keep Binance accessible
and monitor order/fill notifications and health; this chat is not a watchdog.

## Unchanged model contract

- Isolated allocation 200 USDC, not the whole 200.73562479 wallet.
- Existing frozen causal entry/evaluation; first qualifying entry only. No forced
  entry, no replay of old/disarmed/expired signals, no smoke-test price/deadline.
- Normal model price risk is **0.25% or 0.5% of allocation equity**, according to
  the existing direction-adjusted EMA200 distance tier (at least0.5% =>0.5% risk;
  otherwise0.25%). Initial budgets 0.50/1.00 USDC, NOT always0.50. Equity compounds
  only this ledger's actual PnL/fees/funding. Fees/slippage/funding are additional.
- Frozen POC STOP_MARKET, CONTRACT_PRICE, closePosition=true, priceProtect=false.
- TP1 10% at1R from actual entry; ordinary GTC LIMIT, actual maker/taker fees.
- Remaining90% original POC or next-day08:59 New York MARKET reduce-only.
- Notional maximum5x allocation equity plus venue/margin restrictions. Quantity
  rounds down; infeasible signals are not enlarged to satisfy minimum size.
- Same persistent `/data/execution-200.sqlite`; diagnostic DBs remain separate.
  Do not delete/reset source cursors, identities or ledger to force a trade.

The new live/paused JSON files differ from shipped OFF policy only in the two
execution switches; base Compose and baked policy remain OFF. The overlay requires
the exact additional CLI acknowledgement. 119 focused offline tests passed,
including ledger identity/cursor preservation and paused entry with active exit.
Compose merges were validated offline using dummy credentials, never real keys.

## 1. Laptop: publish exactly these new files

PowerShell, local main repo. Keep unrelated changes out of this commit. Check
`git diff --cached --stat` before committing; do not use `git add .`.

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$files = @(
 'models/trend_following_preny_profile_15m/deploy/pilot.ethusdc.execution.live.json',
 'models/trend_following_preny_profile_15m/deploy/pilot.ethusdc.execution.paused.json',
 'models/trend_following_preny_profile_15m/deploy/pilot.executor.live.yaml',
 'models/trend_following_preny_profile_15m/deploy/ETH_PILOT_ACTIVATION.md',
 'tests/test_pilot_activation.py'
)
git add -- $files
git diff --cached --stat
git commit -m "Add explicit operator ETH pilot activation and pause config"
git push origin deploy/orb-live-agent
```

## 2. New VPS: fetch, preserve ledger, validate and build (still OFF)

As root. Reinitialize variables after opening a new shell or `sudo su`.
Stop here if pull/backup/build/validation fails; do not skip a failed command.

```bash
cd /root/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_PILOT_COMMIT=$(git rev-parse HEAD)
pilot=models/trend_following_preny_profile_15m/deploy/pilot.executor.yaml
credentials=models/trend_following_preny_profile_15m/deploy/pilot.executor.credentials.yaml
live=models/trend_following_preny_profile_15m/deploy/pilot.executor.live.yaml
export GG_ETH_PILOT_ACTIVATION=I_UNDERSTAND_REAL_MONEY
unset GG_ETH_PILOT_POLICY

backup="/data/pre-live-$(date -u +%Y%m%dT%H%M%SZ)-$$.sqlite"
docker compose -f "$pilot" exec -T eth-executor \
  python -m models.trend_following_preny_profile_15m.pilot_worker \
  --config /app/config/pilot.json --database /data/execution-200.sqlite \
  --action backup --backup-to "$backup"

docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" config --quiet &&
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" build eth-executor &&
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" \
  run --rm --no-deps -T eth-executor --config /app/config/pilot.json --action validate
```

Expected validation: policy_valid=true, execution_enabled=true,
accept_new_entries=true, orders_submitted=0. This one-shot validation does NOT
start the owner or execute a signal. The previous OFF worker remains unchanged.
Copy the completed backup off the VPS later without copying secrets; preserve
the original145 ledger, both diagnostic DBs, and native-stop probe journals.

## 3. Operator activation — real strategy orders become possible

Only the human operator runs this command after reviewing the above results.
It replaces ONLY the existing `eth-executor` service, not other services. Do NOT
use `down`, `--remove-orphans`, a broad `up`, or start a second execution owner.

```bash
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" \
  up -d --no-deps --no-build eth-executor
```

Wait5–10 seconds, then verify:

```bash
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" ps
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" logs --tail=40 eth-executor
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" exec -T eth-executor \
  python -m models.trend_following_preny_profile_15m.pilot_worker \
  --config /app/config/pilot.json --database /data/execution-200.sqlite --action status
```

Require worker_armed="1", recent heartbeat, empty worker_error, no entries_blocked,
and healthy service/source. Zero immediate orders is normal: wait for a NEW valid
model signal in its existing NY window. Do not force entry or rerun smoke tests.
Send status/log output for review, not credentials or expanded Compose output.
If not healthy, pause new entries below and inspect before further trading.

## 4. Pause new entries, keep exits working

Do not disarm execution/remove credentials/stop the worker while a position is
open: that prevents time exit/reconciliation. Pause the entry gate instead:

```bash
export GG_ETH_PILOT_POLICY=./pilot.ethusdc.execution.paused.json
docker compose --env-file ./binance-production.env -f "$pilot" -f "$credentials" -f "$live" \
  up -d --no-deps --no-build eth-executor
```

Execution stays enabled; existing STOP, TP, reconciliation and deadline continue.
To resume later, review status first, `unset GG_ETH_PILOT_POLICY`, then repeat
the same scoped `up`. Mode changes preserve ledger identity and source cursor.
Only return to the base OFF deployment after actual positions/order reconciliation
are flat and clear. Native STOP remains exchange-side during a server outage,
but timely time exit still requires a working process/network. Docker health
alone is not an external alarm or automatic repair guarantee.
