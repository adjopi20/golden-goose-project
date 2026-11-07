## Current operational copies — 2026-11-03

Research models remain here permanently. Operational copies are exclusively live
(ETH and the dual BTC notifier) or paper (Binance HYPE/BNB and existing LTC v33).
See live_trading/README.md and paper_trading/models/README.md for exact readiness.
The original six offline candidates moved to models/legacy_frozen_candidates;
older candidate-location descriptions below are historical.

## Research/evaluation root retirement — 2026-11-01

The user authorized eliminating root research/ and post_modelling_evaluation/.
The maintained observer/batch remain in model_development/phase_1/; duplicate
root observer implementations are retired, not replaced by forwarding shims.
Shared raw validation, measured OHLCV caching/aggregation and exact-print lookup
now live in model_development/observation_cache.py and are reused across phases.
Its source bytes/cache-builder digest are unchanged, preserving current measured
cache identity; raw/cache data are not moved or rebuilt by this relocation.

The statistical-jump calculator and its tests/docs now live in Phase 3 alongside
the other evaluators. evaluate.py remains the public dispatcher and retains native
flags, selected-run requirements and render mode. Its declared existing numerical
solver remains models/statistical_jump_model/model.py; no solver/formula/default
change or duplicate numerical implementation is added. phase identifiers such as
post_modelling_evaluation are schema values and remain stable, not folder paths.

Existing runs/ and chart_exports/ from the retired evaluation folder are moved
under model_development/phase_3/ byte-for-byte, including every run.json, Parquet
and PNG. Historical path/hash/inspection evidence is not rewritten. Select the
new run folder explicitly for later diagnostics/rendering; immutable provenance
can still name its original location. Source/read/render contracts are unchanged.

Local imports, code provenance, compatibility model launchers, tests and current
documentation follow the new homes. Old root CLI/module paths are retired. No old
hash/path resume exemption or auto-substitution is added; an old exact-path resume
may fail rather than pretend the source path is unchanged. Exact removed source
bytes, before/after hashes and artifact moves are in the workspace migration bundle.
Earlier source-path retention promises are superseded only for these approved roots.

No market study/backtest, source-data cleanup, cache rebuild, model-rule change,
checkpoint, automatic phase transition, service/state change or deployment.

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

## Phase 3 risk-tool relocation — 2026-11-01

The user authorized eliminating the root risk_research folder. Maintained Kelly,
Monte Carlo/stress and sizing calculators now live in model_development/phase_3/.
kelly.py and monte_carlo.py replace forwarding wrappers with the existing math.
Stress/scenario/block helpers and its sizing-study validation are consolidated
in monte_carlo.py. Independent volatility_sizing.py and indicator_sizing.py remain
separate; ledger_io.py owns validated common ledger input/output and receipts.
evaluate.py remains the sole documented public runner; it dispatches these local
calculators, preserves native task flags and explicit selected-run behavior.

Configs, the context data file, tool docs and tests move into Phase 3. Config
path_root is rebased to the SAME project root; internal config/data references
follow the new home. Dataset paths, cache directories, output locations, seeds,
formulas, sizing choices and calibration defaults are unchanged. Old reports,
run/evidence manifests and existing caches stay in place and are not rewritten.
New receipts capture the actual Phase 3 code identity. Old risk_research.* module
commands are retired; exact-path/hash historical reproducibility needs its
original source environment, not a silent namespace/hash exemption. Exact before
bytes/hashes and the relocation map are in the workspace migration bundle.

No study, real-data evaluation, backtest, automatic phase transition, sizing
application, deployment or live state change is authorized by this relocation.
The jump calculator/solver remain at their current declared paths; this move does
not incorporate or migrate them. Earlier no-relocation/original-caller-hash notes
are superseded only for the explicit Phase 3 import/path changes recorded here.

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

## Phase 1 implementation home — 2026-11-01

The maintained observer and batch helper now live in model_development/phase_1/.
observe.py is the public runner; observe_batch.py is its internal helper. Phase 2
imports the maintained schema/instrument helpers. The shared validated cache stays
in research/observation_cache.py because Phase 1/2/3 use the same implementation.

