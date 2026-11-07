## Backtest consolidation — 2026-11-01

The user explicitly authorized eliminating the root backtest_engine folder.
Replay, metrics, exit-policy and legacy path-audit functions are now maintained
in model_development/phase_2/backtest.py. Phase 2 orchestration helpers remain
internal; backtest.py is the only public phase runner. Model decisions stay in
their strategy scripts. Actual fill/accounting math still reuses the unchanged
trading_core.paper_execution. Worker Config imports now go directly to that core.

All local executable callers/tests and local paper Docker build references are
adjusted. This does not activate, rebuild or deploy services. Existing remote
images/checkouts are not checked or switched. Existing support remains VWAP and
Pre-NY HYPE C1 on their declared interfaces; prepared-signal compatibility also
serves old Pre-NY workflows and the ORB app's shared metrics writer. No ETH/BNB
adapter, trading rule, sizing or new backtest is introduced.

The unused merge_candle_caches.py front door is retired. Its source and every
removed/modified file's exact original bytes/hashes are in the workspace migration
bundle, not another active engine. The uncalled research_exit_variants.py imports
absent legacy models; its exact source is archived under _archive/
backtest_retirement_20261101 because historical selected-exit evidence names it.
The moved path-audit cache helper retains its existing external legacy dependency
and does not imply those absent legacy models are now supported.

Historical run/evidence/release manifests are immutable and retain original paths
and hashes. They are historical identities, not promises that retired paths still
resolve. Exact-path resume depending on retired files or modified caller hashes
must fail explicitly; no exemption, shim or automatic source substitution is added.
Current frozen live release files/manifest and operational state stay unchanged.
Earlier instructions promising original caller hashes are superseded only for the
explicit import/path changes recorded in this migration bundle. No result artifact
or historical metadata is deleted or rewritten.

CLI: python -B model_development/phase_2/backtest.py --help

Without --model, the pre-existing prepared-signal CLI remains available. Legacy
descriptive path audit uses --experiment through this same runner. Old module
commands backtest_engine.* are retired, not silently aliased. Former engine tests
now live in phase_2/tests. This is relocation evidence, not market validation.

Prepared development runs default to `--id-policy record`: replay chronological
observed rows while recording both forward ID gaps and backward ID transitions
as source segments. Original timestamps and IDs remain unchanged, including
when IDs recur in a later segment. Continuity remains unverified; missing prints
are not reconstructed. Backwards timestamps, adjacent duplicate IDs, ambiguous
same-timestamp ID resets and invalid prices/quantities still fail validation.
`--id-policy error` retains strict continuity checking. Existing valid measured
candle caches are reused; this runner policy does not change cache identity.

Batch grids may declare `model_cases`: a list of correlated model configuration
objects applied after each Cartesian `model` combination. This supports RM plans
with their own allowed exit horizons without creating duplicate effective jobs.
`--batch-layout folders` keeps the master `run.json`, ledger and equity image,
plus a full result folder per deterministic job ID. Each child can retain its
own strategy traces. The default `master` layout is unchanged. Asset caches use
`--cache-dir/<instrument>` in batch mode; pass the shared cache root, not the
already instrument-specific directory. A failed batch is retained, not resumed
or silently overwritten.

`burst_breakout/window_rm_seed_grid.json` prepares one B / 12H profile / 1m-5m /
rm-d / 24H run per selected asset. `window_rm_all_grid.json` declares all 252 BTC
and HYPE configurations: 3 profile/P85 windows x 2 setups x 3 flow pairs x 7 RM
cases per asset. These remain research configurations, not a new checkpoint.
New runs include a `loss_reward` summary in each account and single-account
receipt: net losing outcomes include fees, win payoff and >=5R tail capture are
separate, and the actual risk fraction and initial stop distance remain explicit.
