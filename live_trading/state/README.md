# State sources

Maintained storage implementations are in `utils/engine/state/`. Paper and live
accounts keep separate databases, ownership, balances, positions and checkpoints.
Promotion starts a fresh live account; it does not convert a paper ledger.

`frozen_v1/` remains an immutable dependency of the prior local candidate release.