research/observe.py and research/observe_batch.py remain byte-for-byte frozen
compatibility sources for historical callers and exact-hash resume. They are not
parallel maintenance targets. Existing model compatibility helpers and historical
evaluation callers remain unchanged; new Phase 1 commands use the maintained runner.
No model/core path substitution or hash exemption is added to resume. Continue an
old run with its original paths; moving a model is not permission to resume it.

Relocation changes only bootstrap, explicit imports, project-root depth and cache
provenance path. Execution, validation, schema, fees, configuration and resume
checks are unchanged. Verification is static AST/source/hash comparison and
import/CLI-help checks only. No research, replay/backtest, data/state movement,
artifact deletion, deployment or automatic phase transition.

# Model development — current migration map

Current status verified from local source on 2026-11-01. The active task is
MIGRATION ONLY: paths, imports, compatibility and documentation. Missing capabilities
are not migration work and do not authorize rule changes or backtest execution.

| Phase | Public entrypoint | Existing implementation/delegation |
|---|---|---|
| 1 | [observe.py](phase_1/observe.py) | Owns observer and batch implementation in phase_1/; current interfaces only. |
| 2 | [backtest.py](phase_2/backtest.py) | Dynamic model loading and batch/grid via phase_2 helpers; old prepared-signal CLI remains compatible. |
| 3 | [evaluate.py](phase_3/evaluate.py) | Reuses existing calculators; explicit selected-run input for Kelly/MC/jump, native inputs for other tasks. |

The user invokes each phase independently. No automatic transition or policy
application. These statuses describe existing code; no new capability is added.

| Maintained component | Current home | Preserved legacy location |
|---|---|---|
| VWAP research model | models/mean_reversion_by_vwap/monthly_sd_reversion.py | Root models/mean_reversion_by_vwap script/grid/tests: frozen for exact-hash resumes. |
| Pre-NY research family | models/trend_following_preny_profile_15m/strategy.py | Root Pre-NY model/runtime/helpers/tests: frozen for historical callers. |
| Standard research indicators | Main-project indicator/research/; separate MA, ATR, ADX, VWAP, CVD, order_flow modules | Executor copies and main legacy short-name indicator APIs remain intact. |
| Research candle clock | Main-project trading_core/candles.py | Explicit historical executor clock remains available. |
| Historical six offline candidates | model_development/models/legacy_frozen_candidates/ | Exact original sources/release; not operational stage packages. |

Each maintained research model has one full-history strategy MD. Old strategy
MD locations redirect to those documents; historical code/artifacts are not deleted.
Pre-NY still uses its declared legacy ATR formula; relocating standard indicators
does not change that decision. Existing support remains VWAP and HYPE C1 for the
respective documented interfaces; no new Pre-NY Phase 1 or ETH/BNB backtest adapter.

The single canonical protocol stays in the documentation workspace at
C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/research_designs/research_protocol/.
[protocol/README.md](protocol/README.md) is a pointer, not a second protocol.

research/, backtest_engine/, risk_research/, post_modelling_evaluation/ and declared
trading_core helpers still supply compatibility/shared implementations. Their
continued presence is intentional, not evidence that another engine is needed.
Root storage/ and utils/ remain outside model_development and live_trading.

Deferred migration/cleanup includes other legacy research model families, actual
operational collector/service/state wiring, and artifact retirement. Those require
their own dependency/path audit. No service switch, deployment or cleanup occurs
in this documentation pass. Existing unsupported features remain unsupported.

## Historical migration records — retain as history

The following text records earlier implementation stages. Statements such as
"not implemented" or "next" apply to those stages, not the current map above.

# Model development — additive migration foundation, 2026-11-01

Phase 1: python model_development/phase_1/observe.py <existing observe flags>
Phase 2: python model_development/phase_2/backtest.py <existing signal-replay flags>
Phase 3: python model_development/phase_3/evaluate.py --task <task> -- <task flags>

