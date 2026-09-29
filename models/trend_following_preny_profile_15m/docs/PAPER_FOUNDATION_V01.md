# Paper foundation checkpoint — 2026-09-28

Status: local infrastructure only; no changes to alpha, exits or sizing contracts.

Implemented shared `live_engine`: venue-bound SQLite, independent model ledgers,
persisted signal/feature snapshots, first-candidate session locks, pending exit
triggers, fills/trades/equity journal, transactional rollback, restart recovery
and consistent backups. It calls the existing shared backtest `on_tick`; it is
not another backtest/sweep engine. No raw-tick archive is written.

Eleven initial automated tests cover long/short execution parity (including partial TP and
time exit), restart after stop/TP trigger, duplicate handling, isolated equity,
configuration lock, rejected-candidate lock, atomic rollback, invalid timestamps,
risk-tier freezing, closed-equity compounding and backup. This is a foundation
test, not live readiness.

Model-specific ledger config/examples stay in `deploy/`; shared implementation
stays in root `live_engine/`. No placeholder exchange adapter is claimed to work.
See `deploy/README.md` for the next readiness gate and initialization command.

Not yet implemented: market feeds, continuous strategy evaluation, feature
warm-up/backfill, production health/scheduling, efficient feed batching,
Docker/Compose service, real orders or exchange reconciliation. Lighter native
product mapping and current fees remain unverified. The next step is a causal
model/feed bridge with parity checks, not server installation or real trading.
