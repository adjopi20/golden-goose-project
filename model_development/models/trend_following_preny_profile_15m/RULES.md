# Pre-NY profile research — maintained family

## HYPE Phase 2 direction comparison

Use the existing global Phase 2 runner with `--model-config '{"comparison":"direction"}'`.
One preparation and one shared replay produce four independent geometric accounts:
`baseline_long_only`, `baseline_both`, `c1_long_only`, and `c1_both`.
This comparison uses the reference table's no-TP1 exit: initial Pre-NY POC stop
or next-day 08:59 New York. The default C1 command below still uses its unchanged TP1.
C1 calibrates on both directions before applying the long-only restriction;
unknown calibration remains NO_TRADE for both C1 accounts. Baseline does not
require C1 feature warmup. This is not a change to paper or live settings.

Keep the standard artifacts: `run.json`, `trades.parquet`, `equity_curve.png`.
`run.json` lists per-account long/short trade counts and net-R expectancy
overall and in consecutive three-calendar-month blocks by NY entry session.
Empty groups have null expectancy. The curves use independent closed equity.
For inspected history with forward raw-ID gaps, explicitly pass
`--inspection-status development --id-policy record`. Continue on observed
prints without rejecting sessions solely for gaps; gap details are in
`run.json` under `cache.discontinuities`. No missing prints are reconstructed.
Strict raw-ID continuity remains the default.

To compare on a holdout, pass `oos_start`, `oos_end` (inclusive NY entry
dates) and `calibration_run` (completed pre-OOS direction run.json) with
`comparison=direction` in the same model-config. All four accounts start flat.
The last saved C1 fold is pinned for the entire holdout without refitting.
The existing chart and ledger are unchanged; run.json adds monthly directional
expectancy and final-session exit coverage. Insufficient exit data is censored.

For the inspected-window price-only challenger, use `comparison=price_only`
with the same two NY date bounds, but no calibration receipt. The baseline
price requirements and execution are unchanged; delta vetoes and C1 are off.
The runner produces both and long-only accounts. This is development evidence.

## HYPE research readiness — 2026-11-02

Current scope: prepare research only. No market backtest, fixed paper-calibration
renewal, paper activation or deployment is performed. One maintained strategy.py
continues to own decisions and causal C1 calibration; the global Phase 2 runner
owns replay, accounting and output. Do not create a HYPE-specific runner.

Use the existing HYPEUSDT C1 contract: 0.5% current closed equity, initial 1,000
USDT, 4 bps per filled side, notional cap 5x, TP1 10% at 1R, then original POC
stop or next-day 08:59 New York. No new filter, sizing or exit rule is selected.
Research calibration remains rolling 180 calendar days, >=25 usable candidates,
one-day embargo and 60-day application blocks. Its causal thresholds come from
historical pre-entry features; it does not require the expired fixed October
paper calibration file. Unknown/warmup calibration remains NO_TRADE.

Prepared command, to execute only when the user requests the actual backtest:

```powershell
Set-Location 'C:\Users\adjop\OneDrive\Documents\golden-goose-project'
.\.venv\Scripts\python.exe -B model_development/phase_2/backtest.py `
  --model model_development/models/trend_following_preny_profile_15m/strategy.py `
  --input storage/hypeusdt/HYPEUSDT-aggTrades-2025-05-31_to_2026-08-31.parquet `
  --shared-core . `
  --venue binance --product linear_quote_margined `
  --quantity-unit HYPE --pnl-unit USDT `
  --initial-equity 1000 --risk-fraction 0.005 --fee-bps 4 --max-leverage 5 `
  --iteration ITER-001 --inspection-status development `
  --output model_development/models/trend_following_preny_profile_15m/runs/hype_c1_research_20261102
```

The input file is present; no raw rows were read for this readiness check. Use a
new output folder if that name has already been used. Main-project closed-candle
clock and shared research paper adapter are used; no external executor checkout is
required by this command. Expected artifacts remain trades.parquet, run.json and
equity_curve.png. This old inspected sample is development history, not fresh OOS.
Existing historical rules/iteration records follow unchanged. No new iteration,
checkpoint, measured return or model validation is inferred from preparation.


