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

## Account and test-only validation, 2026-10-19

Direct read-only server checks cleared all preflight blockers for the 200-USDC
allocation. At 12:31 WIB: wallet/available USDC 202.31862409, one-way/single-asset,
ETH CROSSED with venue leverage 20 (not changed), zero positions/regular/algo
orders, account-specific maker rate 0 and taker rate 0.000400. These are current
API observations, not fixed future rates or a claim of an actual paid commission.
One earlier transport timeout could not be reproduced; all ten GETs then returned
200, typically 93-194 ms after the first connection. No DNS/timeout was changed.

`pilot_order_test.py` is a separate, non-matching permission probe. Its only POST
destination is the literal `/fapi/v1/order/test`; it cannot create/cancel a real
order or change account settings. It rechecks preflight, submits test-only
MARKET BUY/SELL using minimum exact-split geometry, then checks the account is
still flat/order-free and the USDC wallet unchanged. It never opens an execution
DB and never arms the worker. No retry or secret/URL logging is used.

Production returned `executedQty:""` on the test-only dummy response. The helper
treats that field as unavailable, NOT as an observed zero fill; this special case
does not alter parsing of real-order responses. Final BUY/SELL test requests for
0.010 ETH were accepted, approximately 293/99 ms. Wallet before and after:
202.31862409 USDC; positions, regular orders and algo orders all zero. These test
requests do not enter Binance's matching engine. Successful test requests are
evidence for the authenticated test endpoint, not proof of native order fills,
conditional-stop acceptance or live recovery. Earlier parser/schema diagnostic
attempts were also test-only, never matching-engine orders.

The validation helper adds 11 offline cases; the combined focused suite is now
78 passing tests. The earlier expanded-regression results above remain a separate
record. No native stop was placed, no genuine trade filled, and the worker's
execution/new-entry switches remain OFF. Native stop create/query/cancel requires
an explicitly authorized real conditional-order probe or a suitable demo test.

After this helper is included in a reviewed/rebuilt image, it can be invoked as
`python -m models.trend_following_preny_profile_15m.pilot_order_test --config
/app/config/preflight.json --private-key-file /run/secrets/key.pem` in a one-shot
credential-mounted container, with the strict preflight JSON mounted read-only.
For this check the tested source was streamed into a temporary container using
the existing image; no long-running service/image was replaced. Never print the
expanded credential-bearing Compose config.

### Native close-only stop probe: NOT passed, activation remains blocked

Operator explicitly authorized exactly one production close-only SELL stop while
flat, then query/cancel, without opening a position. `pilot_stop_probe.py` reused
the production adapter and response validator. A fsynced, single-writer JSONL
journal stores the request/ID before HTTP. Existing journals are recovery-only:
the helper never re-POSTs after a restart or uncertain outcome. Cleanup DELETEs
are limited to that same saved ID; no bulk cancellation, entry, leverage or margin
mutation exists in the helper. The running strategy/executor were not replaced.

On 2026-10-19 at approximately 12:52 WIB, the single POST for
`ggp-stopprobe-c18d9b16d5fc6777`, trigger 2183.63 USDC (90% of the then-current
bid, rounded down), returned HTTP 400 / code -4509. This code is not in the
reviewed official USD-M error reference. The production adapter conservatively
kept the creation outcome uncertain and redacted the venue error message. Do not
infer the exact reason from the code, add it to an accepted-rejection whitelist,
repeat the POST, remove close-only protection, or open a position just to make
the test pass. In particular, "requires an open position" remains an unconfirmed
hypothesis for this account response, not an established diagnosis.

The original query/cleanup found no order; cancellation could not be positively
confirmed because there was no native order to query. A separate GET-only
reconciliation at 12:53 WIB found the saved ID absent, zero positions, zero
regular/algo orders, unchanged wallet 202.31862409 USDC, worker_armed=0, no worker
error, and zero execution-ledger orders/fills. There was exactly one stop POST;
the helper did not resubmit. The persistent server journal is
`/data/native-stop-probe.jsonl` in the executor volume. Keep it; do not overwrite
it to manufacture a second probe. No matching trade occurred or money changed.

