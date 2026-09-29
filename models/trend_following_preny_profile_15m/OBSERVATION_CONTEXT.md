# Unified observation context

Additional observation, not a new strategy/backtest. The existing six plugs remain
unchanged: VWAP persistence, value-edge acceptance, volume versus result, delta
versus result, ATR activation, and fee/stop burden.

## New trend context

- EMA200 and **SMA20**, not EMA20, calculated on continuous all-hours 15m closes.
  EMA200 spans 50 hours of bars; it is not a 200-day macro indicator.
- ADX14 with Wilder smoothing measures strength, not direction. +DI/-DI provide
  directional context. No 20/25 ADX threshold is selected.
- Price/MA gaps and SMA20/EMA200 separation in percent; MA percent changes and
  ADX-point change over four completed bars (one hour).
- Descriptive MA structure: close > SMA20 > EMA200, inverse, mixed, or unknown;
  alignment relative to the existing trade direction. No entry gating.
- SMA initialization for EMA/Wilder; unavailable until warmup. Entire missing
  15m bars reset warmup; partial bars use observed OHLC and expose coverage.
- Features use only bar ends at or before the original signal timestamp. The
  current unfinished bar and future data never participate. No forward filling.

## One dataset, clear namespaces

`candidate_context.jsonl` is the primary ledger; `candidate_context.csv` is its
flattened copy for inspection. `manifest.json` documents sources and coverage.
One row per executed baseline Pre-NY candidate, including candidates rejected
by challenger research and candidates in the initial training period. This is
not a no-trade-session/opportunity ledger; the original opportunity audit remains
the source for that question.

Namespaces:

- `features`: original numeric inputs for the six plugs, unchanged.
- `audit_context`: existing signal snapshot, including original **1h** EMA/ADX/ATR,
  1h/3h rolling volume/delta, volatility and VWAP. No denominator migration.
- `trend_15m`: the new indicators with explicit timeframe, status and coverage.
- `profile`: frozen original profile; top-level fields identify trade and fill.
- `research_gates`: existing study decisions/fold thresholds, copied not refitted.
  An absent gate means no historical test-fold gate, not a rejected candidate.
- `outcomes`: original audit net R and minute-candle stop-first MFE bounds.
  These are not outcomes from the newer fixed-TP sweep. Do not mix exit policies.

`fee_stop_burden` uses actual entry-fill geometry and is an execution diagnostic,
not strictly pre-fill information. Keep it distinct from causal signal features.
Do not use `outcomes` or any future path field to construct entry features.
The exporter checks existing study feature values/net R before merging, and will
fail rather than silently combine incompatible run versions.

## Run all four assets (no observation or raw-trade replay rerun)

Run from the repository root. Existing all-hours minute caches are required;
the prepared 09:00-12:00 bars alone are insufficient for EMA200.

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH="."

.\.venv\Scripts\python.exe -m models.trend_following_preny_profile_15m.evaluate_plugs `
  --context-only `
  --study-manifest "models/trend_following_preny_profile_15m/runs/four_asset_plug_study_v01/manifest.json" `
  --context-cache BTCUSDC "tmp/cache/btcusdc/btcusdc_pre_ny_submodels_full_24h_orderflow_2024-01-05_to_2026-07-31/candles_1m.parquet" `
  --context-cache BTCUSDC "tmp/cache/btcusdc/btcusdc_pre_ny_oos_2026-08-01_to_2026-08-31_orderflow_v3/candles_1m.parquet" `
  --context-cache ETHUSDC "tmp/cache/ethusdc/ethusdc_pre_ny_submodels_full_24h_orderflow_2024-01-05_to_2026-07-31/candles_1m.parquet" `
  --context-cache BNBUSDC "tmp/cache/bnbusdc/bnbusdc_cp005_2025-09-01_to_2026-08-31_orderflow_v3/candles_1m.parquet" `
  --context-cache HYPEUSDT "tmp/cache/hypeusdt/hype_daily_profile_2025-05-31_to_2026-08-31/candles_1m.parquet" `
  --output-dir "models/trend_following_preny_profile_15m/runs/four_asset_observation_context_v01"

if ($LASTEXITCODE -ne 0) { throw "Context export failed; do not analyze partial output." }
```

The two BTC caches are historical and August segments, not separate strategies.
Overlapping minutes must agree. Assets are tagged separately; this combined
research ledger is not a combined equity portfolio. Use a fresh output directory.

## Interpretation

This adds measurements, not evidence of a new edge. Evaluate baseline/challenger
within the same trend context, report group size and discarded tails, and retain
calendar-aware validation. Already-inspected periods are not new untouched OOS.
No asset-specific optimized cutoffs or stacked filters are approved by this step.

## Challenger 2 — stage 1 stateful effort/result annotations

This stage does NOT change any entry, stop, exit, position size or challenger-1
decision. It is not a backtest of a new entry policy. No MA, EMA, ADX, bubble/P99
or trade-size information participates in the annotation engine. Existing trend
fields remain in the consolidated output, for later independent experiments.

Use the same entrypoint, not another backtest script:

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH="."

.\.venv\Scripts\python.exe -m models.trend_following_preny_profile_15m.evaluate_plugs `
  --effort-result-context "models/trend_following_preny_profile_15m/runs/four_asset_observation_context_v01" `
  --output-dir "models/trend_following_preny_profile_15m/runs/four_asset_stateful_effort_result_v01"

if ($LASTEXITCODE -ne 0) { throw "Annotation failed; do not analyze partial output." }
```

