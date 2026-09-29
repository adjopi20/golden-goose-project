# Prepared-data → paper ledger bridge — 2026-09-28

Update 2026-09-29: [PAPER_FEED_V01.md](PAPER_FEED_V01.md) adds the local Binance
producer/worker. HYPE C1 no longer requires unrelated C2 references. The scope
description below records the bridge at its original creation, not the current
feed implementation status.

Scope: `paper_bridge.py` connects this model's existing `evaluate_session`,
`annotate_session`, `_gate`, and `keep` to reusable `live_engine.PaperOrderManager`.
No entry, exit, stop, reward or 0.25%/0.5% gross price-risk rule was changed.
The paper execution engine still sizes at the **actual entry fill**, from its
distance to frozen POC, bounded by 5× notional/equity. Fees and gaps can make
net loss greater than nominal risk. There is no cost-adjusted sizing overlay.

## Input contract

Call `submit_completed_bar(manager, symbol=..., profile=..., bars=...,
as_of_ms=..., references=..., atr_before=..., trend=...,
c1_calibration=...)` at each completed NY 15-minute bar from 09:15 through 12:00.

- `profile`: frozen same-day 01:00–09:00 Pre-NY profile produced by the model's
  existing 50-bin, 70%-value method.
- `bars`: the completed 09:00-to-current bars only, with aggressive buy/sell
  volume interpreted **natively on this venue**. Never include an unfinished or
  later bar. A missing full bar makes the candidate unready.
- `references`: `through_session_day` strictly earlier than the signal day;
  `slots` maps NY quarter-hour slot 0–11 to medians from prior sessions. This
  bridge requires ≥10 prior observations for the signal slot.
- `atr_before`: completed 1h ATR14 keyed by bar-open UTC milliseconds, known at
  or before that open. HYPE C1 requires it on the signal bar.
- `trend`: existing `trend_snapshot_15m` result at the exact completed signal
  bar. ETH uses its directional EMA200 gap; BNB uses ADX14. HYPE does not use
  this sizing feature. No EMA/ADX is added as an entry gate.
- `c1_calibration`: HYPE only, containing frozen train thresholds,
  `trained_through`, `valid_from`, and `valid_to_exclusive`. An expired/missing
  artifact causes `NotReady`; the old research fallback is *not silently used*
  in unattended paper operation.

The bridge calls the original baseline evaluator on only supplied completed
bars. It ignores its synthetic `NO_TRADE` cutoff row until 12:00. ETH's C2 union,
BNB's C2 initiative and HYPE's C1 veto use existing code paths. Candidate
snapshot, selector verdict and tier are then sent to the shared SQLite ledger.
The first baseline candidate locks the session even if the selector rejects it.
Later bar calls return `SESSION_LOCKED`; restarts reproduce the same lock.

## What this does *not* prove yet

The bridge validates timestamps and prior-history labels, but cannot certify
that an external producer calculated its profile, medians, ATR and trend
features correctly. It does not collect raw trades, subscribe to either venue,
build indicator history, publish a continuously running worker, or execute an
exchange order. Before unattended paper use, build a venue-native preparation
service with backfill/coverage checks and compare its per-bar signals and tiers
against the existing historical observation outputs on recorded data. Lighter
product mapping and trade-side normalization are still unverified.

`NotReady` is an operational data-health state, **not a new alpha filter**.
Research outputs may have accepted missing-feature fallbacks; do not compare
paper trade counts to those outputs without separating readiness exclusions.

Validation on installation: 35 focused tests passed, including long/short
paper-fill parity, restart/idempotence, partial/stop/time exits, bridge timing,
selector session lock, C1 validity, and ETH/BNB risk-tier selection. No new
full-period observation/backtest was run and no performance claim is made.
