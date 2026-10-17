# Phase 4 account delivery: single-inflight prevention

Status: prevention and guarded upgrade tested locally; NOT deployed. Use the
delivery-upgrade procedure below, not the older health-upgrade script. Never
recreate all services to deploy this patch blindly.

## Confirmed failure

The durable allowed 100 unacknowledged messages. After an unfinished batch and
restart, later messages could be committed before earlier messages were
redelivered. The account's maximum-sequence checkpoint then rejected an unseen
older event. This mechanism was reproduced with local NATS 2.12.12 and nats-py
2.15.0 using the unchanged AccountStore. It does not prove the first historical
interruption's cause.

## Minimal prevention

- New account durables use explicit acknowledgment and `max_ack_pending=1`.
- Account workers request one message per fetch, not 100.
- Existing durables with unsafe configuration fail before subscription. No
  in-place consumer rewrite or silent reset is performed on startup.
- Commit-before-ack, payload-conflict detection, source sequence/epoch checks,
  retention checks and account identity locks remain unchanged.
- Market-data collectors, generic receipt consumers and trading rules do not
  change; the generic fetch helper keeps its default batch size of 100.

The limit is per account stream (intention, market, execution), not one raw
exchange trade: execution messages already contain native trade batches.
Production throughput on the VPS is not yet measured.

## Verification

117 tests passed on Windows, covering account accounting, upgrade refusal and
state preservation, broker mocks, prior health upgrade, strategy, collector,
paper bridge, and three integration tests against a disposable local broker:

1. Disconnect before committing the delivered message.
2. Commit the message, disconnect before acknowledgment.
3. Pause real consumer delivery, preserve an open position while upgrading all
   three existing durables from 100 to 1, then resume and process the next event.

In both cases, subsequent delivery remained blocked until redelivery; final
fills, trade P&L and cash matched an uninterrupted control, with exactly five
receipts and no reservation left after the stop exit.

Run the integration tests ONLY with a disposable local broker on port 15222:

```powershell
$env:GG_LOCAL_NATS_TEST="1"
python -B -m pytest tests/test_account_restart_nats.py -q
```

## Guarded server procedure — not yet executed on a VPS

After pushing the reviewed commit, run from `/root/golden-goose-project` on
each server. Existing worker releases must be a reviewed `15983d7`, `348b629`, or `a23fd74` release;
unknown identities are refused. Do not run the next command if one fails.

```bash
cd /root/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
```

NEW server (`139.190.102.183`): preserve both ledgers if their audits pass:

```bash
bash infra/ops/upgrade_phase4_delivery.sh binance
bash infra/ops/upgrade_phase4_delivery.sh lighter
```

OLD server (`157.230.244.232`): new validation epoch only for the affected,
flat Binance account; preserve the healthy Lighter ledger:

```bash
bash infra/ops/upgrade_phase4_delivery.sh binance --fresh-epoch
bash infra/ops/upgrade_phase4_delivery.sh lighter
```

The script builds before stopping anything. For a healthy account it pauses
new broker deliveries for up to ten minutes, lets the running worker ACK its
already fetched batch (20-second limit), then stops only that venue's Phase 4
strategy/account. It saves consistent account and strategy backups, checks the
DB and consumer again, changes only the in-flight limit and reviewed code
identity, resumes delivery, waits for account health, and starts the unchanged
strategy image. Existing positions and equity remain in place.

The helper holds the account writer lock during the upgrade. A partial broker
failure leaves the ledger code identity unchanged; workers stay stopped for
review. Re-running after correcting the cause can finish a partially changed
consumer configuration. No message is acknowledged or deleted by the helper.