The first two launch the unchanged legacy implementations in a separate process,
preserving caller-relative input/output paths and exit status. Phase 3 routes to
existing calculators. No numerical engine, trading rule or raw dataset is copied.
Run with the project's existing Python environment; no packages are installed.

This stage does NOT implement Phase 2 --model, general model/execution/capital
grids, the Phase 3 --run/Parquet adapter, or new artifact schemas. The backtest
wrapper retains legacy multi-artifact signal replay. Tasks retain their native
contracts: Kelly/stress expect compatible JSONL/config; jump accepts its existing
market/trade inputs. Unsupported flags fail through the existing parsers.

Existing commands, models, artifacts, cache identities and code hashes remain
unchanged. Helpers currently stay in research/, backtest_engine/, risk_research/
and post_modelling_evaluation/. These are compatibility implementation locations,
not new canonical public entrypoints. Do not delete them while wrappers use them.

Next: establish common model capability/callback contracts for Pre-NY and VWAP;
extend this Phase 2 front door using existing replay/accounting, test parity,
then migrate ownership and adapters. No live services are switched here.


## Phase 2 dynamic adapter — 2026-11-01

The initial --model limitation above is superseded for the explicit single-source
prepared-intention adapter. model_development/phase_2/backtest.py now dynamically
loads a script exposing prepare_backtest(request). HYPE C1 is migrated; its old
strategy.py --backtest command delegates to this global adapter. Candidate rules,
features/calibration and stop/exit remain in the model. Execution/fees/compounding
still use backtest_engine.replay_aggtrades.run_phase2 and existing paper_execution.

This is NOT universal model support: VWAP's stateful observer/absolute target and
uncapped holding horizon need their own shared adapter. General grids, multiple
raw files/assets and the full global multi-job contract remain pending. Unknown
flags and observer-only models fail explicitly. Phase 3 Parquet adaptation and
six-script live consolidation are still pending. Live/server paths stay intact.

Eight foundation/dynamic-adapter tests and six HYPE Phase 2 tests passed, including
the 205-day synthetic CLI. Native-engine parity covers fill/fees/quantity/equity;
no full-market replay or new performance evidence. Canonical details remain in
the single PROTOCOL.md implementation record; no protocol copy is created here.


## Shared research lifecycle — 2026-11-01

Supersedes the earlier requirement for a separate VWAP adapter: one Session in
model_development/lifecycle.py now drives both prepared intentions (HYPE C1) and
prepared-candle models (VWAP). The same global Phase 2 --model front door loads
the supported interface. Strategies/calibration stay in their existing script;
all sizing/fill/partial/fee/exit mechanics still use existing paper_execution.

Model owns pending/accepted setup state; Portfolio owns executed positions and
equity. State identities are checked. Existing accept_fill and record_exit plus
optional on_execution receive actual entry/partial/exit and rejected/cancelled
events. No same-print entry/exit or backdated delayed decision. Absolute model
targets and uncapped holding are preserved. Submodel equity stays independent.
New output retains three files with per-account identities/metrics and labelled
equity curves. Failed chart/master generation cannot become a complete run.

The raw data remain strictly validated and chronological. No live/paper service,
model formula, cache builder, historical result or artifact is changed. Phase 1
still uses its existing implementation until explicit migration/parity. General
grids/assets/multi-file, dynamic trailing/limit protocols, persistent state and
Phase 3 Parquet adapters remain unsupported. This is research infrastructure,
not proof of market edge, exchange fills or deployment readiness.


## Phase 2 batch/grid — 2026-11-01

backtest.py is the main orchestrator: --batch --model <script> --input <assets...>
--grid-file <json>. Internal batch.py reuses the same model loader, lifecycle,
execution engine and metrics, with independent equity per asset/config/submodel.
No model-specific backtest runner or global HYPE/VWAP rule is added.

One new run folder still contains only trades.parquet, run.json, equity_curve.png.
Per-account IDs/metrics and model/execution grid configs are recorded; the image
separates P&L currency units and preserves each account's coverage. No pooling,
automatic selection, extra job folders or artifact cleanup. Failed jobs/charts/
source checks leave the parent failed; prior completed rows remain explicit.