Current model home: model_development/models/trend_following_preny_profile_15m/.
One strategy.py owns the family's decisions and calibration. Use the global
Phase 2 backtest.py with --model; the maintained script has no runner CLI.
The HYPE C1 configuration, legacy hourly ATR and all trading rules remain fixed.
Global Phase 1 and ETH/BNB Phase 2 adapters are not implemented by relocation.

Maintenance, 2026-11-01: source/calendar/feature/selector/calibration logic was
consolidated without changing outcomes in synthetic parity. No new iteration or
checkpoint. Original sources/tests stay frozen for historical callers. The full
previous rules/iteration/checkpoint/evidence text follows VERBATIM. Old paths and
status statements below describe that history; this paragraph takes precedence
for the maintained home and supported interface.

---

# Trend Following Pre-NY Profile 15m

## Current selected HYPE Phase 2 configuration — 2026-10-31

Architecture maintenance note, 2026-10-31: the command `strategy.py --backtest`
recorded below is a TRANSITIONAL compatibility adapter, not the agreed global
Phase 2 front door. PROTOCOL.md v6 requires the existing global
`backtest_engine/replay_aggtrades.py` to load model/data/configuration grids
dynamically; that --model CLI is not implemented yet. Move orchestration/output
ownership there through explicit parity work, leaving this model's decisions and
C1 calibration here. This note changes no trading rule/iteration/checkpoint,
runtime code, prior result or live/paper service. Do not describe the last HYPE
adapter as completion of the global infrastructure.

The baseline description below remains the historical unfiltered model. For the
user-selected HYPEUSDT C1 model, the current research/backtest configuration is:
baseline first supported candidate, then C1 delta x weak-result exclusion; frozen
Pre-NY POC stop; TP1 10% at 1R; remaining 90% at that same stop or next-day 08:59
New York. No trail, breakeven, replacement entry, EMA or ADX filter/sizing. Risk
is fixed at 0.5% of current closed equity, initial equity 1,000 USDT, notional cap
5x equity, fee 4 bps per filled side. The risk is price-to-stop risk, not notional.

### ITER-001 — full-history geometric C1 adapter, 2026-10-31

Parent: legacy research checkpoint TF-PRENY-PAPER-001 (2026-09-27); historical
documents/results are preserved, not renumbered as new checkpoints. No new
checkpoint is selected by this maintenance/research task. Existing deployment
contracts and production/paper workers are not changed.

User decision: **NO TRADE until causal C1 calibration is available**, including
unavailable C1 features. This explicitly replaces the legacy unknown-feature
baseline fallback in this research run only. C1 keeps its historical exception to
the general selection schedule: rolling 180 calendar days, minimum 25 usable
baseline execution-eligible candidates, one-day embargo, 60-day application
blocks. First origin is the first usable historical baseline candidate; after
180 days, test starts one day later. Refit at each scheduled 60-day step even if a
previous fold lacked calibration. Thresholds are medians of past candidate
features only; no trade outcome is used. Reject high directional imbalance
greater than/equal to its training median together with directional result/ATR
strictly below its training median. A rejected first candidate has no replacement.

Features retain the frozen last-up-to-four completed 15m evaluation bars from
09:00, directional delta imbalance and signed close-minus-first-open divided by
the latest completed hourly ATR14. Reuse legacy `trading_core.indicators` ATR
(`adjust=False`, min_periods=14), not the newer Wilder SMA seed: changing that
formula would be another iteration. Clock/coverage gating explicitly uses the
executor CandleContext, base 15m for the model and 1h for ATR availability. The
measured 1m source/cache supports those bars. Missing evaluation bars/whole hours
or unavailable ATR block the candidate; no source prices or sides are invented.
Synthetic baseline/parity tests do not migrate every legacy observer or certify
the model's live decision parity.

Input is the entire specified raw HYPEUSDT aggTrade Parquet, not the previous
March-August sizing cohort. Start is the first available complete 01:00-09:00
profile, end is the last supplied NY date. Only fully closed decision bars enter.
Final-day candidates remain eligible: trades actually closed before source end
are counted, unresolved positions are censored without forced terminal closure,
including any partial cashflows from an unresolved logical trade. Existing
positions block subsequent qualifying signals and those blocks stay in the master.

