# Collector sources

Maintained shared public feeds are in `utils/engine/collectors/`; venue data
adapters are in `utils/engine/adapters/`. One public stream may serve paper/live
accounts with independent consumer checkpoints. No service is started by imports.

The Burst market feed uses `utils/engine/deploy/market_feed/compose.yaml`
with project name `golden-goose-live-market` and
`GG_BINANCE_FEED_CONFIG=./burst-binance.json`. Set `GG_MARKET_IMAGE` to the
reviewed live-market image tag. Start only `nats`,
`broker-init`, and `binance-collector`. This uses a fresh public feed journal
for HYPEUSDT, INJUSDT, and UNIUSDC on an internal broker network. It does not
switch any armed worker to the broker. Existing paper projects retain their
default config and volume identity.

`live_trading/deploy/burst-shadow.compose.yaml` runs one disarmed Burst worker
per symbol against that broker. The workers have separate durable consumers
and journals and cannot be combined with arming or secret-file flags. The
collector owns REST gap repair. Workers acknowledge a broker batch only after
its trade cursor is committed. They rebuild their profile after an unfillable
gap or a source-epoch change. Preserve each shadow journal through profile
warm-up. The configured trade stream has a two-hour maximum age and a byte
cap, so a blank worker must accumulate its profile before comparison.

`frozen_v1/` remains an immutable dependency of the six historical local candidates,
as recorded in `model_development/models/legacy_frozen_candidates/release.json`; it is not the maintained feed.
