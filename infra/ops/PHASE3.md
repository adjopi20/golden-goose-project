# Phase 3 — independent strategy shadows

2026-10-06. **Bounded server session/recovery acceptance passed** (details below).
This is not an account-service cutover, profitability claim, or live activation.

## Scope

- Two independent model/venue subscribers: Binance and Lighter.
- Each consumes compact `GG_MARKET_V1` events with its own JetStream durable,
  history, evaluations, receipts, and SQLite cursor. No raw trade reaggregation.
- Reuses the extracted frozen `runtime/observer.py` and evaluator. The legacy
  paper Worker delegates to this same orchestration when rebuilt later.
- One consistent read-only collector snapshot supplies warmup. The global
  broker anchor is captured **before** that snapshot, then events already in
  its venue source-sequence anchor are skipped. No collector/account DB writes.
- Today's past boundaries rebuild the first-candidate session lock without
  creating retroactive entry proposals. New valid intentions are stored locally
  only. There is no account manager, exchange adapter or intention publisher.
- Late candidates use the same five-minute / 12:00 deadline guard. Replaying
  input is not permission to place an expired entry.
- Coverage resets block the affected instrument until full native profile
  continuity is available. Other markets retain their independent decisions.
- A retention gap, conflicting event, collector epoch reset, DB identity mismatch or DB rewind behind
  broker acknowledgments fails closed and requires review. Do not delete the DB
  or durable to suppress an error.

## Small transport amendment

The collector now emits `evaluation_boundary` after preceding minute/profile
events, exactly when a native print confirms passage of a quarter boundary.
This is necessary for sparse minutes: a 09:13 bar followed by a 09:16 trade must
still deliver the 09:15 evaluation. It does not fabricate a 09:14 price, add a
wall-clock price, or change entry parameters. The 09:00 profile precedes its
boundary. Update the receipt observer together with collectors because its old
event validator does not know this additional event type.

## Local evidence

- 52 focused regression tests passed: Phase 1 extraction, Phase 2 data/outbox,
  old paper behavior, strategy transactions, bootstrap, native sparse boundary,
  proposal expiry, durable restart, commit-before-ack and dependency separation.
- Captured 2026-10-05 session: **39/39 Binance and 39/39 Lighter causal decisions
  match exactly** on identical captured inputs. Includes the ETH short signal,
  its subsequent session lock, and Lighter's existing NOT_READY states.
- Replay excludes execution-only fields; it does not simulate fills or claim
  that different warmup histories are numerically identical. The consumer's
  collector warmup differs in provenance/start from the older paper worker.
  Any live decision difference must be traced to its inputs, not hidden with a
  tolerance or changed rule.
- Real broker delivery, image startup, resource use and restart on the VPS
  remain deployment acceptance gates. Unit stubs are not a broker outage drill.

## Commit/push from the laptop

Stage only these Phase 3 files, preserving unrelated working-tree changes:

```powershell
git add -- trading_core/contracts.py market_data/collector.py market_data/store.py live_engine/strategy_store.py models/trend_following_preny_profile_15m/paper_worker.py models/trend_following_preny_profile_15m/runtime/observer.py models/trend_following_preny_profile_15m/shadow_consumer.py models/trend_following_preny_profile_15m/shadow_parity.py models/trend_following_preny_profile_15m/deploy/Dockerfile.shadow models/trend_following_preny_profile_15m/deploy/requirements-shadow.txt models/trend_following_preny_profile_15m/deploy/phase3.shadow.yaml tests/test_strategy_phase3.py tests/fixtures/phase3_legacy_worker_v1.py infra/ops/PHASE3.md
git diff --cached --stat
git commit -m "Add independent pre-NY strategy shadows for Phase 3"
git push origin deploy/orb-live-agent
```

## Server preparation — keep legacy paper services running

Build/start only the shadow project. Do **not** use the older model paper
Compose file, run `down`, remove volumes, prune images, or stop active positions.

```bash
cd ~/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_PHASE3_COMMIT=$(git rev-parse HEAD)
phase2=infra/deploy/phase2/compose.yaml
phase3=models/trend_following_preny_profile_15m/deploy/phase3.shadow.yaml

docker compose -f "$phase2" -f "$phase3" config --quiet
docker compose -f "$phase2" build binance-collector
docker compose -f "$phase2" -f "$phase3" build binance-strategy-shadow

# Both collector services and receipt observer share the rebuilt data image.
docker compose -f "$phase2" up -d --no-deps --no-build binance-collector lighter-collector shadow-receipts
docker compose -f "$phase2" ps -a
```

Wait until both collectors are healthy and receipt counts increase without
validation errors. Do not warmup again: their existing history is retained.
Then start the independent strategies:

