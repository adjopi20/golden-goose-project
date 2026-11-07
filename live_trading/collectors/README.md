# Collector sources

Maintained shared public feeds are in `utils/engine/collectors/`; venue data
adapters are in `utils/engine/adapters/`. One public stream may serve paper/live
accounts with independent consumer checkpoints. No service is started by imports.

`frozen_v1/` remains an immutable dependency of the six historical local candidates,
as recorded in `model_development/models/legacy_frozen_candidates/release.json`; it is not the maintained feed.
