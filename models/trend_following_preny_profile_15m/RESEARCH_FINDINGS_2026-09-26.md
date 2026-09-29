# Consolidated research findings — TF-PRENY-15M

**Canonical research note, updated 2026-09-26.** Keep subsequent findings for the
active `trend_following_preny_profile_15m` model in this file. Older ORB/CP005
experiments are not silently combined with this model. Numerical ledgers remain
in their original run directories; this file consolidates their interpretation.

Current conclusion: no universal profitable filter or fixed-TP setting has been
established. Delta/result has its clearest support on HYPE; MA alignment is
promising on ETH; BTC ADX has a modest expansion association with important
stop/fee confounding. BNB remains weak. These are research findings, not newly
approved entry rules.

## Original checkpoint TF-PRENY-15M-001

Decision date: 2026-09-26. Freeze findings and baseline specification, **not a profitable-filter claim**.

## Scope decision

Active baseline: completed 15m price/delta, 01:00-09:00 NY Pre-NY profile only. Four separate assets: BTCUSDC, ETHUSDC, BNBUSDC, HYPEUSDT. The prior study used eight asset/profile groups; subsequent studies use four Pre-NY groups. Do not carry over the previous-24h four-asset improvement claim to Pre-NY.

## Source and method

Source: `runs/four_asset_plug_study_v01/{summary.csv,predictions.jsonl,folds.jsonl,common_period_summary.csv}` under this renamed folder. Original run provenance used `models/daily_profile_1h`.

Six one-at-a-time gates, 180-calendar-day rolling train, one-session embargo, 60-day test, minimum 25 train trades. Median-based thresholds learned from earlier train data, not the same test outcomes. This was a ledger gate study (rejected trade = zero R), not a new compounded-equity replay or delayed/replacement-entry simulation. Historical ATR normalization uses completed 1h ATR; do not silently reinterpret as 15m ATR.

## Pre-NY delta-result results only

Reject condition under study: directional delta imbalance >= earlier train median AND directional displacement / ATR < earlier train median. Strong effort without comparable result, not simply positive delta or declining volume.

| Asset | Baseline test trades -> kept | WR before -> after | Mean net R before -> after | Total net R before -> after |
|---|---:|---:|---:|---:|
| BTCUSDC | 546 -> 413 | 29.3% -> 31.5% | -0.117 -> -0.096 | -63.74 -> -39.61 |
| ETHUSDC | 504 -> 392 | 30.4% -> 32.9% | +0.049 -> +0.101 | +24.72 -> +39.55 |
| BNBUSDC | 114 -> 88 | 33.3% -> 34.1% | -0.038 -> -0.182 | -4.30 -> -15.98 |
| HYPEUSDT | 182 -> 146 | 34.1% -> 37.7% | +0.329 -> +0.547 | +59.80 -> +79.91 |

Three of four Pre-NY groups improve, but BNB deteriorates and BTC remains negative. On the common period 2026-03-03 through 2026-07-30, Pre-NY improves only BTC/HYPE, not ETH/BNB. No universal-stability claim.

The earlier eight-group finding (7/8 mean expectancy improvements) included previous-24h and is historical context only. VWAP persistence and stricter edge acceptance were not consistent upgrades under the exact tested definitions.

## Uncertainty and opportunity cost

- A smaller total loss can come merely from fewer trades in a negative baseline. Compare retained mean R and an equal-frequency random-rejection control, not only total R.
- Diagnostic weekly block bootstrap, 5000 resamples, seed 20260926: only HYPE Pre-NY had a nominal 95% positive interval for retained-minus-baseline mean R. No multiple-testing correction; not universal statistical proof.
- Pre-NY >=2ATR-before-stop tail retention: BTC 171/207 (82.6%), ETH 159/200 (79.5%), BNB 31/42 (73.8%), HYPE 56/64 (87.5%). Filtering loses meaningful expansion too.
- Same-date crypto outcomes are correlated. Historical periods have already been examined. Walk-forward replay is not genuinely untouched OOS here.
- Prior validation recomputed all 48 summary groups consistently, found no duplicate asset/profile/plug/day records and no train/test date overlap. This does not constitute an exhaustive raw execution audit.