Native stop acceptance/cancellation/fill recovery remains UNVALIDATED. Further
diagnosis should obtain trustworthy rejection detail or a suitable demo result;
any additional production POST or real-position test needs new explicit operator
authorization. No automatic entries may be armed on this evidence alone.

A subsequent STOP_MARKET close-only diagnostic sent only to the non-matching
`/fapi/v1/order/test` returned HTTP 400 / -4120 with an instruction to use the Algo
Order endpoint. No second native stop was submitted. Positions, regular orders
and algo orders remained zero. This confirms the legacy test route cannot stand
in for native Algo Order validation; it does not establish the reason for -4509.

### Rejection diagnostics correction (local; not yet deployed)

The adapter previously discarded every Binance `msg`; this was redaction, not
encryption, and the discarded -4509 explanation cannot be recovered from the old
journal. It now keeps a short, sanitized venue reason in `VenueError`, execution
audit events, and stop-probe reports/journals. The non-matching order-test helper
uses the same sanitizer. Credentials/signatures, signed URLs, private-key markers,
oversized/malformed messages, and token-like strings are suppressed. Raw HTTP
exceptions, request bodies/headers, and secret material remain unlogged.

This change does not modify the rejection-code whitelist, retry policy, entry
logic, allocation, or arming switches. In particular, -4509 still has an uncertain
creation outcome; a test fixture's example wording is not evidence of Binance's
actual reason. Existing probe journals remain recovery-only. No new production
order or worker activation was performed to implement/test this correction.

### Authorized second stop probe: flat-account restriction confirmed

On 2026-10-19, after explicit operator approval for one additional close-only
probe, the tested adapter/probe source was loaded only into a temporary container
using the existing 7ad6284 image. The long-running worker was not replaced. The
second journal is `/data/native-stop-probe-authorized-20261019-02.jsonl`; the first
journal was preserved. One POST used client ID `ggp-stopprobe-52d74e5fe313baad`,
SELL STOP_MARKET closePosition=true, CONTRACT_PRICE, trigger 2186.89 USDC.

Binance returned HTTP 400 / -4509 with this sanitized explanation:
"Time in Force (TIF) GTE can only be used with open positions. Please ensure that
positions are available." This establishes the reason for this request: the
close-only stop cannot be validated on this flat account. The prior hypothesis
is now supported by the actual venue response; it is not inferred from an
undocumented code alone. No explicit TIF field was sent by our STOP template.

The helper's outcome remains conservatively UNKNOWN (the code whitelist was not
changed), and create/query/cancel validation did NOT pass. Queries and bounded
cleanup found no native order; do not claim a confirmed cancellation. Independent
GET-only reconciliation at 06:10:10 UTC / 13:10 WIB found zero positions, zero
regular/algo orders, the saved ID absent, unchanged wallet 202.31862409 USDC,
worker_armed=0, both policy switches false, empty worker error, and zero execution
ledger orders/fills. Both journals contain exactly one `sending` event each.
Second journal SHA256:
`6443002bfda82cbb31dda9934e3ccba098bd5e99b89cc8ab1fcbc18263de1f9b`.

The production lifecycle already checks for an actual position before `_protect`
and attempts emergency reduce-only closure if protection cannot be confirmed.
This code path is offline-tested, not yet verified with a live position. Do not
remove closePosition, invent a non-close-only test, or open a real position under
this stop-only authorization. A demo validation or separately authorized,
tightly bounded real-position pilot is a different next step. Automatic entry
activation remains OFF.

### Operator-run full execution smoke tool (not run live by assistant)

See [ETH_LIVE_SMOKE.md](ETH_LIVE_SMOKE.md) for the separately acknowledged real-
money diagnostic. It reuses the shared FuturesExecutor/Store/Orders lifecycle,
with model-specific manual entry, nearby TP/STOP and a 90-second deadline in
separate diagnostic databases. Two scenarios cover partial/time exit versus
native-stop observation; each permits one entry at 200 USDC allocation and
0.25% planned price risk, plus execution costs. Production signal validation,
POC, sizing and policy switches are unchanged. Actual maker/taker flags, fees,
funding, quote estimates, request timing, order statuses and recovery are logged.
115 focused offline tests passed. No real entry/exit or live success is claimed.
