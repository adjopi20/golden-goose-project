# Receipt health and account timeout repair — 16 October 2026

## Evidence from the VPS

- Every active container was running. Exchange feeds and active consumer checkpoints advanced.
- Both Phase 4 account workers had restarted on `nats.errors.TimeoutError` from
  `js.stream_info`. This request goes to the internal NATS broker, not an exchange.
- The installed NATS client uses a 5-second JetStream API timeout.
- Account receipt pruning runs synchronously in the same event loop every minute.
  Both databases lacked an index on `received_ms`; SQLite reported `SCAN receipts`.
- Binance had 441,472 receipt rows (about 125 MiB database). A read-only scan took
  6.53 seconds on the VPS. Lighter had 270,173 rows (about 75 MiB), with a 1.08-second scan.
- Account containers have a 96 MiB RAM limit and 0.25 CPU quota. Memory reclamation
  and CPU throttling were observable. No OOM kills were recorded.
- Active NATS consumers were caught up, with no pending ACKs, slow consumers or
  stalled clients at the diagnostic snapshot. Broker storage was about 50 MiB.

These observations confirm a preventable blocking query and an unhandled internal
API timeout. They do not prove the exact trigger of every historical timeout.
Independent Lighter WebSocket disconnects also occurred; those are separate feed
events and still require normal reconnect/coverage handling.

## Changes

1. `shadow-receipts` now records successful poll heartbeats for both streams and
   a maintenance heartbeat. A read-only healthcheck requires all three to be fresh
   within 180 seconds. An idle exchange can remain healthy; missing new trades
   alone is not treated as a dead consumer.
2. Account receipts have a `received_ms` index, so retention cleanup finds expired
   rows without scanning all retained receipts.
3. A transient account pump timeout logs `BROKER_TIMEOUT_RETRY` and retries after
   one second. It does not advance the health heartbeat on failure. Retention,
   conflicting-event, identity and accounting errors still propagate.
4. The reviewed account upgrade makes consistent SQLite backups and changes only
   the release metadata and index. All positions, trades, balances, cursors,
   durable registrations and allocation rules are preserved. The corrected
   preflight allows consistent open positions/pending entries and historical
   uncertain closes. It rejects disagreement between active state, requests and
   reservations, and unknown source releases. A fingerprint verifies that trading
   state and coverage/audit metadata are unchanged by the upgrade.

No CPU/RAM limits, model rules, sizing tiers, exit logic or exchange adapters change.

Local validation: 57 scoped regression tests passed; the final two migration tests
also passed after adding coverage for an existing database without the index.
Compose configuration, Bash syntax and Git whitespace checks passed. Actual image
build and sustained VPS acceptance are performed by the deployment commands below.

## Install receipt health now

Run on the VPS:

```bash
cd ~/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
phase2=infra/deploy/phase2/compose.yaml
docker compose -f "$phase2" build shadow-receipts
docker compose -f "$phase2" up -d --no-deps --no-build shadow-receipts
docker compose -f "$phase2" ps shadow-receipts
docker compose -f "$phase2" logs --tail=5 shadow-receipts
```

Allow the first maintenance cycle (about one minute) and healthcheck to run.
Collectors, NATS, strategy workers and legacy paper accounts need no restart.

## Install the compatible account repair

```bash
cd ~/golden-goose-project
bash infra/ops/upgrade_phase4_account_health.sh
```

Open positions can resume through this schema/performance-only update. The script builds first, stops only
the two Phase 4 strategy/account pairs, checks again, backs up and upgrades the
existing account databases, starts the accounts, and resumes the existing strategy
containers after account health passes. Tests compare pending-entry, open-position,
partial-exit and historical-uncertain states with an uninterrupted control, including
subsequent fills. It keeps the Lighter recovered v2 volume.
It never creates fresh equity or removes a ledger/durable.

The first release (59f0019) required flat accounts and also counted
`closed_uncertain` as pending. That guard was too restrictive: historical
uncertainty must remain recorded, but does not prevent adding an index or a
compatible runtime restart. The correction never clears an execution-gap block.
Such blocks still require a separate feed/recovery review before new entries.

Corrected upgrade validation: 42 scoped account/broker/upgrade tests passed.
Pending, open, partial and closed-uncertain states survive upgrade, and subsequent
fills/journals match uninterrupted control runs. The error path now prints a
concise refusal rather than a Python traceback.

On failure after services stop, inspect the printed error and backup directory
before resuming. Do not run the old fresh-ledger recovery script.

## Acceptance still needed after deployment

Check that receipt health becomes healthy, account accounting checks pass, the
same trade counts/equity remain, cursors advance, and restart counts stay unchanged
over normal sessions. A persistent broker outage may still make health unhealthy;
the retry does not manufacture execution or claim missing data was recovered.
