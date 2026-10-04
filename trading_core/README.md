# Shared deterministic trading core — V1

This package is reusable by research and runtime. It contains no feed, database,
order submission, experiment runner, or future-outcome analysis.

- `session.py`: timezone-aware session clock; NY daylight-saving transitions remain explicit.
- `profiles.py`: existing equal-width volume profile and contiguous value-area calculation.
- `indicators.py`: existing causal indicator calculations, extracted without changing formulas.
- `contracts.py`: versioned JSON event and strategy-intention envelopes.

## Event contract

Identity includes venue, product, native instrument ID, source, event type,
event timestamp, source sequence and revision. If a source provides no sequence,
the producer must persist its local stream sequence across restarts. Receipt time
does not change identity. A changed payload under the same identity must be treated
as a conflict, not silently overwritten; corrected data uses a revision.

`event_timestamp_ms` is source event time. `available_at_ms` is the producer's
recorded availability time, not a claim that reconstructed history was available
live. `received_timestamp_ms` is receipt at the boundary. They use UTC Unix
milliseconds and must be ordered. Completed bars include open/close timestamps
and cannot be available before closing. Quality is explicit: complete, partial,
gap, stale or invalid. Native instrument identity must not be replaced with a
model's cross-venue alias.

## Intention contract

An intention records environment, venue, account/allocation, model and contract
versions, configuration hash, calibration ID, feature version/as-of, snapshot
reference, session, direction, eligibility/expiry, initial stop, risk fraction,
entry reference and exit policy. Risk fraction is price-loss risk relative to
allocated equity, not notional exposure or margin.

Candidate identity is stable even if a proposal's stop or size changes. A future
account owner must compare content and reject/reconcile conflicting proposals;
it must not submit a second order just because proposal content changed.
The `server-live` enum is metadata, not permission to place orders.

## Phase 1 boundary

These contracts are not yet connected to the legacy worker's database or order
format. Wiring occurs during the collector/consumer/account-owner migration.
Current workers retain their existing execution path. JSON validation rejects
non-finite values and invalid timing/geometry; payload-specific market checks,
persisted deduplication and authorization remain boundary responsibilities.
