# Live execution incident lessons

This is the maintained incident-learning record for live-model work. Read it
before creating or changing a live execution package. Keep strategy rules in
model policy and execution/recovery mechanics in the shared engine. Credentials,
raw account payloads and production journals never belong in this record.

## Required workflow for every live bug

1. Preserve timestamps and read-only evidence. Identify the exact deployed image,
   source hash, model, account group, order role/state and affected account peers.
2. Separate observed facts, reproducible defects and historical attribution that
   remains uncertain. Class names alone are not a sufficient diagnosis.
3. Trace the persisted intent, client ID, native order, fills, signed position,
   protector and pending setup. Determine whether retry changes the input.
4. Fix shared behavior at its shared boundary. Enumerate all models using it and
   all peers reading that model's journal for margin/admission.
5. Add a regression using realistic responses, missing optional fields, partial
   and delayed fills, restart and peer-account access. Keep native constraints,
   both directions, lost acknowledgements, stop races and time exits covered.
6. Verify exact runtime pins and the isolated image, then read-only broker
   identity/positions/orders before rollout. Preserve deterministic order IDs,
   journals and one writer; never arm old and new workers together.
7. Record implementation/test/deployment status separately. Inspect successful
   cycles, positions, protectors and setup outcomes after recovery; heartbeat or
   cleared worker_error alone is insufficient.
8. Append the incident, root cause, fix, tests and unresolved work here. Update
   the root AGENTS.md only for reusable rules, linking to this single record.

## Error classification and recovery contract

| Condition | Required response |
| --- | --- |
| Missing avgPrice or different order response shape | Reconcile actual order fills, match order/session and total quantity to executedQty; calculate a Decimal quantity-weighted price. Do not retry the same field access forever. |
| Partial/delayed fill history | Keep quantity/price unresolved until evidence matches; defer new risk/admission within the original setup validity window. Preserve native protection, time exits and position reconciliation. Never invent price/quantity. |
| Timeout/lost acknowledgement after order mutation | Preserve intent and client ID; query that ID plus fills/position. Never blindly resend POST or assume cancellation/rejection from absence alone. |
| HTTP 429 / Binance -1003 | Treat as a shared IP request-budget problem, including other services/accounts. Design centralized budgeting, bounded retries/backoff respecting venue delay, and prioritized protection/exit reconciliation. A catch-and-retry loop is not a rate-limit fix. |
| HTTP 418 / ban or non-retryable venue rejection | Respect the venue restriction/rejection; avoid request storms. Preserve structured status/code and confirmed protection; reconcile uncertain mutations. Do not retry every VenueError identically. |
| KeyError/TypeError/schema defect | Capture safe diagnostic location/phase and an allowlisted field name; reproduce with saved sanitized data. A runtime retry is containment, not repair. |
| Account identity, ownership or position mismatch | Block new exposure and alert; reconcile against broker state. Do not silently rewrite bindings or flatten an unexplained position. |

Never log credentials, signatures, signed URLs, arbitrary exception arguments or
raw account responses. Diagnostics should record an allowlisted operation/phase,
HTTP status, venue code and source location without local-variable dumps. Missing
such context must be stated as a limit on historical root-cause attribution.

## Incident 2026-11-15: ADD response missing avgPrice

Observed on the deployed e30af19 source release. Burst HYPE ADD filled 0.24 at
65.182 on 2026-11-14 17:10:43 UTC; cycle KeyError began at 17:10:44 UTC. The saved
FILLED response contains executedQty and orderId but no avgPrice. Actual Binance
trade fills were already journaled. ENTRY responses for other positions also lack
avgPrice, but ENTRY already derives its execution price from fills.

The shared PositionAddition path directly indexed native["avgPrice"]. A terminal
ADD is skipped by ordinary order polling, so repeated cycles reread the same
persisted response. The shared-account reserved-margin path made the same field
assumption. Thus the problem affects both an ADD owner and a peer attempting
admission in its collateral group. It is not a missing public price feed.

ADD owners: Burst HYPE, Quiet HYPE Tail, Quiet UNI. Other models can encounter
the defect through shared margin even without their own ADD. Absence of an error
before reaching the ADD/peer-admission branch is not evidence of compatibility.