Fresh-epoch mode refuses an open/pending position, reservation, healthy old
ledger, unpublished strategy intention, or pre-existing unused recovery
volume. It preserves the old DB/durable and copies both old DBs to
`/root/golden-goose-phase4-backups/<UTC>-delivery-binance/`. New equity is 1000
per allocation. Subsequent valid signals only; no retrospective missed fills.
The recovered volume/config persists in `phase4.single-inflight-recovery.yaml`;
include that overlay for future manual Compose operations on the OLD server.
The upgrade script detects the new group on repeat runs and keeps that volume.

Outbox rows with `published=1` are retained audit history, not pending work.
The 2026-10-17 preflight fix counts only `published=0`; an old-server read-only
inspection found 20 published rows and zero unpublished rows. No outbox rows
were cleared to bypass this check.

Legacy paper workers, collectors, NATS service, shadow receipts, other venue,
and MVRV services are not restarted. Docker still reports containers in one
Compose project as orphans when omitted: never use `--remove-orphans` here.

Do not resume blindly after a refusal. Capture the error and inspect the
selected account. The two servers are updated independently, never by resetting
both histories. Copy backups off the VPS after successful verification.

## 2026-10-17 server-2 update-response discrepancy

The `348b629` procedure stopped on its strict comparison of the immediate
consumer-update response. Read-only inspection found the Binance worker stopped,
its source ledger unchanged, no in-flight messages, and a preserved open HYPE
paper position. The first consumer had already persisted the intended limit.

A separately approved single-step market-consumer update captured both sides:
the request targeted limit 1, but the update response reported limit 100, omitted
`pause_until`, and changed `_nats.req.level` from 1 to 0. A separate consumer-info
read then showed the persisted limit was 1, with the original pause/metadata.
All ledger checkpoints remained unchanged. Do not treat that response object as
the final persisted consumer policy.

The corrected helper snapshots the desired policy before sending the update,
then reads consumer-info and compares ALL fields with that snapshot. It does
not ignore metadata or policy differences. A remaining difference names the
channel and fields and still stops the upgrade. Tests cover a stale update
response with correct persisted state, and a claimed-success response whose
persisted state remains wrong. The latter must fail without changing the ledger
release or resuming delivery.

This fixes a guard false alarm; it is separate from the original unfinished-
batch restart bug. Re-run the normal procedure to finish partial consumer
updates. Do not manually change the DB code identity or start old workers.

## Existing consumer migration gates

For a healthy ledger, pause its strategy/account in a reviewed procedure, take
a consistent backup, and require zero pending acknowledgments on all three
durables before changing only `max_ack_pending` to 1. Retain each durable's
identity, filters, start sequence and other policy fields. Check broker ack
floors against the DB, preserve all ledger state, then upgrade the exact code
identity and restart. Test this procedure before production. If any durable has
outstanding acknowledgments or the ledger has holes, refuse the healthy path.

An open position does not itself justify resetting anything. Its stop/deadline,
reservation and pending triggers must survive the coordinated upgrade.

## Old-server Binance is a separate recovery decision

The inspected old ledger has missing receipts behind its checkpoint and input
blocks. Merely changing the durable limit cannot repair it. Do not rewind only
the checkpoint, invent receipts, apply old ticks to advanced positions, clear
uncertainty flags, or delete the old database/durable.

The earliest inspected execution-uncertain record dates to 2026-10-16 06:46 UTC.
The specifically reproduced broker hole is around 14:51 UTC. Later inspection
found GG_EXEC_V1 retained only events from about 14:57 UTC onward. The collector
has minute/profile tables and an empty publication outbox, not a retained raw
execution archive. Retained local data alone is therefore insufficient to claim
an exact reconstruction of all affected executions. External raw-data backfill
and review of causal intentions/snapshots would be a distinct forensic task.

Recommended operational recovery, subject to user approval: archive the old
Phase 4 validation ledger intact and start a clearly identified fresh validation
epoch, never splice its equity into the old ledger. Keep the unaffected legacy
paper services, collectors, strategies used for observation, Lighter and new
server separate. Decide the epoch's initial equity explicitly before creating
its database. Do not silently reinterpret a fresh epoch as recovered history.
