# ETHUSDC native executor — implementation checkpoint, 2026-10-19

Actual execution code is implemented, not only an order-style note. **It remains
DISARMED. No real/test order, account-setting change, transfer, server deployment
or service restart was performed for this change.** This checkpoint does not
declare the system production-ready or authorize the first live transaction.

Allocation was increased by the operator from 145 to **200 USDC** on October 19.
The reported wallet is about 202 USDC; that does not change the model allocation
to 202. Native account balance is not assumed verified from that report.
Changing allocation changes the journal's frozen identity. If a 145-USDC journal
was already initialized, the 200 config must refuse it; do not delete/rewrite the
DB or silently migrate a funded ledger. Inspect its orders/fills/initialization
state first, and preserve the old artifact. No automatic migration is supplied.
After confirming the original journal is DISARMED with no orders/fills and no
initial wallet, this allocation uses `/data/execution-200.sqlite` in the same
persistent volume. The original `/data/execution.sqlite` and its backups remain
untouched. Worker, healthcheck, status and backup must all use the 200 filename.

## Unchanged model and independent execution owner

Existing frozen Binance strategy DB (read-only)
→ selected ETH causal proposal + matching evaluation snapshot
→ separate `server-live` intention
→ new execution journal
→ authenticated Binance orders/fills, only when separately armed.

The existing evaluator, selectors, EMA200 risk tiers, paper ledger, Lighter and
MVRV services are unchanged. Paper fill prices are never treated as real fills.
The bridge is bound to producer code/config identities in
`pilot.ethusdc.execution.json`; changes fail closed and require review. It checks
the causal snapshot hash, profile POC, completed EMA200 snapshot and risk tier.

First startup anchors the latest proposal row, rather than trading historical
signals. New proposals seen while disarmed are recorded DISARMED and cannot be
re-entered after activation. Missed/expired sessions are not backfilled. A source
heartbeat over 180 seconds blocks new entries; an existing position's stop,
reconciliation and time exit do not depend on the evaluator remaining healthy.

## Contract and implemented operational choices

| Component | Behavior |
| --- | --- |
| Allocation | 200 USDC, independent execution DB; only ETHUSDC |
| Price-risk | 0.25% or 0.5% of this allocation's equity: initially 0.50/1.00 USDC before costs |
| Entry | MARKET, first selected candidate, within existing eligibility/5-minute expiry and noon NY limit |
| Initial stop | Frozen POC, native STOP_MARKET, entire remaining position |
| TP1 | 10% at 1R from actual average entry; ordinary GTC LIMIT, reduce-only |
| Remainder | Original stop or MARKET reduce-only at next-day 08:59 NY |
| Notional cap | 5 × allocated equity; also constrained by venue and available margin |
| Accounting | Native realized PnL, each fill's actual commission/asset/maker flag, and funding |

Maker/taker is read per fill. A TP LIMIT can fill as taker: no zero-fee assumption,
maker-only GTX requirement or unapproved market fallback. MARKET entry/exit do
not promise a price. Fees, slippage and funding remain outside price-risk.

Two operational details are **implemented defaults to review before activation**:

1. Stop uses `CONTRACT_PRICE`, `closePosition=true`, `priceProtect=false`. This
   follows the contract-trade price, covers residual size after partials, and
   avoids a price-protection delay. It is not a mark-price stop. With the dedicated
   account premise, a foreign/manual ETH position must never be added.
2. Stop is rounded outward to venue tick. Entry quantity is rounded DOWN to ten
   times the common quantity step so an exact 10% TP is feasible. TP is rounded
   outward to at least 1R. Risk is never enlarged to satisfy minimum size. An
   infeasible signal is skipped/blocked; a terminal partial entry that cannot
   support the split is safety-closed, not silently made a full runner. Margin
   mode/leverage are inspected, never changed automatically.

## Recovery and safety behavior

- Commit order identity/template before HTTP. A crash after marking SENDING is
  ambiguous even if HTTP never started. Query the same client ID; never blindly
  repeat the POST. "Order not found" does not prove a timeout was rejected.
- Confirm a native protective stop before TP. Any observed partial entry already
  receives a close-position stop. Rejected/unconfirmed protection causes an
  emergency reduce-only close; this is a safety exit, not a new alpha rule.
- Time exit cancels TP/unfinished entry, re-reads actual position quantity, and
  keeps native protection until flat. Reduce-only prevents TP/stop/close races
  from reversing the position. Partial closes are reconciled with at most three
  terminally resolved close attempts; unresolved requests are not reposted.
- Reconcile native executions, all attributed fills, signed net quantity, wallet
  and funding before finalizing CLOSED. Incomplete history remains pending.
  Manual transfers/foreign trades or non-USDC paid fees block new entries rather
  than silently changing the allocation. A changed API identity requires review.
- Separate WAL/FULL SQLite journal, OS single-writer lock, deduplicated native
  fill/funding IDs, consistent online backup. Never overwrite/reset this DB or
  copy just a running SQLite main file. A local lock is NOT a distributed lease:
  only the new VPS may run this live writer; the old VPS stays paper.
- Pausing **new entries** must leave execution enabled for an open lifecycle.
  Do not remove credentials/disarm while a position is open: that also disables
  risk-management mutations. Server failure still prevents timely time exit;
  native stop is protection, not a guarantee of a deadline fill.

## Diagnostic data: same journal, no additional market subscription

