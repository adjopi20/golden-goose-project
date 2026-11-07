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

# Research model home

mean_reversion_by_vwap/ is the maintained VWAP model, sole full-history strategy
MD and tests. Each new model keeps one decision script and one maintained MD.
Old VWAP source/grid/tests remain frozen for exact-hash historical resumes;
other root models and all run artifacts remain pending explicit migration.

trend_following_preny_profile_15m/ now owns its maintained strategy.py, full-history
RULES.md and tests. Global prepared Phase 2 support remains HYPE C1; baseline/C2
helper consolidation does not add other phase/asset capabilities.
