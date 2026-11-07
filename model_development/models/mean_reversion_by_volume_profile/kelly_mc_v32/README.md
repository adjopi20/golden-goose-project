# Kelly + Monte Carlo sizing analysis — v32

This is an analysis of audited v31 outcomes, not a new entry model. Primary:
LTCUSDC intersection (distance >=1% AND open outside VA), 116 trades. Union and
all six fixed boost structures are comparisons. No raw trades/profile rebuild.

```powershell
Set-Location 'C:\Users\adjop\OneDrive\Documents\golden-goose-project'
python models/mean_reversion_by_volume_profile/kelly_mc_v32/analyze.py --root . --output models/mean_reversion_by_volume_profile/runs/kelly_mc_v32_5000 --paths 5000 --bootstrap-fits 1000
```

Output must be a new directory. A precomputed 5,000-path run is delivered at that
location. Use another output name for a repeat or a larger simulation:

```powershell
python models/mean_reversion_by_volume_profile/kelly_mc_v32/analyze.py --root . --output models/mean_reversion_by_volume_profile/runs/kelly_mc_v32_50000 --paths 50000 --bootstrap-fits 5000
```

The larger command increases Monte Carlo precision; it does not add historical
evidence or fix selection/regime uncertainty. Arrays scale with path count and
the 903-day horizon; 50,000 paths can require a few GB RAM. Source archives of
25/427 million rows are not read. Dependencies are the project's existing numpy,
pandas, scipy, matplotlib and v31 accounting helper. No new install is needed.

## Math and risk units

```
d_i = abs(entry_i - POC_i) / entry_i
a_i(f) = min(f * fixed_boost_i, 5 * d_i)
f_K = argmax_f mean(log(1 + a_i(f) * net_R_i))
fractional_Kelly_risk_i = q * a_i(f_K)
equity_next = equity_current * (1 + effective_risk_i * net_R_i)
```

`f` is target stop-risk / equity, not position notional / equity. The analysis
uses the actual nonbinary net-R distribution, including time exits, observed
trigger fills and 0.04% fee per side. It preserves the 5x notional cap. Cap knots
can make the objective globally nonconcave: every interval and every kink is
searched. Binary WR-only Kelly and mu/variance approximations are not used.

When the optimum saturates every cap, it is **5x current equity notional on every
trade**. Larger stop-risk targets yield the same allocation. The smallest such
target is reported, not a uniquely identified unconstrained optimum. Fractional
Kelly scales the entire capped position, so quarter Kelly in that case means
1.25x current equity notional, half means 2.5x. This differs from multiplying a
risk target first and then reapplying the original 5x cap.

Initial 10,000 USDC is illustrative. All policies, including the 0.5%/1% risk
baselines, compound from current equity. Their USD/USDC risk amount is not fixed.

## What is calibrated and what is simulated

- Full-history Kelly fits and equity paths are **in-sample**, April 2024 through
  September 2026. Each actual-close equity step is saved with NY timestamp.
- Baseline policies: normal target risk 0.5% and 1%. Kelly fractions: 0.1, 0.25,
  0.5 and 1. The earlier 14 model-account results are independently reconciled.
- Monte Carlo uses stationary circular calendar blocks, mean 30 days; mean 7
  days is a predeclared sensitivity. Inactive/missing-data session dates stay
  flat. Sizing, feature state and outcome are resampled together. Profiles and
  ATR are not synthesized/recomputed. The horizon stays 903 calendar days, but
  the number of sampled entries can vary.
- Primary parent models receive MC at every displayed policy. Conditional boost
  structures receive full-Kelly MC. Identical return streams share simulations.
- Kelly is fixed in the forward resampling. A separate 1,000-draw block-bootstrap
  diagnostic re-estimates its target on a dense grid to show estimation spread.
- Permuting the same whole trades cannot test terminal-return uncertainty: the
  terminal product is invariant. A separate 1,000-permutation check verifies it.
- Retrospective 2026 diagnostic: calibrate on trades completed before midnight NY
  2026-01-01, then lock size for 2026. Expanding train is a documented diagnostic
  exception to 8+2 because Kelly requires 50 observations and the sparse
  intersection has only 116 trades in the full history. 2026 was already viewed
  in earlier research, so this is not a new holdout or a replacement v31 model.

## Outputs

- REPORT.md: results, methods, full-history bias and limits.
- equity_path.png: historical compounded equity and closed DD for both parents.
- monte_carlo_equity.png: intersection pointwise p5/50/95 equity bands; 90% bands,
  not simultaneous confidence bounds. Each panel has its own vertical scale.
- kelly_fits.csv, historical_compounding.csv, yearly_compounding.csv: all models.
- equity_paths.csv: actual-close paths with initial/end-of-data points.
- compounding_ledger.parquet: current-equity risk, units/cash validation inputs,
  equity before/after and actual net P&L.
- monte_carlo.csv: terminal return percentiles, chance of ending below start,
  DD percentiles, and practical-loss thresholds, per policy/block length.
- monte_carlo_bands.csv: pointwise equity percentiles over synthetic days.
- kelly_uncertainty.csv: grid-refit parameter spread, conditional on fixed model.
- locked_2026_diagnostic.csv: temporally locked sizing comparison.
- config_snapshot.json, audit.json: hashes, assumptions and completed checks.

Kelly optimizes empirical geometric growth, without imposing a preferred DD
limit. Monte Carlo probabilities condition on the observed distribution and do
not prove the entry edge is stable. The prior negative LTCUSDT full-history result
still matters. Spread, funding, market impact, liquidation and intratrade DD are
unmodeled, especially material at large notional leverage. Full Kelly is a
mathematical comparison, not a live-risk recommendation.

Checks:

```powershell
python models/mean_reversion_by_volume_profile/kelly_mc_v32/test_analyze.py -v
```
