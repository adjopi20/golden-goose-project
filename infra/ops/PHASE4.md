# Phase 4 — Independent paper account owners

Status (2026-10-07): implemented; local tests and Compose validation pass.
Server acceptance and migration are **not** complete. No real orders or cutover.

Installed targeted suite: 86 tests passed. Docker image builds and live Phase 4
behavior are still unverified until the server gates below are completed.

## Boundaries

- Existing legacy paper workers and Phase 3 strategies remain untouched and authoritative.
- Two new strategy subscribers use the frozen model/evaluator with fresh strategy databases.
- They publish only newly eligible intentions via transactional outbox to `GG_INTENT_V1`.
- Two account owners consume intentions, coverage and native trades. They alone write their
  own reservations, positions, fills, fees, equity and accounting journals.
- Execution is the existing next-public-print simulation, extracted verbatim into
  `trading_core/paper_execution.py`. `backtest_engine.replay_aggtrades` re-exports its API.
  This is not a new backtester or an exchange order adapter.
- Account images have no model, pandas, PyArrow, exchange adapter or credentials.
- Each venue has three independent virtual allocations of 1,000. These new ledgers are
  validation accounts, **not** a transfer/reset of the authoritative paper equity.

Frozen entries, risk tiers, initial POC stop, TP fractions and time exit are unchanged:

| Allocation | Risk tiers | Partial | Remainder |
| --- | --- | --- | --- |
| ETH | 0.25% / 0.5%, frozen EMA policy | 10% at 1R | Initial stop or next-day 08:59 NY |
| BNB | 0.25% / 0.5%, frozen ADX policy | 10% at 2R | Initial stop or next-day 08:59 NY |
| HYPE | 0.5% | 10% at 1R | Initial stop or next-day 08:59 NY |

Sizing uses closed-trade equity and actual fill-to-stop distance, capped by the existing
5x notional limit. Price risk excludes fees/slippage/funding, as agreed. Cash recognizes
fees and partial realized P&L immediately; this does not change closed-equity sizing.
Every cash/fee/P&L posting has a balanced counter-posting. Margin reservations are
distinct from cash transfers and risk amounts.

Lighter retains its native readiness/calibration requirements. `USD-paper` is an explicit
accounting unit and 4 bps is the existing research proxy, not a claim about actual fees
or collateral. No venue's feed or signal is substituted for another.

## Safety and recovery

- New account subscriptions start at the stream tail before new strategy publishers start.
  Bootstrap reconstructs decisions but never publishes retrospective entries.
- Account receipt latency can delay or reject a signal; its original expiry is never extended.
- Intention, reservation, fill, posting and consumer cursor commit before acknowledgment.
- Repeated IDs are idempotent; conflicting bodies fail closed. One active owner per native
  instrument within the account group, with one writer lock per database.
- Stream retention loss, database/durable mismatch, source epoch change, or changed release
  identity require reviewed recovery. Do not delete databases/durables to bypass them.
- Native Binance ID gaps or explicit coverage gaps block new entries in the affected market.
  Existing protection continues on observed prints; affected closes are marked uncertain.
  A delayed cross-stream gap notice also marks already-closed trades overlapping the
  reported interval uncertain. Accounting checks passing do not certify execution coverage.
  The block is not automatically cleared without recovery review.
- A one-second wall-clock scheduler persists overdue exit duty even with no trades. It does
  not invent an executable price. The next observed native print closes the position;
  deadline/fill delay is recorded. A server outage can still delay exits.
- Only receipt IDs/hashes/checkpoints are retained in the account service, not a raw tick
  warehouse. Input receipt metadata is pruned after 72 hours; trading journals remain.
- Health checks are standard-library read-only SQLite polls, not evaluator imports.
- This phase does not add BBO/depth, funding, exchange rounding, liquidation, real stops,
  private account reconciliation, or live orders. Those remain later acceptance work.

## Local checks

Run from the repository with the existing development environment. `nats-py==2.15.0`
must be available (Docker images install it; local tests may use the bundled test copy).

```powershell
.\.venv\Scripts\python.exe -B -m pytest `
  tests/test_account_phase4.py tests/test_phase4_broker.py `
  tests/test_live_engine_paper.py tests/test_strategy_phase3.py -q
```

The AST parity test locks the exact extracted execution definitions. Tests cover
transaction rollback, redelivery, restart, partial/stop/time exits, accounting balance,
isolated sizing/capital, gap/expiry handling, publication opt-in and ACK-after-commit.

## Commit/push — development PC