```bash
docker compose -f "$phase2" -f "$phase3" up -d --no-deps --no-build binance-strategy-shadow lighter-strategy-shadow
docker compose -f "$phase2" -f "$phase3" ps -a
docker compose -f "$phase2" -f "$phase3" logs --since 10m --tail=100 binance-strategy-shadow lighter-strategy-shadow
docker stats --no-stream
```

Initial bootstrap may take several minutes; this is not a daily warmup.
Health means broker polling is current, **not** that all assets are ready to
trade. Lighter native history and HYPE calibration requirements are unchanged.
No Lighter C1 artifact is invented or borrowed from Binance.

## Status / evidence to return

```bash
docker compose -f "$phase2" -f "$phase3" exec binance-strategy-shadow python -m models.trend_following_preny_profile_15m.shadow_consumer --database /data/strategy.sqlite --status
docker compose -f "$phase2" -f "$phase3" exec lighter-strategy-shadow python -m models.trend_following_preny_profile_15m.shadow_consumer --database /data/strategy.sqlite --status
```

Send those outputs plus `ps`, logs and `docker stats`. Observe one complete
09:00–12:00 NY session alongside the unchanged old paper worker, then audit
decision/feature parity and restart one strategy at a time. The other venue and
collectors must continue advancing. Promote nothing before this gate passes.

This phase stores proposed intentions, not positions or equity. Existing paper
services remain the authoritative paper ledgers until Phase 4 is accepted.
Onchain deployment is not silently included in this change.

## 2026-10-06 acceptance follow-up

The completed live session matched 78/78 decision states and all six profiles.
75/78 numeric snapshots matched exactly; the other three were traced to different
legacy warmup histories. Identical-input replay remained exact. Do not splice
legacy paper history into the collector, loosen tolerances, or tune the evaluator
to hide this provenance difference. Lighter native-history/calibration readiness
requirements remain unchanged.

The collector is the authoritative warmup source for each independent strategy.
Use `infra/ops/phase3_continuity.py` with read-only strategy/collector snapshots to
verify the full retained history, epoch and minute payloads. It rejects missing
collector history in the strategy, but permits older strategy rows outside the
collector's retention. No repair or synthetic minutes are performed by this tool.

The Compose healthcheck now uses only Python's standard library and a read-only
SQLite heartbeat lookup. It keeps the 180-second heartbeat freshness guard and
rejects future timestamps; it does not import pandas/numpy/evaluator under the
0.25-CPU strategy limit. This is deployment configuration only: the running image,
trading formulas, DB identity and durable remain unchanged. No image rebuild or
DB identity migration is needed for this healthcheck update.

Apply configuration only to one strategy service at a time with `up -d --no-deps
--no-build --force-recreate <strategy-service>`, using the exact existing
`GG_PHASE3_COMMIT`, then check heartbeat/cursor progress, unchanged decision and
proposal hashes, and the other services' unchanged container IDs. Never recreate
collectors, NATS, or legacy paper as part of this recovery drill. Server acceptance
still depends on the actual continuity and restart results, not this document.

Acceptance evidence on 2026-10-06:

- 52 installed focused tests passed with the existing local NATS test dependency.
- Full retained collector/shadow history initially matched on 124,527 minute
  records, with matching epoch and zero missing/conflicting rows. The audit was
  repeated after recreation to include subsequently received minutes.
- Binance and Lighter strategies were recreated separately with unchanged image,
  persistent DB and durable. Both resumed cursor progress and remained healthy.
- Each venue retained the exact SHA256 of its 39 evaluations. Binance retained
  exactly one proposal; Lighter retained zero. No duplicate proposal appeared.
- NATS showed zero pending messages, pending ACKs and redelivered messages for
  both Phase 3 consumers after recovery.
- Legacy paper, both collectors, NATS and receipt observer retained their original
  container IDs/start times. No account ledger was migrated or execution enabled.
- The lightweight healthcheck completed in roughly 0.6–1.9 seconds in observed
  successful checks, instead of the previous roughly 10-second evaluator import.

This accepts the tested session and short orderly restart paths, not arbitrary
outages or all future decisions. Legacy warmup provenance differences remain
documented; Lighter calibration/history readiness is still enforced. Phase 4
account/position ownership is the next separate implementation milestone.

## Rollback of new strategies only

```bash
docker compose -f "$phase2" -f "$phase3" stop binance-strategy-shadow lighter-strategy-shadow
```

Keep their volumes/durables for diagnosis. Do not restore an old account backup.
An upgraded commit/config must not reuse this identity-locked strategy DB without
a reviewed migration or separate release state. Phase 6 will formalize promotion,
alerts, off-host backups and immutable digest manifests.