Fix implemented in the local add-fill-reconciliation change: ENTRY, ADD and peer
margin share reconciled_order_fills. It matches order ID/session and fill quantity
to executedQty and calculates the weighted execution price. Complete persisted
fills survive restart without resubmitting ADD. Incomplete ADD fills defer peer
admission with EntryAdmissionBusy. Terminal ADD trailing and time/flat exits remain
available while fill reporting is incomplete. Reuse complete fills before requesting
history again. No schema migration, price fabrication or strategy rule change.

Regression: all three ADD variants, long/short, missing avgPrice, reopened journal,
one ADD only, native protection through time exit, weighted multi-fill ADD, delayed
fill history with continuing trailing/time exit, incomplete peer margin, and INJ
admission against a filled HYPE ADD without avgPrice. Existing order races,
rejections, partial exits and lost-acknowledgement regressions are also run.
The focused offline suite passes 118 tests in the pinned production runtime
(test_quiet_live_execution, test_burst_live_execution, test_futures_execution).
Six legacy paper-package pin failures reproduce on the unchanged baseline and
are recorded separately from this live-execution fix.

Status: implemented/tested locally; NOT deployed as of this incident note. Publication
and rollout remain pending. Do not infer deployment from a passing regression.

## Related incident: INJ KeyError and skipped pending setup

INJ did not simply recover its pending trade. Journal evidence on 2026-11-14 UTC:

- 17:15:00.689: a confirmed setup was persisted PENDING.
- 17:15:02.945: one cycle_error with error_type=KeyError, reason=null.
- 17:15:05.100: that setup was SKIPPED, reason=stale_or_entries_paused.

The runtime disables new_entries_ok after a cycle error. The PENDING branch then
skips the setup on the following cycle, even inside its original validity window.
That subsequent successful cycle clears worker_error; it does not restore the
skipped trade. No automatic historical entry/replay should be introduced by a fix.

Strongly supported explanation: INJ's admission read the HYPE filled ADD to reserve
shared USDT margin and encountered the same missing avgPrice. Replaying the old
reserved-margin method against the persisted HYPE journal reproduces KeyError
('avgPrice'); the new method succeeds on the same input. Historical logs contain no
traceback/field name, so the exact original throw site is not provable retrospectively.
The shared-margin fix and dedicated cross-worker regression cover this defect.

Remaining design work: distinguish deferrable admission/reporting issues from a
permanent block or expired setup. Test pending-intent outcomes as well as process
recovery before changing pause/retry policy. Do not silently extend validity or
backdate entries. Better safe diagnostics are recommended, not yet implemented.

## Separate unresolved issue: shared-IP request exhaustion

