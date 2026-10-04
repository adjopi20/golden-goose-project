# Runtime/research boundary — Phase 1, 2026-10-04

Model rules and frozen ETH/BNB/HYPE contracts are unchanged.

The deterministic evaluator now lives in `runtime/`:

- `baseline.py`: baseline session evaluation.
- `effort_result.py`: existing effort/result annotation.
- `selectors.py` and `gates.py`: existing selection and calibrated C1 gate.
- `preparation.py`: existing profile, candle and causal-context preparation.
- `evaluator.py`: existing completed-bar decision and risk tier.
- `intention.py`: optional conversion of a selected decision into the V1 intention contract.

Runtime imports the shared `trading_core` calculations, not research modules.
Research CLI modules re-export the same functions, preserving old commands and
preventing research/live formula drift. Calibration fitting remains a separate
operation; no model is fitted by the live evaluator.

The existing Binance and Lighter workers still collect, persist and execute paper
positions as before. Session reservation on the first candidate, rejection/WAIT/
NOT_READY handling, venue-native warmup requirements, calibration expiry, entry
deadline, POC stop, partial TP, sizing and time exit are not changed here.

Docker now copies the runtime package and the three worker/calibration entrypoints,
not the model's analysis scripts or runs. The legacy reusable paper-execution
dependencies remain allowlisted until the account-owner migration.

This is a dependency separation, not a new entry experiment. The intention adapter
does not authorize execution and is not yet used by the legacy order manager.
Phase 0 snapshot replay verifies decisions only; it does not prove historical feed
availability, fills, latency, or recovery of an open position.
