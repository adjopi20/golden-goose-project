# Phase 4 Lighter filtered-stream retention recovery

On 2026-10-10 13:45 UTC, the Lighter account stopped when the first Binance
intention expired from shared `GG_INTENT_V1`. The Lighter intention durable had
delivered zero messages. Its account code compared a venue-filtered receipt
checkpoint (zero) with the unfiltered stream's first sequence (two). The same
false alarm could occur on shared market and execution streams.

The repair records a stream-wide `verified_through` watermark only after the
venue-filtered durable reports zero pending and zero unacknowledged messages.
The stream tail is captured **before** that consumer check; later arrivals
cannot be mistaken for already checked data. Every committed venue receipt
also advances this watermark. If retention overtakes it, the account still
stops. No old checkpoint or venue coverage is silently marked complete.

The old Lighter account database and durable are preserved. Recovery uses a
new `lighter_preny_phase4_v2` identity and named volume, leaving Binance and
the legacy paper services on their running releases. Missed shadow sessions
remain missed. This is a fresh validation ledger, not a historical repair.

## Before the server change

1. Commit/push the five scoped repair files, this note and the guarded recovery
   script on
   `deploy/orb-live-agent`. Run the focused account/broker tests. Set
   `GG_PHASE4_COMMIT` to the exact pushed commit on the server.
2. Check `docker compose` for the old account and strategy. Confirm the
   old Lighter account is flat with zero requests, and the Lighter strategy
   has zero proposals and zero outbox rows. If any count is nonzero, stop;
   recovery needs an individual reconciliation.
3. Stop only `lighter-strategy-paper-shadow` and
   `lighter-account-shadow`, then repeat the counts. Keep all other services
   running. Back up the old SQLite database with SQLite's online backup API,
   keeping its old named volume and account durable intact.

## Change and checks

Preferred: from the server repository after `git pull --ff-only`, run the
guarded procedure. It verifies the empty account/strategy, stops only the two
Lighter Phase 4 services, checks again, saves consistent SQLite backups,
builds the repaired account image, and starts the existing strategy image.

```bash
bash infra/ops/recover_phase4_lighter.sh
```

The equivalent manual Compose commands are:

```bash
export GG_PHASE4_COMMIT=$(git rev-parse HEAD)
phase2=infra/deploy/phase2/compose.yaml
phase4=models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml
repair=models/trend_following_preny_profile_15m/deploy/phase4.retention-recovery.yaml

docker compose -f "$phase2" -f "$phase4" -f "$repair" config --quiet
docker compose -f "$phase2" -f "$phase4" -f "$repair" build lighter-account-shadow
docker compose -f "$phase2" -f "$phase4" -f "$repair" up -d --no-deps --no-build lighter-account-shadow
docker compose -f "$phase2" -f "$phase4" -f "$repair" ps -a
```

After the new account is healthy, start the **existing** strategy container:

```bash
docker compose -f "$phase2" -f "$phase4" -f "$repair" start lighter-strategy-paper-shadow
docker compose -f "$phase2" -f "$phase4" -f "$repair" exec -T lighter-account-shadow \
  python -m live_engine.account_worker --database /data/account.sqlite --status
```

Check the account metadata has `account_group=lighter_preny_phase4_v2`,
`verified_through:*` advances on idle streams, and accounting checks pass.
Confirm the old strategy image was *started*, not rebuilt or recreated: its
existing strategy DB has a release identity. Do not run broad `up` or `down`
commands against this Compose project. Do not delete the old account volume or
durable; they are evidence of the outage.

Lighter's 2026-10-11 `/orderBooks` 502/503 outage happened after this account
had stopped. Those HTTP errors merit separate collector retry/backoff work;
they did not cause the filtered-stream false alarm.