Worker heartbeat/order logs show HTTP 429, Binance -1003, with a reported IP limit
of 2400 requests/minute. Multiple active workers and audit/collector services share
that IP. Exceptions can occur both during order reconciliation and fill/history
reads (including the executor's finally block). Current cycles catch VenueError and
try again later; a successful cycle clears worker_error, but rate-limit errors recur.
Logs do not identify the endpoint for every cycle_error, so do not attribute every
historical VenueError to 429 without supporting status/code evidence.

The avgPrice patch is NOT a complete rate-limit fix. It avoids an extra ADD-history
read when local fills are already complete, but does not add a global request
budget, Retry-After handling or account-wide backoff. Those require a separate
measured change: inventory request weights/frequency across all services, coalesce
account reads, reduce redundant history reads, use authenticated order/fill streams
with REST reconciliation, and preserve a bounded priority path for protection/exits.
Test 429 recovery, cross-worker cooldown, uncertain submissions and pending setup
outcomes before deployment. Do not globally sleep the execution loop and thereby
suspend position safety checks.


## Shared Binance REST budget: baseline evidence and local fix (2026-11-15 WIB)

**Evidence.** Read-only VPS inspection found all seven workers on
`b901f9c7e2464a5d1ac2002895b500827bdcd07d` (the ADD fill fix), but the
collector and audits were on older images. On 2026-11-14 UTC a public `time`
GET returned HTTP 200 at 19:59:22, :26, :30 and :34 with
`X-MBX-USED-WEIGHT-1M` of **901, 1140, 1254 and 1453**. This is +552 weight
over 12 seconds in the same venue minute, of which three units were our later
samples. A separate public `exchangeInfo` confirmed 2400 weight per minute,
300 orders per 10 seconds and 1200 orders per minute. Two-hour log counts:
15 worker lines and 38 audit lines matched 429/-1003 (17 quiet-core, 13
quiet-tail, 8 Burst). These are log lines, not counted requests. Existing logs
do not expose complete per-endpoint traffic or successful-response headers.
The public samples measure aggregate IP usage, not attribution, a peak, or
post-fix performance. No signed API probes and no server changes were made.

**Inventory.** Weights were checked against the Binance [USD-M limits](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info),
[trade](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade),
[account](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account),
[market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
and [Spot account](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)
docs. Futures routes below use `/fapi/v1/` except where indicated.

| Callers | Endpoints and weight | Baseline frequency |
| --- | --- | --- |
| Seven account workers: Burst HYPE/INJ/UNI, Quiet core HYPE/ETH/UNI, Quiet Tail HYPE | GET `v3/positionRisk` 5, `order` / `algoOrder` 1 each, `userTrades` 5 per page | Roughly every 2 s during an active session. Lifecycle plus the final sweep could query the same order multiple times. Closed sessions are not continuously polled. |
| Same workers, order mutations | POST `order` / `algoOrder` 0 IP and 1 each in the two account order windows; DELETE 1 IP | On entry, ADD, protection, TP, close or cancel, with same-ID reconciliation after uncertainty. |
| Same workers, admission and maintenance | `v3/account`, `accountConfig`, `symbolConfig` 5 each; `openOrders` / `openAlgoOrders` 1 with symbol, 40 without; `exchangeInfo` 1; `ticker/bookTicker` 2 with symbol, 5 without; `income` 30 per page; `time` 1; Spot `/api/v3/account` 20 in its own window | Startup, fresh entry/ADD or settlement. Time sync ~1/min/worker; scheduled funding ~1/min/active worker, plus admission and close. Spot UID at portfolio startup. Standalone Pre-NY preflight also allowlists `commissionRate` (20); it wasn't an active portfolio poll. |
| Broker audits: Burst, quiet-core and quiet-tail | `userTrades`, `allOrders`, `allAlgoOrders` 5/page/symbol; `income` 30/page/account; account-wide `v3/positionRisk` 5, `openOrders` 40, `openAlgoOrders` 40, `time` 1 | Every 60 s plus capture duration. 161+161+131 = **453 weight per round** before pagination; baseline reread three days on every pass. |
| Binance collector / public repair | `exchangeInfo` and `time` 1 each; `aggTrades` 20/page; `klines` 1/2/5/10 (limit <100/<500/≤1000/>1000) | Startup, warm-up and gaps; WebSocket is ordinary trade delivery. Old client paces HTTP at one call per second; gap repair could add up to about 1200 weight/min on its own. Active market consumers read NATS, which sends no Binance REST. |
| Engine monitor / MVRV | No Binance requests | Monitor reads journals; MVRV uses Coin Metrics and Telegram. |

At 30 cycles/min, each active worker with one position, one STOP query and
one recurring entry-fill query could use ~330 weight/min for those reads alone;
if a second fill call recurs each cycle, ~480. For seven simultaneously active
workers plus audit that is ~2760–3810 weight/min before funding, extra orders,
pagination or a public gap. This is a stress estimate, not a diagnosis that all
seven were active, nor that each worker made this exact mix. Request limits
such as 1000 rows, 48h aggTrade retention, and per-route history time bounds
remain pagination/validation rules. The docs for active routes did not list an
additional per-IP 5-minute cap for these routes; do not apply the cap documented
for other statistical/funding endpoints to this `aggTrades` or `income` route.
Binance documents the `bookTicker` weight header as inaccurate; charge the route,
but ignore its header.

**Local fix.** Shared host SQLite records an atomic reservation before each
HTTP attempt, including retries and pages. Defaults: Futures 1920/min, Spot UID
4800/min, and 240/960 orders per 10s/minute per verified account group, with
a 70% admission ceiling for background/audit/collector and new ENTRY/ADD.
Reconciliation, position checks, native protection and exits may use the last
30% but still honor the hard caps and venue cooldown. Known -1015 scopes to the
account's order submissions; -1003 scopes to the product; unclassified 429
or 418 scopes to the egress IP. Use Retry-After or explicit venue ban expiry
when available; absent information, default to 60s for 429 and up to 3 days for
418. Local denied requests return immediately. Loop checks are staggered and
bounded; HTTP exceptions after transmission remain uncertain and reconcile by
the journaled client ID. No trade rules, fill pricing, or risk limits from the
ADD fix were replaced.

Ordinary terminal fill reads stop only when the scoped journal contains the
full executed quantity in validated native trades. Repeated reads of the same
order revision within one cycle coalesce; any incomplete fill can be fetched
again on the next cycle. Conditional children still reconcile separately, and
close finalization verifies native executed quantities, balanced fills, fees
and funding. The final sweep no longer runs after a failed safety pass.
Transient admission failure retains the original setup; a proven-unsent plan
is discarded to recheck its quote, margin and stop before submission. Expiry
and permanent blocks have separate reasons. Audit polls every 120–130s with a
10-minute overlap and the prior 3-day overlap daily; a gap starts at the last
committed coverage minus overlap. Only a complete multi-page capture advances
coverage. Long gaps can exceed Binance retention and require manual review.
Process heartbeat advances independently; stale capture or active lifecycle
reconciliation still alerts. No market claim is made off local tests.

**Verification.** 172 targeted local tests passed. Both Compose templates rendered
successfully with synthetic environment values. Tests simulate reservations between independent
processes, UTC windows and headers, restart and unavailable/locked storage,
priority, 429/418, ambiguous POST, setup expiry, delayed/partial fills, stop
recovery, closes, audit gap/freshness and all seven Compose bindings. Existing
ADD and peer collateral regressions use the original `b901f9c` behavior.
This Windows checkout independently reproduced **12 failures and 1 pass** in
the complete historical Burst paper package suite at the unchanged baseline;
the same 12 tests fail with v8, due to the untouched v3 release pin. This wider
count does not supersede the earlier six reported from a smaller selection.
See the [deployment runbook](../../live_trading/deploy/README.md) for operation,
recovery, rollout and rollback.

## Next phase: private USD-M state streams (design only)

Run one manager for each verified account UID, using its own credentials.
Normalize and publish account-scoped events on restricted NATS JetStream
subjects. Consumers checkpoint only after updating their journals. Deduplicate
fills by account/symbol/order/trade ID and order and algo updates by native
identity plus venue event time and status; account deltas are idempotent by
transaction/asset/position identity. Retain client ID / algo ID / child order
ID linkage. Enforce a bounded durable backlog and never publish key material,
listen keys, signed requests or unconstrained raw account messages.

Binance documents [USD-M private events](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams):
`ORDER_TRADE_UPDATE` for order lifecycle and fills, `ACCOUNT_UPDATE` for changed
balances/positions, `ALGO_UPDATE` for conditional state and child identity, and
`CONDITIONAL_ORDER_TRIGGER_REJECT` for a rejected trigger. An algo trigger or
finish is not evidence of the child's fill, commission or flat position.
Ordering applies within an event type on one connection; event timestamps do
not prove gapless cross-type coverage. Use socket health, ping/pong, listen-key
renewal and planned reconnect before the venue's 24h disconnect. A quiet account
can be healthy. Publish separate timestamps for transport liveness, checkpoint
and successful reconciliation.

On connection loss, restart, out-of-order conflict or a JetStream sequence
gap, pause ENTRY/ADD. Reopen and buffer events, obtain budgeted REST position,
open standard/algo orders, targeted uncertain-order and trade/funding
snapshots, and replay/deduplicate buffered events before calling state complete.
Keep protection and exit using authoritative reconciliation, and retain REST
for submission, unknown orders, child/fee/funding validation, periodic checks
and recovery. Reduce individual polls only after comparing the private stream
against REST/account journals in shadow mode, including partial stop children
and disconnects. No private stream code is included in this patch.
