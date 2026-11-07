# Active model: full-history LTC sizing — v31

## v31.1 data-reader fix

The LTCUSDT archive contains repeated aggregate trades on 2022-09-12 and
2022-09-13. The runner now retains one occurrence of an aggregate ID **only if
every copy has exactly the same timestamp, price and qty**. First-occurrence
order is preserved; timestamps are not sorted or reconstructed. Distinct IDs at
the same timestamp remain separate transactions. Conflicting repeats or remaining
ordering problems still fail explicitly. No raw Parquet file is changed.

`LTCUSDT_raw_quality.csv` and `LTCUSDC_raw_quality.csv` show raw/used row counts,
copies removed, and invalid/missing days. The audit reconciles every raw row as
either a used observation or a verified duplicate. A missing day remains missing.

The existing failed `frozen_sizing_v31_full` run can continue using the same
`--resume` command below. Only the known original v31 code is eligible for
automatic migration, and rules, sources, indicator hashes and environment must
still match. `config_snapshot.original_v31.json` preserves the original snapshot;
completed checkpoint rows remain reusable because the original reader stopped
before it could checkpoint any duplicate day. The full LTCUSDC pass and the
completed 2020–2022 LTCUSDT history do not need to be repeated.

Trading, sizing, fees and ATR calibration rules are unchanged. Current patch
validation is recorded in `PATCH_VALIDATION.json`.

## Run

From PowerShell:

```powershell
Set-Location 'C:\Users\adjop\OneDrive\Documents\golden-goose-project'
python models/mean_reversion_by_volume_profile/frozen_sizing_v31/backtest.py --root . --output models/mean_reversion_by_volume_profile/runs/frozen_sizing_v31_full
```

This processes **all available dates** in every Parquet archive inside
`storage/ltcusdc` and `storage/ltcusdt`, including subfolders. There is no start/end
date filter and no substitution of assets. Overlapping files fail explicitly;
keep one non-overlapping archive set per pair. Required numeric columns:
`timestamp` (epoch milliseconds), `price`, `qty`, `agg_trade_id`.

The runner needs the project's existing numpy, pandas, pyarrow and IANA timezone
support. No download, API key, new packages or old research run is required.
Profiles use `indicator.volume_profile.build_basic_volume_profile`; true range
uses `indicator.trend.true_range`.

The full 427-million-row LTCUSDT run is for the user to execute. Progress prints
every 30 UTC days. Memory is bounded to three daily arrays and eight cached row
groups, with small trade/session/report tables retained. Do not modify sources
during execution. Source fingerprints use metadata, size and mtime, without
hashing 12 GB of raw data.

To continue an interrupted run, repeat the same command with `--resume`:

```powershell
python models/mean_reversion_by_volume_profile/frozen_sizing_v31/backtest.py --root . --output models/mean_reversion_by_volume_profile/runs/frozen_sizing_v31_full --resume
```

Resume requires identical inputs, rules, indicator files and environment; code
must match, except for the explicitly supported original-v31 migration above.
Completed UTC days are reused. An incomplete final JSONL checkpoint line is
discarded and recomputed. Output folders are never overwritten by a fresh run.
Only `audit.json` with `status: passed` confirms completion.

## Frozen trading rules

Every calendar day, including weekends:

1. Freeze the previous 09:30 NY → current 09:30 NY profile, 50 equal-width bins,
   quantity weights and 70% value area. DST is handled with America/New_York.
2. The first observed print at/after NY open determines simple-base direction:
   opening below POC → long only at VAL; above POC → short only at VAH.
   Exactly at POC skips. This is **not A / first-boundary direction**.
3. Entry is the first equality/crossing of that selected boundary during the
   next NY-open period, from either direction. No body, timeframe, second-push,
   delta, CVD or ADX condition. No crossing from before the open triggers entry.
4. Actual observed touch print is entry; invalid first-touch geometry consumes
   the opportunity. TP stays POC, SL is symmetric from actual entry, RR 1:1.
   First subsequent TP/SL-trigger print is exit; otherwise last print before
   the next 09:30 NY. Maximum one trade per profile period.
5. D = abs(entry − POC) / entry >= 1%. L = NY open strictly outside [VAL, VAH].
   L is fixed: no profitability selection, no inside/outside re-training, and
   no skipped first fold inherited from v25. Union = D OR L, deduplicated;
   intersection = D AND L. Membership is evaluated on the first candidate.
6. Normal risk = 0.5% current account equity; boosted risk = 0.75% or 1%.
   Entry notional/equity remains capped at 5×, so boosts can be partially or
   completely limited. Fee = 0.04% each side on actual entry/exit notionals.

