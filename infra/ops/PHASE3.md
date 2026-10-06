# Phase 3 — independent strategy shadows

2026-10-06. Implemented and locally tested; **server acceptance pending**.
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

## Rollback of new strategies only

```bash
docker compose -f "$phase2" -f "$phase3" stop binance-strategy-shadow lighter-strategy-shadow
```

Keep their volumes/durables for diagnosis. Do not restore an old account backup.
An upgraded commit/config must not reuse this identity-locked strategy DB without
a reviewed migration or separate release state. Phase 6 will formalize promotion,
alerts, off-host backups and immutable digest manifests.