This handles all four assets from the existing manifest. Reads prepared 15m bars,
frozen profiles and minute caches; no raw aggTrade scan, no strategy regeneration.
Use a fresh output path. Existing runs are never overwritten.

### Fixed research definitions (before inspecting this study's outcomes)

- Reference: same NY quarter-hour slot, median of up to 20 earlier observed
  sessions; at least 10 needed. Current/future sessions excluded. Separate buy,
  sell, total volume and absolute-body medians. These are research definitions,
  not optimized settings or promises of predictive power.
- ATR14 1h normalization uses the latest completed clock-hour available at the
  **opening** of each 15m candle; stale hours are unavailable. No denominator is
  taken from an unfinished candle. Historical six-plug fields stay unchanged.
- VWAP is the volume-weighted typical-price approximation from 09:00, not exact
  trade VWAP. CVD is cumulative buy minus sell volume from 09:00.
- Each completed candle is evaluated in BOTH directions. Long delta = buy-sell;
  short delta = sell-buy. Directional progress = current close minus previous
  close, signed for direction (first candle uses its open).
- Good result: directional body and progress positive, close in the favorable
  half of the range, body at least the same-clock historical median.
- Weak result: adverse/zero body or progress, close in the unfavorable half, OR
  small body versus median with rejection wick at least as large as the body.
- Initiative: directional aggression at least its earlier median, positive
  directional delta, and good result.
- Possible absorption: the same effort conditions but weak result. This does
  not prove resting liquidity, a trapped participant, or eventual reversal.
- Path of least resistance: total volume below its earlier median, positive
  directional delta/CVD increment, and good result. Low volume alone is not bad.
- Participation fading: three consecutive observations with decreasing positive
  directional delta, decreasing directional aggressive volume, AND decreasing
  directional price progress. It can coexist with other labels; not four gates.
- CVD divergence: a new favorable price extreme versus recent evidence, less
  directional cumulative delta than at that earlier extreme, AND weak result.
  Divergence without deteriorating result is not marked as failure.

### State and memory

Per-direction evidence retains the last four observed candles (one hour), not an
ever-growing score. Counts, directional delta, recent-extreme retracement, wick,
body, close position, edge/VWAP distance and support streak are exported.
Re-entry into value or an opposite-edge change resets the relevant outside-value
episode memory; new outside acceptance starts a new episode. Session CVD remains
continuous. Missing full 15m bars reset local memory and mark the remainder of
the session's cumulative coverage incomplete; no prices/volume are fabricated.

Support observation = outside the directional edge, favorable VWAP location,
initiative/path, without absorption/fading/weak-result divergence, and complete
session coverage. Two consecutive support observations get the descriptive label
`SUPPORT_PERSISTING`; one is `BUILDING_SUPPORT`. Other states are `WAIT`,
`REBALANCING`, `INSIDE_VALUE`, `UNKNOWN_REFERENCE` and `UNKNOWN_DATA`.
These labels DO NOT approve/reject baseline entries in stage 1. Missing evidence
is unknown, not a reason to silently delete a candidate. ATR and coverage values
remain available for analysis even when no categorical label is assigned.

### Output and leakage boundary

- `candidate_context.jsonl/csv`: original integrated records preserved; only
  `effort_result_pre_entry` is appended at the existing signal timestamp.
- `session_timeline.jsonl`: all 12 scheduled closes, 09:15–12:00, including
  no-entry sessions. Both direction interpretations. `audit_phase` separates
  `PRE_ENTRY`, `POST_ENTRY`, `NO_BASELINE_ENTRY`. This phase is retrospective audit
  metadata, NEVER an input feature or prediction of whether entry will occur.
- `annotation_summary.csv`: descriptive counts/WR/net R/PF(R)/MFE% by label at
  signal time; all candidates and existing test-fold cohorts separately. Labels
  may overlap; do not sum groups. Outcomes are the original baseline time-exit
  audit, NOT the fixed-reward sweep and NOT performance of a new C2 strategy.
- `manifest.json`: parameters, source lineage and counts. Prior-inspected data
  remains exploratory, even when called an existing test fold.

No post-entry candle is used by the pre-entry snapshot or annotation summary.
Do not restrict analysis to episodes later confirmed, winners, or days with entry.
The timeline provides no-entry sessions, but no hypothetical return is invented
for those sessions. WAIT/replacement-entry replay is a separate later experiment.