The `audit` table now timestamps events in local UTC epoch milliseconds. It
records intention receipt (with signal/feature/eligibility times), order
preparation, durable send start, first acknowledgement or status/quantity change,
and first observation of each native fill. Submit failures record sanitized HTTP
status/venue code and duration. Unchanged successful polls and duplicate fills
do not add diagnostic events. No API key, PEM, signature or signed request URL is
stored in these events.

Submit/query durations use a monotonic clock, measured around the client call
(including signing/HTTP/parsing), and cannot be interpreted as pure network RTT
or Binance matching-engine latency. Query durations are retained only when an
order's observed state/quantity changes, not as an all-requests network monitor.
No automatic latency-based sizing/entry rule has been added.

The frozen plan retains the executable bid/ask reference, quote's exchange time,
local observation time and clock offset estimate. Native response/fill times,
actual entry versus reference slippage, actual commissions/maker flags, realized
PnL and funding remain available in the same journal. Together these support
auditing signal-to-dispatch delay, dispatch-to-response duration, approximate
stop-confirmation delay, spread, price slippage and delayed fill detection.

Clock offsets must be considered when comparing venue and local times. Fill time
is the exchange's field; "first observed" is our REST poll receipt, which may be
later (the loop waits two seconds between cycles, plus request time). A late
observation is not proof that Binance filled late. The BBO snapshot is not full
depth history, so it cannot separate every cause of impact/adverse selection.
These observations can identify a suspected bottleneck, not prove that latency
caused worse execution without further evidence.

Harvest via the consistent online-backup command below. One SQLite artifact
contains sessions, plans, orders, fills, funding and diagnostic events; no raw
trade archive or order-book subscription is added. Do not upload credential
files together with the backup.

## Initial rollout: DISARMED, no credentials

Run on the **new server only**, after this exact reviewed change is committed,
pushed and pulled. These commands do not activate trading. Do not paste Markdown
fences into the shell. Do not use `down`, `--remove-orphans` or a broad `up`.

```bash
cd /root/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_PILOT_COMMIT=$(git rev-parse HEAD)
pilot=models/trend_following_preny_profile_15m/deploy/pilot.executor.yaml
docker volume inspect golden-goose-phase2-shadow_binance_strategy_phase4 >/dev/null &&
docker compose -f "$pilot" config --quiet &&
docker compose -f "$pilot" build eth-executor &&
docker compose -f "$pilot" run --rm --no-deps eth-executor \
  --config /app/config/pilot.json --action validate &&
docker compose -f "$pilot" up -d --no-deps --no-build eth-executor
docker compose -f "$pilot" ps
docker compose -f "$pilot" logs --tail=30 eth-executor
docker compose -f "$pilot" exec eth-executor \
  python -m models.trend_following_preny_profile_15m.pilot_worker \
  --config /app/config/pilot.json --database /data/execution-200.sqlite --action status
```

Expected: `execution_enabled:false`, `accept_new_entries:false`, metadata
`worker_armed:0`, and zero acknowledged orders/fills. `validate` checks policy,
not account access. Health checks worker/source freshness and unresolved order
state; healthy DISARMED does not mean exchange acceptance was validated.

The base deployment has **no API key environment or PEM mount**. The optional
`pilot.executor.credentials.yaml` is a future credential overlay only: it does
not switch ON either policy flag or supply the explicit acknowledgement. It can
use the existing server-only env/PEM without changing their contents/ownership.
Never print expanded Compose configuration or secret files. No activation
command is included at this checkpoint.

Online backup, without stopping the owner (choose a NEW filename each time):

```bash
docker compose -f "$pilot" exec eth-executor \
  python -m models.trend_following_preny_profile_15m.pilot_worker \
  --config /app/config/pilot.json --database /data/execution-200.sqlite \
  --action backup --backup-to /data/execution-backup-YYYYMMDD-HHMM.sqlite
```

Copy the completed backup off the volume with `docker cp` using the container ID
from `docker compose -f "$pilot" ps -q eth-executor`; never mount a restored backup
beside another active owner or retroactively replay past entry intentions.

## Verification and remaining gates

Offline tests cover signing/arming, long/short quantities, expiry, native stop
rejection/unknown status, partials, actual-fee accounting, funding, restart at
multiple durable boundaries, no duplicate entry, TP/stop/deadline races, source
bootstrap, no paper-ledger conversion, writer lock and online backup.

Local verification: **67 executor/preflight tests passed**. Expanded regression:
**152 passed, 3 skipped, 1 pre-existing failure**. The failure is
`test_pure_runtime_and_core_have_no_io_dependencies`, because existing
`runtime/observer.py` imports `live_engine.state_store.encode`; the same import is
in base commit `636bede`, and no evaluator/observer code was changed. The three
skips require a disposable local NATS integration instance. Compose base config
validates. No Docker image build or exchange/native-order acceptance was tested.

Before activation: review the stop/rounding policy above; validate the image on
the server, current private account/USDC/filter state, actual TRADE permission and
native conditional-order schemas/behavior using an explicitly authorized demo or
controlled execution test; perform restore/reconciliation and actionable-alert
drills; then obtain separate authorization for the first real-money order.
Mocks cannot prove matching-engine behavior. There is no external alert delivery
or cross-VPS distributed account lease in this patch. REST reconciliation is
bounded polling, not a certified private-user-stream execution system. No profit
or risk-limit fill guarantee is implied.

References: official Binance USD-M
[New Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order),
[New Algo Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order),
[Account Trade List](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Trade-List).