## The six sizing cases

| Case | Parent trades | Boost condition |
|---|---|---|
| 1 | Union | L |
| 2 | Intersection | L, hence every intersection trade |
| 3 | Intersection | ATR high |
| 4 | Union | ATR high |
| 5 | Union | L AND ATR high |
| 6 | Intersection | L AND ATR high; identical to case 3 |

Both 1.5× and 2× are run for each case, plus normal-sizing union/intersection
baselines: 14 independent hypothetical accounts **per pair**. Accounts do not
share capital. WR and unweighted mean R cannot change solely because of sizing.

## Adaptive ATR: the only recalibration

- ATR30% = SMA of 30 daily true ranges / last completed daily UTC close × 100.
  Needs 31 consecutive valid daily bars. It is not Wilder ATR.
- Latest completed daily state available at actual entry is used; no current
  daily candle. Gaps/partial daily bars invalidate affected ATR windows.
- At midnight NY on Jan 1, Mar 1, May 1, Jul 1, Sep 1 and Nov 1, calculate
  q(2/3), approximately percentile 67, from daily ATR states available in the
  preceding eight calendar months; freeze it for the following two months.
  The same calendar applies to both pairs, each using its own raw data.
- Require a full eight-month raw history window and at least 60 valid ATR
  states. This is feature calibration only; no outcome/P&L enters it.
- Entry-time ATR > that frozen cutoff means high. An entry after midnight in
  a new block uses the newly available cutoff even if its session opened on
  the preceding date.
- Before adequate history, or if current ATR is unavailable, trade with normal
  risk. Do not discard early-history trades. Report ATR-ready/unavailable
  counts separately. Unknown does not mean low volatility.

The originally quoted v30 numbers will **not** be reproduced exactly. Those
were test-only results with train-selected L. This run uses full history,
always-outside L, and explicit ATR warm-up. These are intentional user-approved
changes. No thresholds are tuned against the new results.

## Outputs and interpretation

One selected output folder contains:

- `REPORT.md`: overall and yearly tables, definitions and limits.
- `summary.csv`: 28 full-history account results, gross/net WR, raw mean/median
  R, raw and weighted net R, compounded return, closed-trade DD, cap/boost counts.
- `period_metrics.csv`: years, semesters and fixed two-month blocks, including
  zero-trade periods; first/last session and valid-session counts included.
  Periods belong to NY session-open date and reset equity to 1 for that period.
- `stability.csv`: counts of positive/negative nonempty two-month periods and
  the range of period net expectancy. Interpret together with period trade counts.
- `common_dates_summary.csv`: each account on the same valid NY session dates
  across both pairs, to avoid confusing longer USDT history with better results.
- `sessions.csv`, `quality.csv`: every session, invalid periods and exclusion
  reasons. Session filters retain the earlier 300s internal / 60s boundary-gap
  limits and consecutive aggregate IDs. Thin markets can lose many sessions.
- `trades.parquet`: one simple-base trade per eligible session, with fixed
  D/L membership and causal ATR annotations before sizing.
- `sizing_ledger.parquet`: all account trades, risk fractions, notional,
  equity before/after, fees and closed DD. Each row is independently checked
  with position units × actual entry/exit prices and cash fees.
- Per pair: raw UTC-day checkpoint, daily ATR states and two-month cutoff table.
- `config_snapshot.json`, `audit.json`: frozen rules, sources, code/indicator
  hashes, environment, completion checks and report hashes.

Raw `mean_net_r` and `total_net_r` describe trades before conditional weighting.
`risk_weighted_mean_net_r` weights by actual equity-risk fraction;
`weighted_total_net_r` expresses P&L in normal 0.5%-risk units at each trade's
equity. Account return compounds with current equity. Larger returns with
larger sizing are not automatically evidence of a stronger entry signal.

This is a trade-print execution proxy: no spread, latency, impact, funding,
liquidation, guaranteed limit fills or intratrade equity/DD. Exchange and product
provenance are unverified. Previously examined LTCUSDC history is development
evidence; shared-underlying LTCUSDT is a cross-pair robustness check. No automated
claim of stable edge is made merely because total net profit is positive.

## Local checks

```powershell
python models/mean_reversion_by_volume_profile/frozen_sizing_v31/test_backtest.py --root . -v
```

Tests include synthetic execution and accounting, DST, missing history, ATR
causality, overlapping archives, and a 340-day synthetic end-to-end run for both
pairs with resume. They do not execute the large real-history backtest.