Current limits: one selected model and one strict raw file per instrument. Multiple
assets/configurations are supported; same-asset split files, resume/record-gap,
general execution costs, trailing/limit protocols and unadapted legacy models are
still unsupported. Phase 1/live and Phase 3 migration status otherwise unchanged.
Canonical methodology is still the single protocol folder referenced above.


### Batch grid usage (synthetic names, replace with declared sources)

This is the versioned Phase 2 format, not the older Phase 1 flat grid:

```json
{
  "schema_version": 1,
  "model": {"timeframe": ["1h", "4h"], "sd_levels": [[1, 2, 3]]},
  "execution": {"risk_fraction": [0.005, 0.01]},
  "assets": {
    "AAAUSDT": {"venue": "unverified", "product": "linear_quote_margined", "quantity_unit": "AAA", "pnl_unit": "USDT"},
    "BBBUSDC": {"venue": "unverified", "product": "linear_quote_margined", "quantity_unit": "BBB", "pnl_unit": "USDC"}
  }
}
```

The model namespace above illustrates knobs declared by the VWAP research model;
other models expose their own parameters and must reject unsupported ones. It is
not a global instruction to use those timeframes, SDs, assets or risk sizes.
No source venue is certified by a filename. Declare provenance accurately.

```powershell
python model_development/phase_2/backtest.py `
  --batch --model <strategy.py> `
  --input <AAAUSDT-aggTrades-source.parquet> <BBBUSDC-aggTrades-source.parquet> `
  --grid-file <grid.json> --shared-core <executor-checkout> `
  --cache-dir <shared-cache-folder> `
  --iteration <existing-iteration-id> --inspection-status development `
  --output <new-run-folder>
```

Replace angle-bracket placeholders; this is not an executable full-market run.
Declare the grid before inspecting outcomes. All combinations are retained;
changing or selecting from outcomes remains selection under canonical PROTOCOL.
Batch jobs are sequential and cache-backed, not a guarantee of large-data speed.


## Independent phases and explicit Phase 3 --run — 2026-11-01

Each phase has its own user-invoked orchestrator: Phase 1 observe.py, Phase 2
backtest.py, Phase 3 evaluate.py. There is no automatic transition between them.
The user decides whether/when to run any phase or apply a sizing estimate.

evaluate.py now accepts an explicitly selected completed run.json/trades.parquet
through --run for kelly/monte-carlo. Account/configuration/submodel selection must
be unambiguous, including batch accounts. Source outcomes and their gross/net/
fees remain intact and hash-checked. Computation never starts observation,
backtest or trading. Other task --run adapters remain unsupported; legacy flags
after -- (including jump render --run) still work.

Kelly and MC reuse factored helpers in the existing risk_research calculators;
their numerical kernels and legacy CLIs remain compatible. New exit-UTC calendar
bins retain multiple trades/day; this is not parity with old NY-entry-session
studies. Phase 2 MC defaults to observed net account-return pairs. Phase 1 MC
requires explicit capital/risk/leverage for a labelled conditional ledger study,
not a backtest. No automatic policy export/application or model promotion.

This new output is one NEW folder with run.json only: results/parameters/seed,
source/code identities, account, gross/net/fee totals, censoring, scope and status.
No raw dataset, source copy, paths, CSV or extra reports are generated. Live
sources/services and original runs are untouched. Canonical details remain in
the single maintained PROTOCOL.md/OUTPUT_CONTRACT.md, not a copied protocol here.


### Explicit Phase 3 usage

Run ONLY when you choose to evaluate a completed source. Replace placeholders;
choose --portfolio/--configuration/--submodel/--instrument if scope is ambiguous:

```powershell
python model_development/phase_3/evaluate.py `
  --task kelly monte-carlo --run <completed-run-folder> `
  --portfolio <source-portfolio-id> --output <new-evaluation-folder>
```

