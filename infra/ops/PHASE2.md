# Phase 2 — collector separation and shadow validation

Prepared 2026-10-04; handoff 2026-10-05. Implementation is ready for a **separate shadow stack**, not a cutover.
The existing Binance/Lighter paper containers keep running unchanged.

## What is implemented

- `market_data/`: independent venue-native public collectors, compact SQLite history,
  transactional publication outbox, broker publisher and independent observation consumer.
- Existing aggregate-trade normalization/repair is reused. Candle/delta/profile
  aggregation is shared with the old worker, not independently rewritten.
- No collector imports a strategy, indicator experiment, order manager, account ledger
  or backtest. It does not evaluate entry or hold exchange credentials.
- UTC event/availability timestamps, native instrument IDs, persisted local sequence,
  explicit partial/gap status, deterministic event identities and acknowledged delivery.
- A collector checkpoint and its outbox commit together. Broker errors leave the
  outbox intact. A full outbox pauses progression instead of dropping prints.
- Raw prints travel in bounded batches of at most 500. They are removed from the
  collector outbox after broker acknowledgment, not retained as a permanent archive.
- Completed native minutes and requested Pre-NY profile snapshots are durable.
  Empty minutes are not fabricated. Profiles require native 01:00–09:00 NY continuity.
- Consumers own their receipt DB and acknowledge only after commit. Separate durable
  consumers receive independent copies. Retention gaps are recorded explicitly.

The requested profile feature is currently the existing 50-bin/70% Pre-NY definition.
Other windows/instruments require an explicit subscription/configuration extension;
there is no automatic multi-model subscription registry or Spot service yet.

## Retention and provisional resource budgets

| Component | Initial bound |
| --- | --- |
| Collector outbox | 32 MiB per venue; full queue blocks advancement |
| Broker compact events | 72 hours / 256 MiB, whichever binds first |
| Broker ordered print batches | 2 hours / 256 MiB, whichever binds first |
| Collector completed minutes/profiles | 45 days |
| Per-price profile aggregates | 2 days |
| Shadow raw receipts | 2 hours; compact receipts 72 hours |
| Container RAM limits | NATS 192 MiB; each collector/shadow 128 MiB |
| Container CPU limits | 0.25 vCPU each; benchmark before increasing markets |

These are operational starting budgets, not guarantees about VPS capacity or
trading recovery. Byte caps can shorten actual replay duration. Recovery outside
retention requires supported backfill or an explicitly unverified interval.
Do not use this two-hour shadow replay as proof that open positions survive
arbitrary multi-hour outages. Account/execution recovery is Phase 4.

Broker ports are not published to the host. NATS runs on an internal Compose
network; only collectors have an outbound network for public exchange feeds.
This is a single-host shadow deployment, not HA. The broker image digest is pinned;
the collector image ID is captured before future promotion.

## Local evidence

- Scoped regression suite: **122 passed** (existing model/paper/feed plus Phase 2).
- Phase 0 session replay remains **78/78 identical** across both venues.
- Exact candle/delta/15m/profile parity on identical synthetic source events for
  Binance consecutive IDs and Lighter monotonic non-consecutive IDs.
- Restart/duplicate/conflict tests, unrepaired-gap rejection, profile reset and
  atomic rollback when publication capacity is exhausted.
- Bounded repair iteration; no full Binance reconnect history materialization.
- Real local NATS 2.12.12 / nats-py 2.15.0 integration: lost publish acknowledgment,
  deduplication, independent consumers and unacknowledged redelivery passed.
- Real stream configuration bootstrap passed; Compose configuration validates.
- The Phase 1 missing `defaultdict` import in the legacy Binance CLI run branch
  is restored, with a CLI startup regression test. The old server image was not changed.

Actual collector image build, sustained live shadow comparison and VPS resource/
restart gates remain to be performed by the commands below. No strategy cutover
is authorized by passing local tests alone.

## 1. Commit/push locally, without unrelated work

From the repository in PowerShell, review the scoped list first:

```powershell
git status --short
$files = Get-Content infra/ops/phase2_release_files.txt
git add -- $files
git diff --cached --stat
```

Only after reviewing that staged set:

```powershell
git commit -m "Phase 2: independent venue-native collectors and shadow broker"
git push origin deploy/orb-live-agent
```

The existing deployment branch is retained for this migration. Main/release-branch
reconciliation is not forced over unrelated local work. No dataset or capture DB
belongs in this commit.

## 2. Build the new stack on the server

```bash
cd ~/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
phase2=infra/deploy/phase2/compose.yaml
docker compose -f "$phase2" config --quiet
docker compose -f "$phase2" build binance-collector
docker compose -f "$phase2" up -d --no-build nats broker-init
docker compose -f "$phase2" ps -a
docker compose -f "$phase2" logs --tail=50 broker-init
```

Expect `broker-init` to exit **0** after configuration verification. It is a one-off
bootstrap service; an exited-successful status is normal. Do not start collectors
if bootstrap failed.

Test broker delivery without real-data subjects/orders:

```bash
docker compose -f "$phase2" run --rm --no-deps \
  --entrypoint python binance-collector -m market_data.selftest
```