One command uses this maintained `strategy.py --backtest` and the shared
`backtest_engine.replay_aggtrades.run_phase2`; it does not create another simulator.
Strict shared raw validation/cache lives outside runs; discontinuous IDs fail,
never automatically retry with exploratory record policy. Exact profiles reuse
the existing raw preparation; exact fills reuse the shared next-print engine.
Initial build needs a raw validation/preparation pass plus execution replay;
subsequent runs can reuse matching validated minute data but still replay raw fills.

Output: one new run folder with `trades.parquet`, `run.json`, `equity_curve.png`.
Metadata contains code/source/config identities, exact calibration windows and
thresholds, exclusions, gross/net statistics, capital, fee and censoring policy.
Curve is net closed-position equity plus drawdown, not floating/MTM equity.
Gross comparison uses the same actual quantities as the net-funded portfolio;
it is not a separate no-fee compounded simulation. Funding, orderbook spread,
additional slippage, impact, liquidation and exchange lot rounding are unmodeled.
The raw next-print price is an execution proxy, not a guaranteed venue fill.

Status: implementation and synthetic checks only; full market backtest NOT RUN.
This history has been inspected/used in selection; no new untouched OOS is claimed.
The candidate is fixed for this Phase 2 run, with no reward/threshold search.
Use a fresh output name for every attempt; failed metadata is retained.

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -B -m models.trend_following_preny_profile_15m.strategy `
  --backtest `
  --input "storage/hypeusdt/HYPEUSDT-aggTrades-2025-05-31_to_2026-08-31.parquet" `
  --shared-core "C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor" `
  --initial-equity 1000 --risk-fraction 0.005 --fee-bps 4 --max-leverage 5 `
  --output-dir "models/trend_following_preny_profile_15m/runs/hypeusdt_c1_full_geometric_20261031_001"
if ($LASTEXITCODE -ne 0) { throw "Phase 2 failed; do not analyze partial artifacts." }
```

The CLI auto-detects source bounds and creates the three requested artifacts.
Its configuration is research-only and does not authorize deployment changes.

Checkpoint: **TF-PRENY-15M-001**, 2026-09-26. Research baseline, not a live-deployment approval.

## Hypothesis and scope

Persistent progress outside the completed Pre-NY value area, supported by directional aggressive flow and price response, may identify intraday expansion. This is a falsifiable hypothesis, not an established edge.

- Four separate asset experiments: BTCUSDC, ETHUSDC, BNBUSDC, HYPEUSDT.
- Only **Pre-NY volume profile**, 01:00 inclusive to 09:00 exclusive, America/New_York (DST-aware).
- Price candles and delta observations: **completed 15-minute bars**. No waiting for 1-hour closes.
- Previous-day/24h profile is excluded from the active model and comparisons.
- Historical runs are preserved; old two-profile results are not relabelled as new runs.

## Entry rules (unchanged Pre-NY baseline)

1. Observe 09:00-12:00 NY. Entry fill at exactly 12:00 is allowed; after it is not.
2. Long above VAH; short below VAL. A wick alone does not confirm acceptance.
3. Default: two consecutive completed 15-minute closes outside the same edge. Earliest candidate is 09:30. The existing three-bar option remains a research parameter, not the default.
4. Directional cumulative delta of the outside-value sequence and latest-bar delta must be positive; latest directional candle body and close-to-close progress must also be positive.
5. Three successively declining positive directional deltas **together with** declining positive price progress cause WAIT. Declining delta alone is not a veto.
6. Re-entry into value, side change, or a missing 15-minute bar resets the sequence. Maximum one entry candidate per asset/session.
7. No eligible breakout by 12:00 means no trade. The new delta-result challenger is **not active** in baseline entry.

Profile parameters remain 50 price bins and 70% value area, without new optimization. Profile is frozen at 09:00 before entry observation.

## Stop, exit, sizing and execution

