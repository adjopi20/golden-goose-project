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

## Remaining legacy-root relocation — 2026-11-01

Root market_data, models, analytics and apps are retired by explicit user request.
Public collector/store/broker code now lives in live_trading/collectors/market_data;
its existing server-paper constraints, DB schema/owner identifiers and calculations
remain unchanged. This is a local source/build-reference migration, not deployment.

All old models contents are moved under model_development/models. Existing maintained
Pre-NY and VWAP strategies are not overwritten: each old family is retained under
its family's legacy/ directory, including old operational support and evidence.
These are historical compatibility implementations, not new maintained strategies
or new phase runners. Other families retain their structure below the new root.
Local callers use explicit new namespaces. Historical run/release/inventory metadata,
data, credentials and state bytes are not rewritten. Old exact-path/hash resume can
fail; no shim, alias, exemption or automatic source substitution is introduced.

Cash-flow-aware daily portfolio reporting from analytics/backtest_report.py is
retained as phase_3/portfolio_report.py; it differs from Phase 2 trade metrics.
Uncalled analytics trade_log/performance_metrics are retired with exact source bytes
in the workspace migration bundle. Legacy ORB feature preparation, configuration
and triggers now live in phase_1/legacy_orb; the two old Pre-NY callers follow it.
Uncalled ORB execution/AI-state support modules are retired, not deployed. Existing
ORB data/profiles/rules/environment are moved verbatim, without loading credentials.
This does not add ORB support, migrate its pipeline to CandleContext or run a cache
build. Known absent ORB/btc_pny dependencies remain absent. The previously declared
statistical_jump_model solver source is also absent in this checkout at audit time;
no substitute solver is created. Its declared path follows the new models root.

All six frozen live candidates, their fourteen pinned source hashes and release.json
remain unchanged. Operational image entrypoints/build sources follow moved local
files; service names, volumes, DB ownership, schema and account rules stay unchanged.
No image build, service activation, order, backtest, market study, cache rebuild,
sizing application or deployment is performed. Earlier source/path retention notes
are superseded only for the explicit files and moves recorded in the workspace
remaining_roots_retirement.json bundle.

# golden-goose-project

## Structure migration — 2026-11-01

- [Model development](model_development/README.md): three phase destinations and current compatibility limits.
- [Live trading](live_trading/README.md): six-model local inventory and consolidation boundaries.
- [Canonical protocol](model_development/protocol/README.md): pointer to the single maintained source.

Existing code, models and artifacts remain in place. The new structure is an
additive foundation, not completed dynamic model backtesting or live deployment.