## Next experiment (historical plan at the initial checkpoint; now completed below)

Use shared `backtest_engine.replay_aggtrades` for baseline vs one delta-result challenger, independently on each asset, Pre-NY only. Keep candidate timing, POC, next-day 08:59 exit, fee and sizing fixed. Do not replace rejected trades with later entries yet. Threshold procedure stays train-only. Missing evidence preserves the baseline rather than silently rejecting.

Report net expectancy, realized equity/DD, period stability, losers avoided, winners/tails lost, and same-frequency random-removal comparison. Do not add new thresholds, stack gates, tune by asset or change runner exits simultaneously. No large experiment was run as part of this checkpoint.

## Challenger implementation prepared

`replay_challenger.py` and `run_challenger.ps1` now reuse the frozen study decisions
and the shared raw replay engine. See `CHALLENGER.md`. Preflight reproduced the
four Pre-NY candidate counts in the table above. Synthetic tests verify gate
independence from outcomes, embargo validation, raw replay, equity rescaling and
resume integrity. No full historical raw replay has been run by the assistant;
economic results were pending at that point and are now incorporated below. The 1h ATR denominator of
the original research gate is explicitly retained, not retuned to 15m.

## A. Scope and rules retained throughout this research

- Completed 15m candles and 15m delta; frozen 01:00–09:00 NY profile only.
- Entry window 09:00–12:00 NY, maximum one candidate per session; initial stop POC.
- Original exit: hard stop or next-day 08:59 time trigger. Fixed-target variants
  add full exits at 1/1.5/2R; the time exit remains a fallback.
- Fee 4 bps per fill; risk 0.5% of current equity; starting equity 1000; leverage
  cap 5. Each asset/policy is a separate portfolio.
- No bubbles, P99, large-trade clusters, added MA/ADX entry filter or delayed
  replacement entry. No rule change was made while analyzing these outputs.
- ATR normalization in the six historical plugs remains **1h**. New MA/ADX
  observations are **15m** and do not replace historical denominators.
- Next-aggTrade fills model observed execution-price changes, not bid/ask depth,
  market impact or funding. A `total_slippage=0` field is not proof of zero
  execution friction. Reported DD uses realized-close equity, not mark-to-market.

## B. Six separate plug findings

Source: `runs/four_asset_plug_study_v01/cross_asset_stability.csv`, Pre-NY rows only.
Number of assets with positive versus negative change in total net R per original
opportunity (not just higher expectancy among fewer retained trades):

| Plug | Positive / negative assets | Conclusion for this definition |
|---|---:|---|
| VWAP persistence | 1 / 3 | Not a consistent upgrade |
| Value-edge acceptance | 1 / 3 | Stricter acceptance not automatically better |
| Volume without result | 0 / 4 | No improvement in this tested form |
| Delta without result | 3 / 1 | Only challenger advanced; not universal |
| ATR activation | 2 / 2 | Mixed |
| Fee/stop burden | 2 / 2 | Mixed; can remove useful expansion |

These are findings about the tested operational definitions, not proof that an
entire indicator family is useless. No stacking or per-asset optimum selected.

## C. Completed raw replay and statistical validation of delta challenger

Source: `runs/four_asset_preny_delta_challenger_v01`, with the original time exit.
The original test-candidate counts and R results above were reproduced in raw
replay. Challenger keeps BTC 413/546, ETH 392/504, BNB 88/114, HYPE 146/182.

The more complete statistical audit used 10,000 calendar-block resamples, with
7/14/28-day block sensitivity and Holm correction across the four primary asset
comparisons. Primary outcome: challenger total net R minus baseline total net R,
divided by ORIGINAL candidate count (rejected candidates contribute zero).