- Initial hard stop: completed Pre-NY POC; must be below actual long fill / above actual short fill.
- No fixed TP, partial exit or trailing stop in this baseline.
- Time-exit trigger: next-day 08:59 NY. Raw replay fills at the first available eligible print; a sparse feed can fill later than trigger time.
- Reuse `backtest_engine.replay_aggtrades`; do not create a model-specific backtest engine.
- Entry uses the first eligible aggTrade, maximum delay 300 seconds and never after 12:00. Stop fills on the print after its trigger.
- Comparison settings unchanged: initial equity 1000, risk fraction 0.005, maximum leverage 5, fee 4 bps per fill. Assets remain separate portfolios.
- AggTrade fill is a proxy, not an order-book execution model. Funding/market impact are absent. Reported equity drawdown is realized-close based, not intratrade mark-to-market.
- No new daily-loss circuit breaker, risk adjustment or regime filter is introduced by this checkpoint.

## Interfaces and historical compatibility

Active module: `models.trend_following_preny_profile_15m.strategy`.
Optional `--profile-window pre_ny` is now the only accepted profile choice and the default.
Signal schema remains compatible with the shared replay engine; strategy identifier is `trend_following_preny_profile_15m_v1`.
Prepared historical profiles containing both windows can be read: only Pre-NY is evaluated. Use a fresh output directory, not an old two-profile output.

`audit_opportunity.py` remains a non-trading research utility. Its existing EMA/ADX/ATR **hourly context** fields and schema are preserved to reproduce the historical study. These do not decide entry, and do not mean the active candle is 1h. Changing those research denominators to 15m would be a separate versioned audit, not part of this rename. New strategy preparation no longer generates 1h candles.

The folder (including runs) was renamed from `models/daily_profile_1h`. Historical manifest paths remain provenance strings; point commands at the new location. Old module commands must use the new module name. `RULES_LEGACY_V2.md` is historical documentation only.

## Research decision

### Unified observation context (2026-09-26; not a trading filter)

`evaluate_plugs --context-only` combines the original six-plug features, existing
signal-time audit context, historical research gates, and new **15m EMA200,
SMA20 (MA20), ADX14 and directional DI** into one candidate ledger across assets.
It does not refit thresholds or change entries/stops/exits. See
`OBSERVATION_CONTEXT.md` for the command, timing and coverage contract.
The old hourly ATR and EMA/ADX fields remain explicitly separate and unchanged.
No order bubbles/P99, ADX cutoff, MA filter, or new strategy rule is introduced.

See `RESEARCH_FINDINGS_2026-09-26.md`. Only `delta_without_result` advances as a candidate challenger; it is not frozen as a trading filter. Next research: baseline versus this challenger using shared-engine replay, fixed entry/stop/exit/cost/sizing, plus equal-frequency random-removal control. No stacking six gates, no asset-specific winning-rule selection, no replacement entry after rejection in the first comparison.

Promotion requires improved net quality and risk across periods/assets while reporting sacrificed winners and tails. No new numeric success threshold is selected from these results. Chronological reuse of already-inspected data is research, not untouched OOS.


## Maintenance — global Phase 2 adapter, 2026-11-01

Candidate/feature/calibration preparation now lives in prepare_backtest(request)
in the SAME strategy.py; the old --backtest CLI delegates to the global runner.
No strategy iteration or checkpoint is created. Existing candidate/calibration
functions and runtime source remain unchanged. Synthetic integration passed;
this does not establish real-market performance or full pre-migration parity.

New global command shape (full-data execution remains operator-run):

```powershell
python model_development/phase_2/backtest.py `
  --model models/trend_following_preny_profile_15m/strategy.py `
  --input <HYPEUSDT-aggTrades-source.parquet> `
  --shared-core <executor-checkout> `
  --venue binance --product linear_quote_margined `
  --quantity-unit HYPE --pnl-unit USDT `
  --iteration ITER-001 --inspection-status development `
  --initial-equity 1000 --risk-fraction 0.005 --fee-bps 4 --max-leverage 5 `
  --output <new-run-folder>
```

Declare venue/product provenance truthfully; filenames do not certify the venue.
Full-source bounds/legacy inclusive session dates and original calibration apply.
No new asset/rule/grid override, changed exit or new unseen OOS is implied.
