# Phase 1 — contracts and deterministic runtime boundary

Date: 2026-10-04. Scope: local implementation only; no VPS cutover.

## Delivered

1. `trading_core`: shared profile, clock, indicator calculations and V1 JSON contracts.
2. Model-local `runtime/`: evaluator/annotation/selection without research imports.
3. Compatibility re-exports: existing research commands continue using the same functions.
4. Docker source allowlist: model research scripts and runs excluded.
5. Import-boundary, contract, extraction/golden-profile and intention-adapter tests.
6. Offline evaluator replay against separate copies of the Phase 0 snapshot.

## Acceptance evidence

- Scoped regression suite: **109 passed** in 14.75 seconds on 2026-10-04.
  Covers new contracts/boundaries plus existing evaluator, research compatibility,
  feed, paper order manager and partial/time-exit tests. Not the entire legacy repository.
- Original AST fingerprints retained for 20 extracted functions.
- 15 golden volume-profile cases, including flat prices and tie handling.
- Isolated allowlisted-image-tree import smoke test (not an actual Docker build).
- Phase 0 capture: `phase0_20261004T073320Z_216971.tar.gz`.
- Capture SHA256: `5bc07188dce36b2404e8638c4cef9747112cda7f911fb2d0e1c7b6ac002567ea`.
- Session replay: 2026-10-03; Binance 39/39 and Lighter 39/39 evaluations match.
- Compared fields: state, candidate, reason, selected, risk_fraction, signal and snapshot.

The replay command creates a NEW output directory and database copies:

```powershell
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe -B -m infra.ops.verify_evaluator_baseline `
  --capture-dir "PATH_TO_EXTRACTED_PHASE0_DIRECTORY" `
  --output-dir "tmp/phase1_replay_NEW_DIRECTORY"
```

It does not send orders, connect to an exchange, modify the original capture,
or refit calibration. It is not proof of fill parity or original event availability.

## Deployment boundary

Do not rebuild/restart production solely to complete this phase. Existing server
containers continue on their current image. No Compose topology or database
migration is required by Phase 1. An actual Docker build remains to be verified
before any future release.

Next: Phase 2 separates venue-native collectors and introduces durable normalized
events. Keep the old workers running until shadow comparison, gap/reconnect and
resource-use gates pass. Broker/collector implementation and execution ownership
are not delivered in this phase.
