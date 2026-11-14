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
