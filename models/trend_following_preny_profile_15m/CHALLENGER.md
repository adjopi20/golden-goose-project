# Delta effort without price result: execution challenger

Research only. Entry logic in `strategy.py` is unchanged. Pre-NY only, four
separate assets. No new backtest engine: `replay_challenger.py` exports signal
subsets and invokes `backtest_engine.replay_aggtrades` for both portfolios in
one raw pass per asset.

## Frozen experiment

- Reuse `runs/four_asset_plug_study_v01`'s earlier-training medians and test
  folds (180 train days, 1 embargo day, 60 test days, 25-trade minimum).
- Only `delta_without_result`: reject high directional delta imbalance with
  below-median directional price displacement / historical 1h ATR. All price
  and delta observations for the gate are completed 15m bars. No new ATR scale.
- Recompute each Boolean from saved features/thresholds to validate the study.
  Outcome columns do not decide whether the trade is kept.
- Baseline and challenger use the **same historical executed candidate cohort
  in test folds**, not all sessions or new opportunities. Selection of this
  cohort is conditional on historical execution; this is a matched attribution
  test, not a full live-system qualification. Future signal generation is not
  altered by this adapter.
- Missing features keep the baseline trade. No rejected trade is replaced by
  a later entry. No stacking filters or choosing different rules per asset.
- Actual entry/stop/exit must match between portfolios for retained trades.
  Quantity and money P&L change as equity compounds after skipped trades.
- Config remains equity1000, risk0.5%, fee4bps/fill, leverage cap5, entry delay
  limit300s. POC stop and next-day08:59 time exit remain unchanged. No TP/trail.

## Run

From the repository root:

```powershell
.\models\trend_following_preny_profile_15m\run_challenger.ps1 -Stage prepare
.\models\trend_following_preny_profile_15m\run_challenger.ps1
```

`prepare` reads existing observation/study files, not raw ticks. `all` (default)
prepares then runs raw replay and reports. No observation/cache rebuild needed.
Optional `-Symbols BTCUSDC` runs one asset. The default runs all four sequentially.
Each complete raw replay is hash-checked and reused on rerun; interrupted asset
replay restarts. Altered source/config requires a fresh output folder.

Output: `runs/four_asset_preny_delta_challenger_v01/`.

- `comparison_all_assets.csv`: independent asset/variant equity, WR, PF_R,
  expectancy, fees, realized-close DD and losing streak.
- `common_period_comparison.csv`: overlapping calendar interval, cached fills
  rescaled to the same starting equity; no pooled cross-asset equity.
- Per asset: raw `replay/{baseline,challenger}` ledgers/orders/equity/summary,
  `fold_comparison.csv`, `opportunity_cost.json[l]`, causal decisions and folds.
- `random_same_frequency.csv` / `random_summary.json`: 1000 fixed-seed random
  subsets with the same kept trade count per test fold. Proportional equity
  rescaling from fixed fills avoids 1000 raw scans and is checked against the
  actual challenger replay. This works only for this proportional sizing model,
  no overlapping positions, no lot rounding and no size-dependent impact.
  Percentiles are a diagnostic null, not an adjusted significance test or a
  tradable random-selection policy.

Tail diagnostics use the existing lower-bound MFE before stop (>=2% and >=2ATR);
minute-bar ambiguity and missing MFE remain explicit. Report sacrificed winners
and tails as well as avoided losers. Do not tune thresholds from this replay.

AggTrades are a fill proxy, not bid/ask depth. Funding/market impact remain absent.
Equity/DD are realized-close based. Already-inspected historical folds are not
untouched OOS. A high random-control percentile is not deployment approval.