| Asset | Policy change R/original candidate | 95% interval, 14-day blocks |
|---|---:|---:|
| BTC | +0.0442 | [-0.0486, +0.1483] |
| ETH | +0.0294 | [-0.0870, +0.1774] |
| BNB | -0.1024 | [-0.3388, +0.2030] |
| HYPE | +0.1105 | [+0.0484, +0.1701] |

Only HYPE survives the four-primary-comparison correction consistently across
block lengths. This does NOT erase the prior search over six plugs or repeatedly
inspected history. Secondary PF/MFE gains are less robust under broader multiple
comparison correction. Common-period cross-asset evidence does not establish a
universal filter. A smaller loss on BTC is not proof of positive edge.

## D. Unified EMA200 / SMA20 / ADX observation

Source: `runs/four_asset_observation_context_v01`. 1,786 executed baseline rows;
1,783 ready trend contexts, three warmups; no duplicate asset/session keys or
missing stop-first MFE values. The same ledger includes six-plug inputs, original
audit snapshots, copied test-fold decisions, profiles and outcomes in separate
namespaces. It is not a ledger of all no-trade sessions.

EMA200 is 200 completed 15m bars (50 hours), not a daily macro regime. MA20 means
SMA20. ADX14 describes strength, not direction; DI and MA structure add direction.
Aligned long = close > SMA20 > EMA200; aligned short = close < SMA20 < EMA200.
Mixed is not automatically countertrend. No fully opposed-stack candidates were
present in the observed population.

Results on the existing test-fold candidates, original time exit:

| Asset | Aligned / mixed N | Aligned / mixed expectancy R | PF(R), aligned / mixed | Mean MFE %, aligned / mixed |
|---|---:|---:|---:|---:|
| BTC | 327 / 219 | -0.034 / -0.241 | 0.953 / 0.690 | 1.579 / 1.254 |
| ETH | 300 / 204 | +0.287 / -0.300 | 1.421 / 0.611 | 2.617 / 1.851 |
| BNB | 66 / 48 | -0.034 / -0.043 | 0.950 / 0.947 | 1.304 / 0.976 |
| HYPE | 102 / 80 | +0.135 / +0.575 | 1.194 / 1.900 | 3.079 / 2.899 |

ETH's alignment difference is +0.587R (14-day bootstrap interval +0.172 to
+1.032R). Positive in 11/12 full folds; negative in the last partial fold.
Dropping the three largest ETH winners leaves aligned expectancy +0.079R versus
mixed -0.300R. However approximate p=.011 becomes .102 with Holm correction over
12 primary associations: promising, not a confirmed universal rule.

ADX/net-R rank correlations: BTC +.292, ETH +.194, BNB -.010, HYPE +.146.
BTC/ETH survive the primary multiple-comparison correction, BUT ADX also
correlates with stop width. Partial rank correlations controlling stop-risk %:
BTC +.071, ETH -.014, BNB -.069, HYPE +.016. Therefore much of the apparent R
relationship may reflect geometry and lower relative fee burden, not independent
entry quality. This diagnostic is not proof of causality.

ADX/MFE-percent correlations: BTC +.140 (modest; adjusted p=.013 at 14-day blocks),
ETH +.017, BNB -.018, HYPE +.033. A high ADX does not universally predict a large
future tail. Conventional ADX bands are descriptive, not approved cutoffs.

Interaction with delta challenger, change in R per original subgroup candidate:

| Asset | MA aligned | MA mixed |
|---|---:|---:|
| BTC | +0.015 | +0.087 |
| ETH | -0.062 | +0.164 |
| BNB | -0.044 | -0.183 |
| HYPE | +0.102 | +0.121 |

ETH aligned challenger removes 66/300 trades with little per-trade expectancy
change and loses 18.65 aggregate R. Thus stacking MA and delta is not automatically
better. Interaction subgroups have not validated a new conditional policy.