This creates and cleans up only a uniquely named temporary test stream.

## 3. Warmup once and start shadow collection

Binance warmup is native minute candles, not reconstructed raw volume-at-price:

```bash
docker compose -f "$phase2" run --rm --no-deps binance-collector \
  --config /app/config/binance.json --database /data/binance.sqlite --action warmup
docker compose -f "$phase2" up -d --no-build binance-collector lighter-collector shadow-receipts
docker compose -f "$phase2" ps -a
docker compose -f "$phase2" logs --tail=100 binance-collector lighter-collector shadow-receipts
docker stats --no-stream
```

Do not rerun warmup on an already active collector database. Lighter has no approved
equivalent historical delta warmup and must collect its own native history. Both
need collection to cover a complete requested Pre-NY window. Warmup candles do
not replace the profile's volume-at-price inputs.

The new Compose project is `golden-goose-phase2-shadow`, with new named volumes.
It does not mount the old paper ledgers. Do not run `down -v` or stop/rebuild the
old model Compose services. Extra exchange subscriptions are temporary shadow
overlap; single-upstream sharing happens after the migration gates pass.

## Idle pull timeout repair (2026-10-05)

The first VPS shadow run exposed a consumer-only regression: an empty
`fetch(100, timeout=1)` can raise builtin/asyncio `TimeoutError`, while the old
handler caught only its NATS subclass. An idle stream therefore restarted the
receipt consumer. Collector health and bootstrap success do not test this path.

`fetch_batch` now treats both timeout variants as an empty batch. Other failures
and cancellation still propagate; receipt commit-before-ack is unchanged.
Local evidence: 18 Phase 2 tests passed, including both timeout types, resumption,
non-timeout errors and cancellation. Real NATS 2.12.12/nats-py 2.15.0 scratch test
also passed idle timeout followed by successful reception, alongside ACK-loss,
deduplication, independent consumers and redelivery. VPS acceptance remains pending.

After committing/pushing the four repair files, run on the VPS:

```bash
cd ~/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
phase2=infra/deploy/phase2/compose.yaml
docker compose -f "$phase2" build shadow-receipts
docker compose -f "$phase2" run --rm --no-deps \
  --entrypoint python binance-collector -m market_data.selftest
```

Only if the scratch test reports `status: pass`, `idle_timeout: true` and
`resumes_after_idle: true`, recreate the receipt consumer:

```bash
docker compose -f "$phase2" up -d --no-deps --no-build shadow-receipts
docker compose -f "$phase2" ps -a
docker compose -f "$phase2" logs --since 5m --tail=100 shadow-receipts
```

The consumer prints its receipt/checkpoint status every 60 seconds. Verify it
remains up, records incoming receipts and no longer emits timeout tracebacks.
Do not rerun warmup, delete any volume/database, or recreate collectors/old paper
services for this repair. Rebuilding the shared image does not alter running
collector containers. This patch does not change trading or evaluation rules.

## 4. Capture and acceptance gate

After at least one complete native Pre-NY window and preferably 24–48 hours:

```bash
bash infra/ops/capture_phase2.sh
```

Copy the printed archive off the VPS and provide it along with a fresh Phase 0
capture of the old services covering the same session. The capture includes
collector DB backups/status, shadow cursors, logs, resource snapshot and image IDs.
It is a consistent SQLite backup, not a raw copy of an active DB.

Before cutover require:

Capture repair (2026-10-06): `docker compose images` can fail when an old
collector container still runs but its image metadata is no longer in the image
store. Capture now records container-owned image IDs directly and marks missing
repository digests as unavailable in `images/` and `warnings.txt`. It does not
substitute the current mutable tag or claim that the old image is reproducible.
Other inspection failures still abort capture. No collector restart, rebuild or
warmup is needed to capture evidence. Preserve these warnings for release review;
future promotion must use a verified, retained immutable image.

1. Shared complete minutes, delta and profiles match; missing/invalid coverage is
   distinguished from valid zero-volume silence. Compare exports with
   `python -m infra.ops.compare_phase2_history --legacy OLD --collector NEW`.
2. Publisher backlog drains; no unexplained duplicate/conflicting data or sequence gaps.
3. Controlled collector restart repairs from native source and does not duplicate
   aggregates. Restart only the NEW shadow collectors when ready to test this.
4. Broker outage/restart leaves pending publications recoverable; verify backlog drains
   and consumers resume. An unrecoverable interval must be labeled, not concealed.
5. Resource measurements under busy market: no OOM, no sustained CPU saturation,
   disk growth consistent with retention and sufficient VPS headroom. One `docker
   stats` sample is not a sustained load test.

**Phase 2 is not fully accepted until server evidence passes these gates.** Phase 3
connects strategy consumers; Phase 4 moves account/position ownership. No entry,
stop, sizing, calibration, fill or trading-performance claim changes here.

## References

- [NATS stream persistence](https://docs.nats.io/learn/jetstream/your-first-stream)
- [NATS Python client API](https://nats-io.github.io/nats.py/modules.html)
- [NATS 2.12.12 release](https://github.com/nats-io/nats-server/releases/tag/v2.12.12)
