# Selected entry / partial-time exit study — 2026-09-26

Research configuration, not a newly approved trading model. No new indicator,
entry timing, stop selection, or replacement-entry experiment is introduced.

## Frozen entry subsets

| Asset | Independent entry variants |
|---|---|
| BTCUSDC (3) | EMA200 signed distance >=0.5%; ADX14 >=25; ADX14 >=30 |
| ETHUSDC (6) | Baseline; C2 initiative OR possible absorption OR support persisting; MA stack aligned; EMA200 signed distance >=0.5%; ADX14 >=30; C1 delta/weak-result exclusion |
| BNBUSDC (3) | C2 initiative; EMA200 signed distance >=0.1%; ADX14 >=30 |
| HYPEUSDT (9) | Baseline; C1; ATR activation; C2 initiative; C2 support persisting; MA stack aligned; MA stack mixed; ADX14 >=25; C2 initiative OR support persisting |

HYPE selection means the eight positive-PF entries in the latest compact table,
plus the requested union; NOT every threshold in the wider 109-row catalog.
BNUSDC/HYPEUSDC in the request are interpreted as existing BNBUSDC/HYPEUSDT data.
MA is SMA20 versus EMA200 on completed 15m candles. Signed distance means above
EMA for longs and below EMA for shorts. C2 unions deduplicate each original signal.
Possible absorption is an included descriptive subgroup for ETH, not an automatic
buy signal or a universal exclusion. HYPE's union explicitly excludes it.
Missing EMA/ADX/C1 context retains baseline, matching the prior study; MA and C2
subgroups require the relevant observed label. No gate uses future outcomes.

## Exit and execution

TP1 R = [1, 1.5, 2]; TP1 allocation = [10%, 20%, 50%]. Cartesian product = 9.
R is anchored to actual entry fill and original POC stop. Close the selected
fraction at the next aggregate trade after the target is crossed. The original
stop remains on the remainder. No breakeven, trailing, or second price target.
Force-close at first trade at/after the signal's 08:59 NY next-day cutoff.
Before TP1, stop/time exit applies to the whole position. TP1 does not guarantee
a profitable complete trade. Stop gaps can lose more than the planned 1R.

Shared engine: `python -m backtest_engine.replay_aggtrades --grid ...`.
The model's preparation adapter only exports signal subsets; the PowerShell file
orchestrates existing tools. It does not implement another backtest engine.
Each variant gets an independent initial $1,000 account, 0.5% current equity risk,
4 bps fee per fill, and 5x maximum leverage. This is compounding, not fixed cash
risk. Leverage caps can reduce actual risk below 0.5%; fees can make losses larger.
21 entry choices x 9 exits = 189 independent portfolios, not 189 concurrent bets.
Raw prints approximate fills, not depth-aware executable liquidity. Funding and
unobserved market impact are not included; net means after configured fill fees.

## Cohort / causality

Keep existing historical test-fold membership so comparisons retain the same
population. The initial ~180-day training window plus one-day embargo explains
the later reported starts; earlier available history is NOT untouched OOS.
These historical test folds have since been inspected repeatedly, so are research
data, not fresh validation. Assets need not share the same start date.
ETH extends with new August 1–30 observations; August 31 is needed for settlement.
August features reuse historical definitions and pre-entry timestamps. C1's latest
pre-August training thresholds stay fixed throughout August (no August outcome fit).
The ETH August rows are tagged separately in selection decisions. Historical
simulation and August continue the same equity account, but August metrics should
also be reported separately before making robustness claims.

## Outputs and resume

Run `run_selected_exit_sweep.ps1 -Symbol All` (or one asset). Large runs are for the
user to launch. ETH alone builds missing August cache/observation. Every asset is
replayed once for all its combinations. Prepared entry files are retained for reuse.
`comparison_all_assets.csv` combines metrics, not capital. Each portfolio exports
summary, trades, fills/orders, decisions, `equity_curve.jsonl`, `equity_curve.png`.
Equity/DD are completed-trade realizations, NOT continuously marked-to-market;
partial proceeds are consolidated in the final trade outcome. With at most one
open position this does not alter the next trade's compounded sizing.
Completed runs are skipped. To change rules/configuration use a new OutputRoot.

## Reminder — explicitly NOT executed

Later consider C1, C2, and C2 union combined with exactly ONE of MA, EMA, or ADX,
one experiment at a time, possibly looser thresholds (for example ADX >=10).
Do not combine MA + EMA + ADX together. Do not add these combinations to this grid.
After exit comparison: stability, recent regime validation, then Monte Carlo.