## E. Fixed reward sweep: 1R / 1.5R / 2R

Source: `runs/four_asset_preny_fixed_reward_sweep_v01`. All 24 portfolios completed,
zero skipped/excluded candidates. Recomputed trade count, mean R, fee arithmetic,
cash PF, PF(R), ending equity and realized DD against individual ledgers. Entry
time, entry price, direction and POC are identical across targets and the prior
time-exit control for each candidate. Equity-dependent quantity changes are
expected as exit policies change the compounded equity path.

This is the SAME test-fold candidate cohort, not all raw-history sessions despite
the `full` suffix. Session windows: BTC 2024-07-05–2026-08-30; ETH first candidate
2024-07-06 and last 2026-07-29; BNB 2026-03-03–2026-08-30; HYPE
2025-12-01–2026-08-30. Selecting a reward from this sweep is exploratory, not new OOS.

| Asset | Policy | N | E at 1R | E at 1.5R | E at 2R |
|---|---|---:|---:|---:|---:|
| BTC | Baseline | 546 | -0.116 | -0.130 | -0.143 |
| BTC | Delta challenger | 413 | -0.091 | -0.074 | -0.091 |
| ETH | Baseline | 504 | -0.010 | -0.029 | -0.019 |
| ETH | Delta challenger | 392 | -0.013 | -0.062 | -0.037 |
| BNB | Baseline | 114 | -0.060 | -0.042 | -0.043 |
| BNB | Delta challenger | 88 | -0.091 | -0.062 | -0.092 |
| HYPE | Baseline | 182 | +0.008 | -0.022 | -0.018 |
| HYPE | Delta challenger | 146 | +0.086 | +0.060 | +0.057 |

E = mean NET R/trade. Full challenger results (initial equity 1000):

| Asset | Target | WR | PF cash | Final equity | Realized max DD |
|---|---:|---:|---:|---:|---:|
| BTC | 1R | 50.36% | 0.815 | 825.09 | 21.84% |
| BTC | 1.5R | 42.86% | 0.863 | 851.72 | 25.21% |
| BTC | 2R | 38.01% | 0.842 | 822.76 | 29.52% |
| ETH | 1R | 52.30% | 0.970 | 971.31 | 9.96% |
| ETH | 1.5R | 42.09% | 0.893 | 879.96 | 20.90% |
| ETH | 2R | 38.27% | 0.937 | 923.10 | 14.40% |
| BNB | 1R | 51.14% | 0.817 | 959.70 | 7.33% |
| BNB | 1.5R | 44.32% | 0.886 | 971.73 | 7.85% |
| BNB | 2R | 38.64% | 0.851 | 958.75 | 7.91% |
| HYPE | 1R | 56.85% | 1.187 | 1062.60 | 4.58% |
| HYPE | 1.5R | 46.58% | 1.103 | 1042.03 | 6.09% |
| HYPE | 2R | 41.10% | 1.090 | 1039.21 | 7.29% |

All assets exceed 50% WR at 1R, but BTC/ETH/BNB still lose after fees. Example BTC
challenger 1R: average win +.826R, average loss -1.021R; break-even WR with this
realized payoff mix is approximately 55.3%, above observed 50.36%. Not all wins
are TP fills: time exits, fees and next-print execution produce nonbinary payoffs.

Cost decomposition, challenger 1R mean gross R / fees R / net R:
BTC +.0127 / .1035 / -.0908; ETH +.0636 / .0761 / -.0125;
BNB +.0314 / .1225 / -.0911; HYPE +.1404 / .0549 / +.0855.
BTC has little gross edge to pay costs. This is not fixed by increasing reward
alone: all tested BTC/ETH/BNB settings have negative measured net expectancy.

### Comparison with no fixed TP

HYPE delta challenger, identical 146 entries and stops:

