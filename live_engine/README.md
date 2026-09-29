# Paper-runtime foundation v0.1

This is reusable **paper-only** infrastructure. It cannot send exchange orders
or reconcile real positions. `adapters/binance.py` now collects public data;
`market_data.py` prepares compact candles and profile buckets. The model-specific
worker and operating instructions live under
`models/trend_following_preny_profile_15m/docs/PAPER_FEED_V01.md`.
Research/backtests remain in `backtest_engine` and `risk_research`.

## What works

- SQLite WAL / FULL synchronization. One database is permanently bound to one
  venue and paper mode; model+market accounts have independent equity/state.
- Persisted first-candidate intention, causal feature snapshot, position,
  pending stop/TP1 triggers, fills, closed trades and closed-equity changes.
- Transactional event application. A failed commit consumes neither the event
  cursor nor its fills. Replayed source events do not create duplicate fills.
- A selector rejection still reserves the session's first candidate.
- Fill mechanics directly call `backtest_engine.replay_aggtrades.on_tick`.
  No second implementation of stop/partial/time-exit execution.
- Account configuration is immutable; a changed contract requires a new account.
- Consistent SQLite backup through its backup API, not copying a live DB file.

The database is authoritative after restart. Pending triggers survive restart.
This is local simulated execution recovery, **not exchange reconciliation**.

## Interface

`PaperOrderManager.register(...)` binds an account and allowed risk tiers.
`submit(..., signal, snapshot, risk_fraction, selected)` accepts the first causal
candidate from a model adapter. It does not generate the candidate or compute
the selector/sizing indicator. The risk tier is frozen for that trade.
`tick(account, normalized_trade)` advances the same paper-fill mechanics as the
shared backtester. It requires integer monotonic market trade IDs and UTC-ms
timestamps, with signals delivered before their first eligible market event.

Only one latest tick cursor per account is retained, **not a raw-trade archive**.
Source IDs older than that cursor are ignored; conflicting duplicates of the
latest ID are rejected. The Binance collector validates consecutive IDs and
repairs gaps with public REST before committing subsequent data. This continuity
rule must not be reused for Lighter without verifying its different ID semantics.

`python -m live_engine.runtime --config <file> --database <sqlite> --action init`
initializes ledgers; `status` inspects them; `backup --backup-to <new-file>` takes
a consistent snapshot. `consume` reads normalized JSONL events from stdin only
as a developer plumbing harness. EOF exits; it is not a live service or sweep.

`signal` event keys: type, account, signal, snapshot, risk_fraction, selected.
`trade` event keys: type, account, trade (timestamp_ms, agg_trade_id, price).
The model deploy templates are JSON-formatted YAML, parsed with the standard
JSON library. Do not add YAML-only syntax to those files.

## Explicit limitations / next implementation gate

1. Binance public feed + repair + completed-bar worker are implemented for local
   validation. Lighter normalization exists, but its historical delta/backfill
   continuity is not certified; there is no Lighter trading worker yet.
2. Model bridge reuses existing evaluator/selectors. Native kline warmup is
   explicitly labeled; full-session parity against raw research data still needs
   field validation. HYPE needs an in-date C1 calibration artifact.
3. `ticks` batches execution using the same `on_tick` function. Market checkpoints,
   profile accumulation and paper state share atomic transactions. OS writer lock
   prevents two collectors on one DB. Sustained throughput and recovery over a
   real session still need measurement before unattended server deployment.
4. Stop/TP fills are next-public-print proxies, not exchange matching. No funding,
   spread/impact model, lot rounding, liquidity or liquidation simulation.
5. Equity is **closed-trade equity**, matching research. Open/partial PnL is held
   in position state until final close. It is not available cash or mark-to-market
   risk. A time exit cannot happen without another incoming trade in this proxy.
6. No Docker/Compose daemon, external monitoring, watchdog, server provisioning,
   native stops, real execution adapter or automatic maintenance has been enabled.

Keep DBs out of Git, on persistent local disk (not a live OneDrive-synced DB).
Use one writer per venue. AI maintenance must not edit rules or access trading
credentials by default. These limits are operational scope, not new alpha rules.
