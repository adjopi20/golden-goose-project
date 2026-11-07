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

# Legacy ORB compatibility utilities — not a deployment target

Cleanup: 2026-09-27. The obsolete live/AI runner, its Docker launcher, unused replay/sweep entry points and obsolete tests were removed at the user's request.

Do not deploy this directory. The proposed live runtime is `live_engine`; its implementation is not yet complete.

The directory cannot yet be removed wholesale:

- `models/trend_following_preny_profile_15m/run_plug_asset.ps1` and `run_selected_exit_sweep.ps1` invoke `orb_live_agent.feature_cache`.
- `feature_cache.py` imports `config.py` and `trigger_observer.py`.
- Some historical ORB research files still reference `fast_orb_backtest.py`, `paper_broker.py`, `risk_gate.py` and `execution_engine.py`. Those compatibility modules were preserved; this does not certify the old ORB experiments as working. Some already depend on deleted strategies.
- Existing `.env`, data, profiles and rules were not inspected for secrets or deleted.

The modified legacy smoke test was preserved outside the active test tree at `_archive/2026-09-27_orb_live_cleanup/test_state_builder_smoke.py.txt` before its removal. Other pre-existing user modifications were retained.

Next cleanup, separately: relocate the active feature/cache builder to a shared utility with regression tests, update callers, then reassess the remaining ORB dependencies. Do not merely delete the package and break fresh-data cache preparation.