This scopes the commit to Phase 4 and preserves unrelated staged/working changes.
Review the diff before committing; do not use `git add .`.

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
if ((git branch --show-current) -ne 'deploy/orb-live-agent') { throw 'Wrong release branch' }
$phase4Files = @(
  'backtest_engine/replay_aggtrades.py',
  'docs/architecture/TRADING_PLATFORM_V1.md',
  'live_engine/order_manager.py',
  'live_engine/strategy_store.py',
  'live_engine/account_store.py',
  'live_engine/account_owner.py',
  'live_engine/account_worker.py',
  'live_engine/service_health.py',
  'live_engine/Dockerfile.account',
  'trading_core/paper_execution.py',
  'models/trend_following_preny_profile_15m/shadow_consumer.py',
  'models/trend_following_preny_profile_15m/runtime/intention.py',
  'models/trend_following_preny_profile_15m/deploy/Dockerfile.shadow',
  'models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml',
  'models/trend_following_preny_profile_15m/deploy/phase4/binance.account.json',
  'models/trend_following_preny_profile_15m/deploy/phase4/lighter.account.json',
  'infra/ops/PHASE4.md',
  'tests/test_account_phase4.py',
  'tests/test_phase4_broker.py'
)
git add -- $phase4Files
git diff --cached --stat -- $phase4Files
git commit --only -m 'Add independent paper account shadows for Phase 4' -- $phase4Files
if ($LASTEXITCODE -ne 0) { throw 'Commit failed; do not continue to deployment' }
git push origin deploy/orb-live-agent
```

## Server startup — additive validation only

First commit/push the scoped Phase 4 files from the development machine. On the VPS:

```bash
cd ~/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_PHASE4_COMMIT=$(git rev-parse HEAD)
phase2=infra/deploy/phase2/compose.yaml
phase4=models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml

# No dirty code/config used by these images; unrelated research changes are excluded.
test -z "$(git status --porcelain -- trading_core live_engine market_data \
  models/trend_following_preny_profile_15m/runtime \
  models/trend_following_preny_profile_15m/shadow_consumer.py \
  models/trend_following_preny_profile_15m/deploy)" || exit 1

docker compose -f "$phase2" -f "$phase4" config --quiet
docker compose -f "$phase2" -f "$phase4" build \
  binance-account-shadow binance-strategy-paper-shadow
docker compose -f "$phase2" -f "$phase4" run --rm --no-deps account-stream-init
docker compose -f "$phase2" -f "$phase4" up -d --no-deps --no-build \
  binance-account-shadow lighter-account-shadow
```

Check account services become healthy (usually within a few minutes); **do not** start
the new strategy publishers until both account owners are healthy. If not healthy,
inspect their logs instead of repeatedly restarting or resetting their databases.

```bash
docker compose -f "$phase2" -f "$phase4" ps -a
docker compose -f "$phase2" -f "$phase4" logs --tail=50 \
  binance-account-shadow lighter-account-shadow
```

Then start the two new publishers only:

```bash
docker compose -f "$phase2" -f "$phase4" up -d --no-deps --no-build \
  binance-strategy-paper-shadow lighter-strategy-paper-shadow
docker compose -f "$phase2" -f "$phase4" ps -a
docker stats --no-stream
```

Do not use `down`, `down -v`, a bare `up` for the whole merged stack, or restart collectors,
NATS, Phase 3 or legacy paper workers as part of this startup. Both new images are reused
for the two venues. Existing market streams are not reconfigured; init adds only the
32 MiB / 72-hour intention stream, within the existing NATS disk budget.

## Evidence to return

```bash
docker compose -f "$phase2" -f "$phase4" exec binance-account-shadow \
  python -m live_engine.account_worker --database /data/account.sqlite --status
docker compose -f "$phase2" -f "$phase4" exec lighter-account-shadow \
  python -m live_engine.account_worker --database /data/account.sqlite --status
docker compose -f "$phase2" -f "$phase4" logs --since 15m --tail=100 \
  binance-strategy-paper-shadow lighter-strategy-paper-shadow \
  binance-account-shadow lighter-account-shadow
```

Return these plus `ps -a` and `docker stats --no-stream`. The read-only account status
checks balanced postings, closed equity against trade P&L, flat cash/equity and reservation
consistency. Process health alone is not proof of matching trade behavior.

Acceptance requires a complete eligible session, comparison of signal/contract provenance,
causal fill timing (not forcing latency-dependent fills to be identical), exit accounting,
and orderly recreation with the same tested image/database/durables. No need to wait for
Lighter to trade if native readiness rejects it correctly; exercise its execution using
the controlled fixture separately, without relaxing readiness.

Keep `GG_PHASE4_COMMIT` fixed to the deployed tested release when reconnecting. A new Git
commit/image is not permission to reuse an incompatible ledger. Release migration,
snapshots/backup restore and promotion are reviewed separately. Do not migrate legacy
equity or remove any old services until explicit acceptance and cutover authorization.