| Exit | WR | Mean net R | PF cash | Final equity | Realized max DD |
|---|---:|---:|---:|---:|---:|
| POC stop / time exit, no fixed TP | 37.67% | +0.547 | 1.868 | 1455.25 | 5.00% |
| Fixed 1R plus time fallback | 56.85% | +0.086 | 1.187 | 1062.60 | 4.58% |

The WR increase trades away substantial tail profit with only a small realized-DD
reduction in this sample. This does not prove the uncapped policy is safe: its
top three winners supply 36.0% of positive cash PnL, and mark-to-market DD is not
measured here. Do not confuse lower observed close-equity DD with low tail risk.

On ETH, previous delta benefit under time exit does not transfer to fixed TP:
challenger per-trade E is worse than baseline at all three fixed targets. On BNB
it also remains worse. On BTC it reduces losses without achieving positive edge.
On HYPE it improves all fixed targets, but its best fixed-target result is still
much smaller than the prior no-fixed-TP result.

### Statistical caution and linkage to MA context

Diagnostic paired 14-day calendar-block bootstrap, 10,000 resamples, did not
establish positive challenger expectancy for any fixed target. HYPE 1R E=.0855R
has a 95% interval [-.0450, +.2233]. These intervals are pointwise, not adjusted
for selecting among reward settings. Some challenger-versus-baseline policy
improvements (BTC/HYPE) have positive pointwise intervals; that is not the same
as proving the resulting strategy profitable or better than random removal.

Descriptive ETH baseline MA join remains directionally consistent under fixed TP:
aligned versus mixed E = +.028/-.067 (1R), +.036/-.124 (1.5R), +.059/-.134 (2R).
No new MA-filter replay or policy optimization was performed. Those are small
group returns, not validated deployment evidence.

## F. Decision register / next research

1. Keep baseline specification frozen for comparison, not as a profitable model.
2. Keep delta challenger experimental; no universal promotion, especially BNB.
3. MA-alignment on ETH is a coherent next hypothesis, but do not invent a different
   winning rule for every asset or stack MA/ADX/delta without isolation.
4. ADX's R association must be separated from risk/fee geometry; higher ADX is not
   sufficient evidence for larger future tails or higher sizing.
5. Fixed 1R can increase WR above 50% without economic viability. Fixed reward
   alone did not repair BTC/ETH/BNB in the measured cohort.
6. No reward/runner/sizing setting was frozen from this sweep. Do not launch another
   broad exit grid before deciding which entry/context hypothesis to falsify next.

## G. Reproducible analysis locations

Primary run folders are under this model's `runs/` and named above. Additional
read-only analysis scripts, bootstrap outputs and calculation checks are stored in
`C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/analysis/`:
`preny_significance_v01`, `preny_trend_context_v01`, `preny_reward_sweep_v01`.
This document is the canonical summary; those files are supporting calculations,
not competing model rules. No original runs, caches or legacy findings were deleted.

## H. Challenger 2 approved for annotation only (2026-09-26)

Next experiment is stateful directional effort/result, independent of the delta
challenger. First stage does not filter or replace entries. All baseline fields,
stops and outcomes remain unchanged; MA/EMA/ADX are retained as context only and
are NOT part of this evaluator. No bubbles/P99/cluster features.

`evaluate_plugs --effort-result-context` enriches the consolidated ledger and
exports the 09:00–12:00 timeline for all available profile sessions, including
no-entry sessions. Four directional interpretations: initiative, possible
absorption, path of least resistance, participation fading. Finite recent memory
and outside-value episode resets prevent stale evidence accumulating forever.
See `OBSERVATION_CONTEXT.md` for the exact fixed research definitions and command.

The stage separates original signal-time annotations from later timeline bars.
No higher win rate or new edge is claimed yet. Only after measuring incremental
information may a separate WAIT/re-entry timing policy be tested. Subsequent
SMA20, EMA200 and ADX experiments remain separate, not stacked with C2 by default.
