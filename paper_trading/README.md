## Shared paper/live engine — 2026-11-02

The user approved sharing collector/data adapters, monitoring, persistence and
operational execution sources under utils/engine. Root live_engine and infra are
retired after explicit caller migration. The next-print paper simulator is now
utils/engine/adapters/paper_execution.py; exchange IO remains a separate adapter.
Existing paper account/order orchestration and the ETH-specific Futures lifecycle
are preserved as declared legacy contracts, not falsely certified as one universal
execution behavior. Their fill, risk, order, timing and DB rules are not changed.

Public collector events have no account-mode field: one source stream/epoch can
feed independent paper/live consumers. The same production adapters are reused.
The collector now also accepts explicit shared-market ownership for a NEW feed DB;
old server-paper/local-dev identities remain valid and cannot be silently relabeled.
Existing Pre-NY session/profile enrichment is legacy behavior, not a mandatory rule
for every model. No collector, broker, subscription or service is started here.

paper_trading/models/<submodel>/ is the new paper package destination. Its strategy
and model contract travel unchanged on promotion; runtime account configuration is
external. New live accounts start fresh: never copy paper positions, balances,
pending orders, execution cursors or account DBs. Collector source identity can be
shared, but account consumer checkpoints/state remain independent. Existing DB
schema/owner/release guards remain intact. Shared storage code does not mean shared
account state. Model-specific decisions remain in one strategy script.

New model contracts explicitly pin the engine release manifest hash; the manifest
pins engine sources, its non-engine calculation dependencies and the environment.
verify_release checks them before use. This pass does not invent six new model
packages or promote/activate existing candidates. All six original frozen live
candidates and their fourteen release sources/manifest retain exact bytes; those
historical pinned copies are intentional version isolation, not active engines to
deduplicate silently. No original release identity or saved run metadata is rewritten.

Local callers, tests, build COPY sources, Compose base/overlay paths and current
documentation follow the new homes. Old module/CLI paths are retired without shims,
hash exemptions, backdating or silent source substitution. Original source bytes
and before/after/move hashes are in the workspace shared_engine_migration.json bundle.
No model rule, market study/backtest, cache rebuild, account/state upgrade, service
activation, order, dependency installation or deployment is authorized by this move.

## Approved trading-core retirement — 2026-11-01

The user approved utils/closed_candle_context.py for the shared closed-candle
availability/evaluation boundary and utils/market_session.py for the NY/DST clock.
Paper-execution calculations now belong to paper_trading/execution.py, reused by
research/replay and legacy paper callers. Model development still has independent
observe/backtest/evaluate phases; paper and live remain separate major stages.

The root trading_core folder is retired after caller migration. Legacy Pre-NY
research calculations are preserved as indicator/legacy_preny.py because HYPE C1
still uses its historical ATR initialization; standard MA/ATR/ADX APIs remain
separate and unchanged. The pure profile primitive is indicator/value_area.py;
it does not replace the differently specified indicator/volume_profile.py API.
Wire validation reuses the existing byte-identical pinned live contracts module.

All six local frozen live candidates already embed their model-specific indicator/
profile/session calculations. Three Pre-NY candidates each contain the same three
indicator functions (9 AST matches). They do not import mutable indicator/utils/
paper/research implementations. No second set of live submodel scripts is created.
The user accepts redeploying models from scratch; this does not authorize replacing
or redeploying the live infrastructure. Frozen source/release hashes, operational
state/configuration, service activation and server infrastructure remain unchanged.
Local import and Docker build-source references follow the new file homes only.

CandleContext currently gates Phase 1 VWAP, Phase 2 VWAP/HYPE, and Phase 3 jump;
Kelly/MC outcome diagnostics do not need it. Existing operational paper/live paths
do not automatically acquire this boundary by moving it into utils. It controls
available input, aggregation/warmup and evaluation timing, not entry/exit rules.
Main-project defaults use the new utility. Explicit external legacy cores retain
their original clock and its imports through source-checked binding; no fallback
to another checkout, timing backdating, source/hash exemption or silent resume.

Historic manifests retain original identities. Exact old-path/hash resumes can
fail after retirement; exact before bytes and the relocation map are in the
workspace migration bundle. No old results, source caches or SQLite state are
rewritten. Earlier original-path preservation promises are superseded only for
the approved files/callers in this retirement. No new trading rule, study, market
backtest, sizing application or deployment is introduced.

# Paper trading

execution.py contains Config, Portfolio, next-print fills, partial/stop/time exits,
fees and closed-equity accounting. Research/replay and legacy paper callers reuse
this one implementation. Importing it starts no feed/account and sends no orders.
Frozen live release execution remains pinned separately under live_trading.

Canonical protocol remains in the documentation workspace research_designs/research_protocol.
