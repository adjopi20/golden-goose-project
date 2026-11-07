# Execution sources

Maintained operational lifecycles are in `utils/engine/execution/`; paper fill
simulation and exchange IO are separate adapters under `utils/engine/adapters/`.
Existing paper and ETH Futures contracts retain their declared behavior and limits.
Shared source locations do not establish identical paper/live fills or lifecycle parity.

`frozen_v1/` remains pinned historical source for the six local candidates. Do not
edit it, relabel its release, or substitute the mutable engine during legacy resume.
