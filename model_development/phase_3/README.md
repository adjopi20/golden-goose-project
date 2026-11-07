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

# Reusable Kelly and sizing stress tools

Two separate CLIs, **outside models**, operating on existing backtest ledgers:

- `python model_development/phase_3/evaluate.py --task kelly --`: empirical multi-outcome Kelly and block-bootstrap uncertainty.
- `python model_development/phase_3/evaluate.py --task monte-carlo --`: exit x risk-size x stress x block-length comparison.

This is not a new backtest engine. Neither CLI reads raw aggTrades, reconstructs signals, changes execution rules, or combines assets into an account. Inputs from any model are accepted when they follow the documented ledger contract. Only NumPy and Python standard library are required.

## Current study

`configs/three_shortlists_four_exits.json` selects exactly four existing ledgers for each of ETH C2 union, BNB initiative and HYPE C1. No EMA or ADX filter. TP1 at 1R/2R, allocation 10%/20%; existing stop/time-exit fills unchanged. Each ledger is an independent account.

Risk grid: 0.1%, 0.25%, 0.5%, 0.75%, 1% of current equity. Max leverage 5. Circular calendar block lengths 7 and 14 days. Same sampled days across exit variants/scenarios/sizing when horizons match. Includes no-trade days. Entry session dates carry complete trade returns; this is closed-trade sequence research, not intratrade cash/mark-to-market simulation.

7 stresses x 5 sizes x 2 block lengths x 12 ledgers = **840 summary combinations**, with 5,000 paths each by default. Chunking bounds temporary array memory. No fixed running-time guarantee; progress prints after each dataset/scenario. Kelly separately estimates 12 baseline references with 2,000 resamples. Neither job has been run as part of installation.

### Scenarios (explicit hypothetical severities, not calibrated predictions)

| Name | Transformation |
|---|---|
| baseline | Existing net outcomes |
| fees_1p5x | Actual recorded fees multiplied by 1.5 |
| extra_cost_2bps | Additional 2 bps total roundtrip on initial entry notional; a cost allowance, not fill simulation |
| stop_gap_0p25R | Extra 0.25 initial R on the fraction still open when a stop triggered |
| winner_gross_75pct | Reduce gross PnL of originally net-positive trades by 25%; retain fees |
| miss_largest_winner | Set largest net winner's gross PnL to zero, still pay original fees |
| combined | 1.5x fees + 2 bps extra cost + stop-gap shock + winner haircut (does not additionally remove the largest winner) |

Winner-based shocks intentionally use outcomes as counterfactual **stress diagnostics**, never as causal entry filters. Stops are detected from stop_trigger_timestamp_ms or an exit_reason containing `stop`. Partial trades need remaining_exit_quantity to avoid charging the shock on already closed size. Baseline historical slippage is already embedded in ledger fills; extra_cost adds a distinct adverse allowance, not a measured new slippage estimate.

There is no fitted stress severity, threshold search, automatic chosen sizing, or hard 10%/20% DD rule. Those thresholds are reporting columns only. Set your acceptable risk separately before selecting a result.

## Commands (PowerShell, repository root)

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH="."

# Optional quick unit checks (no real-data study):
.\.venv\Scripts\python.exe -m unittest discover -s model_development/phase_3/tests -v
if ($LASTEXITCODE -ne 0) { throw "Unit checks failed." }

.\.venv\Scripts\python.exe model_development/phase_3/evaluate.py --task kelly -- `
  --config model_development/phase_3/configs/three_shortlists_four_exits.json `
  --output-dir reports/risk_research/three_shortlists_four_exits_v01/kelly `
  --resamples 2000 --block-days 7 --seed 20260927
if ($LASTEXITCODE -ne 0) { throw "Kelly failed; do not analyze incomplete output." }

.\.venv\Scripts\python.exe model_development/phase_3/evaluate.py --task monte-carlo -- `
  --config model_development/phase_3/configs/three_shortlists_four_exits.json `
  --output-dir reports/risk_research/three_shortlists_four_exits_v01/stress `
  --simulations 5000 --seed 20260927
