# Paper Replication

Observation horizon: `2013-12-07` through `2025-04-12`.

This track contains the exact NUPL and MVRV threshold strategies, the CVDD
entry study, buy-and-hold, generated trade logs, tables, and charts. Results
use the supplied Coin Metrics prices rather than the paper's Investing.com
prices, so small differences are expected.

The paper reports MVRV-3 cumulative simple return `3260.62`. The local chart's
approximately `3495x` is final wealth from a different price series and signal
dates. Portfolio models must be compared with the local MVRV-3 result under
identical data and execution assumptions, not directly with `3260.62`.

## Units

The paper's rules are 100% long or 100% cash and the paper states no capital, so
this track used to run each strategy as a unit-stake index: `equity` started at
`1.0` and `net_flow` was `1.0`, under column names that read as dollars. That
made `ending_portfolio_value` look like `$3.49` for a strategy that ends
`3494x` up (audit finding C1).

`run_backtest.py` now runs each strategy as a real `$10,000` account, so
`ending_portfolio_value` and `contributions` are dollars. The recorded
`results/performance_report.csv` still holds index units; multiply its
`ending_portfolio_value` by `$10,000` to compare, and note that every ratio-based
column (average acquisition price, time-weighted return, drawdown, Sharpe) is
unchanged by the rescaling. Verified: the new ending values are exactly `10,000x`
the recorded ones, and the ratio columns agree to within `1e-11`.

The dollar figures now line up with the fixed-capital rows of
`research/portfolio_allocation/results/`: MVRV-3 ends at `$34.9M` and
buy-and-hold at `$1.21M` on both tracks.

## Look-ahead labels

`paper_cvdd_entries_mvrv_6` uses the entry dates printed in the paper's
Appendix. They cannot be reproduced from the data — the locally calculated
CVDD `+/-1%` band never triggers, so `cvdd_mvrv_6` holds cash for the whole
sample (`0` computed entries) — which makes that row the best Sharpe in the
table for reasons that are not deployable. It is now flagged
`ex_post_entries = True` in `strategy_summary.csv` (audit finding C2), and
`paper_cvdd_trades.csv` carries `ex_post_entries` and `exit_source`
(`ex_post_cycle_peak` for the 2015 and 2018 cycles, `sample_end` for the third)
instead of the old `lookahead_exit` boolean, which did not flag the sample-end
"exit" (audit findings C2, C4).

## Monte Carlo columns

`cvdd_monte_carlo.csv` reports `beat_random_share` — the share of random nearby
entry dates that the actual entry outperformed — and `p_value` as its
complement. The columns were previously named `*_p_value` while holding the beat
share, so the recorded values of `0.94` to `1.00` read as "not significant" when
they actually say the CVDD entries beat `94%` to `100%` of nearby random dates
(audit finding C3). The recorded CSV keeps the old column names.