For fixed-risk Phase 1, Kelly needs no invented capital. Monte Carlo additionally
requires explicit --risk-fraction <f> --initial-equity <capital> --max-leverage <cap>
to declare a conditional ledger-sizing study. It does not execute the strategy.
Defaults are 2000 Kelly draws, 5000 MC draws, 7-day blocks, seed 20260927; override
with --resamples/--simulations/--block-days/--seed. They are legacy reference
settings, not calibrated per-model choices. Zero samples/no observed loss retain
explicit unavailable-estimate statuses. Inspection/selection rules still apply.


## Phase 3 jump --run precedence — 2026-11-01

Statistical jump now also accepts the explicit selected Phase 1/2 --run contract
through model_development/phase_3/evaluate.py, reusing the existing calculator
and shared run_input reader. Require explicit market/provenance/window/inspection
arguments and exactly one account; reject bounds dropping selected closed trades.
Retain unknowns, source fields, gross/net/fees, hashes and censoring. Phase 2 chart
uses original capital/monetary P&L at observed quantities; Phase 1 remains R.
Default solver/formulas and legacy CLI/render stay compatible. No automatic
phase transition, sizing application, entry filter or live migration. Other
non-Kelly/MC/jump tasks still use native inputs. Canonical PROTOCOL.md and
OUTPUT_CONTRACT.md record the output/coverage/cache contracts and evidence limits.

### Statistical-jump diagnosis from an explicitly selected run

Run independently when you choose this diagnosis. Replace placeholders and use
additional --model-id/--configuration/--submodel selectors if ambiguous:

```powershell
python model_development/phase_3/evaluate.py `
  --task statistical-jump --run <completed-run-folder> `
  --portfolio <source-portfolio-id> `
  --market <measured-daily-OHLCV.parquet> `
  --instrument <source-symbol> --venue <declared-venue> --product <declared-product> `
  --start <YYYY-MM-DD> --end <YYYY-MM-DD> --inspection-status development `
  --output <new-diagnosis-folder>
```

Alternatively replace --market with --input <chronological-raw-parquets> and an
optional --cache-dir, or --candle-cache <validated-cache-folder>. History must
meet the fixed classifier warmup/training contract; start/end must retain every
selected closed source outcome. No automatic scope reduction. Outputs and chart
bases are defined in the newest canonical PROTOCOL.md/OUTPUT_CONTRACT.md record.
Use --task statistical-jump -- render --run <saved-diagnosis> --output <new-folder>
for the existing render-only operation, without fitting or changing originals.

Local live consolidation now has six isolated candidate scripts; see the separate
live_trading/README.md and release.json. This changes no research runner or phase
transition and does not activate/migrate operational services or state.


## VWAP research migration precedence — 2026-11-01

Maintained VWAP home: model_development/models/mean_reversion_by_vwap/ (one script,
one full-history strategy MD). Main-project shared research APIs are
indicator.research.vwap, indicator.research.cvd and trading_core.candles. Global
Phase 1/2 default shared-core to the main project; explicit old cores still work.
Legacy indicator.vwap is unchanged. Old VWAP model/grid/tests and executor sources
stay frozen for exact-hash historical resume, not active parallel maintenance.
Do not replace them with a shim or silently permit cross-path/source resume.
See canonical PROTOCOL.md's newest migration record. Other indicators/models are
not automatically relocated. No deployment, artifact deletion or new checkpoint.


## Pre-NY/indicator migration precedence — 2026-11-01

Maintained Pre-NY research family: model_development/models/
trend_following_preny_profile_15m/strategy.py, one decision/calibration script and
one full-history RULES.md. The global Phase 2 runner calls prepare_backtest; HYPE
C1 remains the supported frozen configuration. No new Phase 1 or ETH/BNB adapter.
Shared standard APIs now use indicator.research.moving_average / atr / adx /
order_flow; trend is optional composition. Keep Pre-NY's legacy ATR unchanged.
Old models/runtime/indicators/tests remain frozen compatibility evidence; all live
sources and artifacts stay intact. See newest canonical PROTOCOL.md record.