if ($LASTEXITCODE -ne 0) { throw "Stress failed; do not analyze incomplete output." }
```

Use a NEW output directory for a rerun; nonempty output is rejected so incomplete/stale results cannot be mixed. `complete.json` is written last. A failed partial output should not be analyzed. Config and ledger/code SHA256 hashes are recorded.

### Outputs

Kelly: `kelly.csv` with empirical full/quarter reference, P5/P50/P95, zero-estimate frequency and sensitivity to removing the largest winner. `manifest.json`, `complete.json`.

Stress: `stress_summary.csv` (MC equity quantiles, DD median/P95/P99, finish-below-start, insolvency and DD-threshold frequencies); `historical_sizing.csv` (WR, net-R expectancy, cash PF, equity, DD); `historical_equity.csv`; `manifest.json`; `complete.json`. No plots automatically generated in this minimal tool; these tables can feed the existing visualization workflow.

## Mathematics and caveats

Kelly solves max mean(log(1+f*R)), where f = initial price-stop risk/equity, not notional or margin. Negative mean R gives zero. With no observed losses, no finite unconstrained estimate is reported. Bootstrap uses calendar clusters; zero-trade resamples are omitted and counted. The finite original outcome support bounds the admissible risk domain. Kelly here is an uncapped theoretical reference, **not the optimum of the execution engine with leverage limits**, and quarter Kelly is merely a reference, not advice.

Stress sizing respects the leverage cap: account_return_i = min(risk_fraction, max_leverage * stop_distance_pct_i) * stressed_R_i. Fees scale proportionally. Insolvency is absorbing; simulations never resurrect an account after losing all equity. Realized losses can exceed planned risk. Historical equity consolidates partial PnL at final trade completion.

Monte Carlo frequencies are conditional on this finite inspected sample, not future survival probabilities. Research selection bias, unseen extremes, changing edge, funding, liquidation mechanics, size-dependent impact, lot rounding and mark-to-market DD are not modeled. No combined-account/correlation research is performed. Compare asset results only with awareness of different sample horizons.

## Reuse with another model

Copy/edit a config; do not copy the engine. `path_root` is relative to the CONFIG directory. A dataset can point directly at any compatible ledger:

```json
{
  "schema_version": 1,
  "path_root": "../../..",
  "initial_equity": 1000,
  "max_leverage": 5,
  "risk_fractions": [0.001, 0.0025, 0.005],
  "block_days": [7, 14],
  "scenarios": [{"name": "baseline"}, {"name": "fees_1p5x", "fee_multiplier": 1.5}],
  "datasets": [{
    "name": "my_model_my_exit",
    "ledger": "path/to/trades.jsonl",
    "start_date": "2025-01-01",
    "end_date": "2025-12-31"
  }]
}
```

Alternatively use `comparison_csv`, `ledger_root`, `where`, `expected_count` and optional `completion_marker` as in the provided preset. `where` values accept single values or lists; numeric comparisons allow serialization tolerance. Different independent input series must have distinct names.

### Ledger contract

JSONL required fields: sample_id, session_day (YYYY-MM-DD), entry_timestamp_ms, exit_timestamp_ms, quantity, entry_price, stop, actual_initial_risk, equity_before_entry, pnl, fees, r. Price and quantity describe a linear quote-margined product; risk must equal quantity*abs(entry-stop), r must equal net pnl/risk. If gross_pnl_before_costs exists, gross-fees must match net. Optional remaining_exit_quantity, tp1_filled, stop_trigger_timestamp_ms and exit_reason support stop stresses. Multiple trades per session, overlapping positions, duplicate IDs, missing/nonfinite values, invalid risk and out-of-window sessions fail loudly. Broader strategies require an explicit adapter, not silent aggregation.

Dates refer to eligible ENTRY sessions; final next-day exit may fall after end_date. Original fills remain intact. Full chronological series is required; the tool does not fabricate missing trades or data coverage.


## Selected-run trade-order shuffle

Use the existing public runner with `--task monte-carlo --run <completed-run-folder>
--output <new-evaluation-folder> --mc-method trade-shuffle --simulations 500 --seed 20261106`.
The default method remains `calendar-bootstrap`.

Trade shuffle permutes all selected closed trades without replacement. Each trade,
including each large winner, occurs exactly once in each path. By default it uses
net P&L / equity before entry, preserving the observed net return and recorded
sizing/leverage effect together. Fees already belong to these returns and are not
charged again. Explicit conditional sizing overrides retain their existing semantics.
This calculates equity geometrically at every completed-trade boundary and keeps
zero equity absorbing. No raw market replay, entry opportunity regeneration or
calendar-block resampling occurs in this mode.

The existing evaluation `run.json` stores all 500 equity paths, source trade IDs,
return factors, mean/median/P5/P95 equity bands and per-path drawdown, minimum equity,
share of trade boundaries below starting capital and average equity over normalized
trade progress. The latter is the trapezoidal mean over completion fractions 0 to 1,
so runs with different trade counts can be inspected on the same progress axis.
No additional path files or charts are generated. There is no preset drawdown
pass/fail threshold or automatic winner selection.

With fixed returns and the same trades in every permutation, terminal equity is
order invariant apart from rounding. Compare intermediate path dispersion,
underwater exposure and drawdown distributions to study sequence stability. A low
fraction of paths below starting capital is descriptive, not a future probability
of success. This study preserves the sampled tails and tests their arrival order;
it does not test whether those tails recur in future markets.

Selected-run diagnostics also accept `--scenario-config <json> --scenario <name>`.
The JSON uses the same `scenarios` fields documented above. Source Parquet rows
retain exit reason, stop trigger and remaining partial quantity; stop-gap shocks
require that evidence and never charge already exited size. The selected scenario
is applied to net Monte Carlo outcomes and net Kelly; source gross Kelly remains
an explicitly unstressed reference. Scenario config hashes are checked before
completion. Historical artifact bytes are never rewritten. The three requested
burst candidates and existing stress severities are specified in
`configs/burst_three_candidates_20261107.json`.
