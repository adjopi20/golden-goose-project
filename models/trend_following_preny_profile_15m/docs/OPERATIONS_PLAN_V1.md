# Operations decisions — 2026-09-27

Applies to `trend_following_preny_profile_15m`. Planning/cleanup checkpoint only: no live runtime, server, credentials or orders were deployed.

## Runtime ownership

- Shared reusable `live_engine` orchestrates feed ingestion, scheduling, persistence, order lifecycle and reconciliation.
- All model-specific alpha, sizing configuration, contracts and Compose configuration remain in this model folder. No duplication of strategy logic by venue.
- Two venue-native workers initially: Binance and Lighter. Each builds its own profile/delta/indicators and may make different decisions. Common-signal execution comparison is not the current plan.
- Separate model/asset/venue state and virtual equity ledgers. One feed subscription per market shared by model instances; one coordinated order writer per account. Logical isolation does not require one server or process per model.
- Live financial isolation is not guaranteed by internal ledger labels or an exchange's isolated-margin switch. Actual account/subaccount mapping and capital reservations must be agreed before real trading. Never give each model the whole account balance independently.

## Small-server budget and storage

Initial capacity hypothesis: 2 vCPU / 4 GiB Linux VPS for two light workers; benchmark actual feed load and memory. A 1 vCPU / 2 GiB host may be enough without an AI maintenance agent but is not yet measured. Do not run research sweeps or local LLM inference on this host.

SQLite per venue, with per-model ledger keys and one coordinated writer, is sufficient as an initial design. Storage complexity is governed by record volume, not the number of table names. Six asset/venue streams produce 576 fifteen-minute bars/day, roughly 210,240/year; at an illustrative 2 KB/bar this is about 420 MB/year before indexes/features/backups. This is an estimate, not a measurement or total-disk guarantee.

Persist order intentions, acknowledgments, all fills, positions, protective-order state, feature/decision snapshots, equity, fees/funding, feed checkpoints and errors. Persist DBs outside replaceable containers. Set log rotation/disk alerts, use consistent SQLite backups and keep an off-host copy. Never prune live state or backups merely to clear space automatically.

## Raw-trade policy

Process every required trade but do not retain every exchange raw trade indefinitely on the trading VPS. No permanent raw archive by default. Retain a bounded diagnostic journal with a byte/age cap during commissioning; archive selectively off-host if useful. Size the cap from measured traffic, not an assumed fixed daily volume.

Keep completed 15m buy/sell volume and OHLC, causal indicator state, profile snapshots and source sequence/checkpoint IDs. Exact profile reconstruction needs more than OHLC candles: preserve the Pre-NY price-to-volume accumulator/checkpoints until profile finalization. The 50 equal-width bins depend on the final window low/high; do not silently replace the researched profile algorithm with a coarse incremental approximation.

If a process fails between checkpoints, backfill the missing interval and deduplicate before using the state. If the venue cannot supply the necessary missing trades/history, mark the session unready rather than fabricate volume/profile. Retaining bounded raw events helps diagnostics but does not guarantee arbitrary historical replay.

## Hermes as maintenance assistant

The user wants AI-assisted maintenance, not AI-selected trades. This is compatible with a deterministic engine.

- Hermes (if selected) is a separate operations process/container using a remote LLM provider, not a model dependency. LLM usage is a separate cost; use event-triggered diagnosis and bounded budgets rather than continuous polling.
- Default access: sanitized logs, health/status, resource usage and read-only diagnostic DB views. No trading secrets, withdrawals or ad-hoc order tools.
- Approved maintenance actions should use narrowly scoped, tested wrappers. Docker socket access and broad sudo effectively grant host control; do not expose them as a convenient default.
- For rule edits, deploy/restart with an open position, database changes or destructive cleanup: explain the action and get approval. Use versioned releases, backups and rollback. Do not auto-fix production code from a log message.
- Monitoring, reconnects, safe restart reconciliation and native protective stops must work without an AI provider or this chat being available.
- This chat can prepare code and help via approved SSH access; it is not an always-on service. Do not paste secrets into chat.

Official references checked 2026-09-27: [DigitalOcean pricing](https://www.digitalocean.com/pricing/droplets), [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security). Provider capabilities/prices must be rechecked at installation.

## Cleanup performed in this step

Delete the explicitly named `notebook/` tree (19 files, approximately 38.46 MiB at audit). Retire obsolete ORB live/AI entry points and their launcher/tests, while preserving active cache-builder dependencies and uncommitted changes. Full `orb_live_agent` removal is deferred until its still-used utilities are migrated; see its replacement README. No unrelated model, research output, credential file or reusable backtest/risk engine is deleted.
