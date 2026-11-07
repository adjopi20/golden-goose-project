# Bitcoin On-Chain Cycles — selected runtime

The selected daily service is **0/7 + four-year P5/P95**, initial 50:50 capital.
Start with [live_mvrv/README.md](live_mvrv/README.md) and [live_mvrv/CONTRACT.md](live_mvrv/CONTRACT.md).
That service is paper/alerts only and does not use accumulation, CVDD, Chromium or an AI API.
See [LEGACY.md](LEGACY.md) for preserved research and legacy entrypoints.

The material below documents earlier research and accumulation models; it is NOT the selected deployment contract.

---

# Bitcoin On-Chain Cycles

## Audit correction (2026-09-29)

Read `research/CORRECTIONS_2026-09-29.md` before relying on older reports.
Corrections change several experimental cash-deployment results and invalidate
the old forward-drawdown study. Frozen thresholds have not been retuned.
The accumulation monitor is not the paper MVRV/CVDD buy-and-sell strategy.

Daily, long-or-cash Bitcoin strategies from Grobys, Nasman, and Sandretto
(2026), "Using on-chain data to predict Bitcoin cycles."

## Required data

- `btc.csv` (**required**): `time`, `PriceUSD`, `CapMrktCurUSD`, and
  `CapMVRVCur`, refreshed from the Coin Metrics community API.
- `data.tsv` (**optional**): full-history daily Coin Days Destroyed, used only
  to calculate the paper's CVDD entry test. Every live strategy in this model
  runs without it; see "CVDD limitation" below.

`bitcoin_dataset.csv` is not used because it contains neither CDD nor the
realized-capitalization inputs required by the paper.

## Rules

- NUPL 1/2/3: buy below `0`; sell at `0.67`, `0.70`, or `0.73`.
- MVRV Z-score 1/2/3: buy below `-0.2`; sell at `5`, `6`, or `7`.
- CVDD: flag the first day price enters a `±1%` band around CVDD.
- Hybrid: buy on a CVDD entry and sell when MVRV Z-score reaches `6`.
- Buy-and-hold: buy on `2013-12-07` and hold through `2025-04-12`.
- Position size: 100% BTC while long, otherwise 100% zero-yield cash.
- Main-paper transaction costs: zero.

Signals are evaluated at the daily close and earn the next close-to-close
return. No short positions, leverage, stop loss, or intra-day execution is
added because the paper does not specify them.

## Accumulation-only model

`allocation.simulate_accumulation_dca` turns the same on-chain premise into a
never-sell accumulation plan instead of a 100% in/out switch:

- each inflow splits into an always-on base tranche and a dry-powder reserve;
- powder is spent only when the MVRV Z-score reaches a tier threshold
  (`+2.0`, `+0.5`, `-0.2`), spending a share of the powder left at that moment;
- base buying pauses at or above Z `+5.0`, so the model stops adding at
  euphoria;
- nothing is ever sold, so risk can only be shaped by purchase price and by the
  cash held.

Two tier modes are compared. `crossing` spends on every downward crossing of a
threshold; `once_per_cycle` lets each tier spend at most once between re-arms
at or above the no-buy level. The ledger carries a diagnostic `tiers_fired`
column because `signal` records only the last tier that fired on a close, so a
close spending two tiers at once would otherwise hide one of them.

```powershell
python models/bitcoin_onchain_cycles/run_accumulation_experiment.py
```

Generated under `research/portfolio_allocation/accumulation_results`:
`performance_report.csv`, `findings.md`, `daily_ledgers.csv.gz`,
`transactions.csv`, `signals.csv`, `pareto_frontier.png`, `equity_curves.png`.

Measured on the paper horizon with identical contributions, the `once_per_cycle`
variants cut average acquisition cost by `6%` to `19%` versus static 12-month
DCA, but only one of the five raised ending wealth: `acc_once_base25_deep`, the
frozen variant, by `+10.7%`. The other four moved wealth by `+3.7%`, `-1.2%`,
`-4.1%`, and `-8.0%`, and `acc_once_base60` actually paid a *higher* average
price (`+1.5%`). Treat "the family beat DCA" as a claim about one variant and one
benchmark schedule, not about the family: see the Benchmark Sensitivity section
of `research/portfolio_allocation/accumulation_results/findings.md`, which shows
the benchmark's own deployment schedule moving the answer more than this edge
does.

They also did **not** cut maximum drawdown: every variant still held about 99%
BTC at the equity trough, so its drawdown converged on Bitcoin's own. A
never-sell accumulator cannot reduce the drawdown of the BTC it already owns;
holding cash through a top requires not deploying the reserve earlier, and
cutting drawdown materially requires either a weight cap or an exit.

The drawdown-ladder round (`run_ladder_experiment.py`) tested a reserve that is
never fully deployed: rungs at `-20%`, `-30%`, `-40%`, `-55%`, `-70%`, and
`-85%` from the running high, one rung per 30 days, each rung once per cycle,
and cumulative deployment capped at `1 - reserve_floor` of all cash received.
It does cut drawdown, but only by giving up most of the wealth. The lowest
drawdown was `ladder_unit40_f05_b20` at `79.66%` against `83.78%` for static DCA,
and it held `30.7%` of DCA's ending value; the variant holding `33.8%`
(`ladder_share_f60_b35`) had a `79.81%` drawdown. An earlier version of this note
paired one variant's drawdown with another's wealth. No ladder variant beat DCA
on wealth, and all of them spent more time underwater than DCA as well
(`20.6%` of days against `19.6%` for the best one).

Cash yield does not repair that gap. `run_ladder_yield_experiment.py` applies
one variable, the yield earned by unspent cash, to every strategy in a scenario,
and the real FRED `DTB3` 3-month bill rate moved the low-drawdown variant from
`$847,040` to `$849,488`, or `+0.29%`. The reason is the drawdown approximation
`portfolio_drawdown ~= (1 - cash_weight_at_peak) x btc_price_drawdown` (it is an
approximation, not an identity: contributions keep buying through the decline, so
the measured residual reaches `0.47` points across the variants — see audit
finding G7): what sets drawdown is cash relative to equity at the peak, and a
budget-relative reserve floor cannot make that share large after a long bull
market.

`run_cost_basis_experiment.py` re-scores the same strategies on the risk measure
that suits an accumulator: equity against every dollar contributed, and price
against the average price paid. On felt loss the frozen baseline loses `53.7%`
at worst against static DCA's `66.5%` and buy-and-hold's `73.7%`, spends `13.0%`
of days underwater against `19.6%` and `21.1%`, and returns `116.6x` on money
put in against `105.2x` and `88.4x`, so it beats both benchmarks on cost-basis
risk as well as on wealth. The ladder's reserve cuts felt loss further, to
`23.4%`, but the terminal multiple drops to `31.6x`; the gap between the two
drawdown columns is the cash cushion, `+47` points for the ladder and zero for
anything fully invested.

## Frozen accumulation rule and the live monitor

One accumulation variant is frozen: `acc_once_base25_deep`, under
`ACCUMULATION_CONTRACT.md`, with its out-of-sample read recorded in
`research/oos_validation/accumulation/`. Its parameters live in exactly one
place, `frozen_accumulation.py`, and every tool that reports the rule reads
them from there, so a report cannot drift from the contract.

`run_daily_monitor.py` used to infer tiers from the last two MVRV Z-scores,
which cannot see the contract's cycle state: the rule lets each tier spend at
most once per cycle and re-arms only at or above Z `+5.00`. Through the whole
2025-2026 decline it therefore kept naming tier thresholds the frozen rule was
not allowed to act on. The monitor now replays the full history with the
reference simulator and reports the replayed state instead:

- `state.cycle` (`armed` or `locked`) and `state.reserve_deployable`;
- `state.tiers_spent_this_cycle` and `state.tiers_pending_this_cycle`;
- `state.reserve_cash_usd` and `state.reserve_last_deployed_date`;
- `state.last_rearm` and `state.mvrv_zscore_distance_to_rearm`;
- `informative_crossing_read`: the old Z-only reading, kept but labelled as
  information that can name a tier the rule cannot use.

On the `2026-09-25` data the report reads: cycle `locked`, reserve `$3,825`
last deployed `2022-06-18`, `4.00` Z below the re-arm level, so the reserve
cannot be spent until Z reaches `+5.00`. The base tranche keeps buying `25%` of
each inflow.

```powershell
python models/bitcoin_onchain_cycles/run_daily_monitor.py            # refresh data, then report
python models/bitcoin_onchain_cycles/run_daily_monitor.py --offline  # rebuild from the prepared CSV
python models/bitcoin_onchain_cycles/run_daily_monitor.py --self-test
```

`--offline` skips Coin Metrics and rebuilds `data/latest_signal.json` from
`data/prepared/daily.csv`; the data age check still applies.

## Run

From the repository root:

```powershell
python models/bitcoin_onchain_cycles/run_backtest.py `
  --btc "C:\Users\adjop\Downloads\btc.csv" `
  --cdd "C:\Users\adjop\Downloads\data.tsv"
```

Generated files:

- `data/prepared/paper_daily.csv`
- `research/paper_replication/results/daily_backtest.csv`
- `research/paper_replication/results/strategy_summary.csv`
- `research/paper_replication/results/performance_report.csv`
- `research/paper_replication/results/threshold_trades.csv`
- `research/paper_replication/results/paper_cvdd_trades.csv`
- `research/paper_replication/results/cvdd_monte_carlo.csv`
- `research/paper_replication/results/equity_curves.png`
- `research/paper_replication/results/hybrid_signals.png`

The paper-specific `strategy_summary.csv` keeps the replication statistics.
The shared `performance_report.csv` adds cash-flow-aware portfolio measures:
ending value and BTC, average BTC purchase price, XIRR, TWR, drawdown and time
underwater, CAGR, Sharpe, Sortino, Calmar, cash weight/drag, turnover,
contributions, and withdrawals.

Prices are Coin Metrics `PriceUSD`, a composite USD reference price rather
than a specific BTCUSD, BTCUSDT, or BTCUSDC exchange market.

## Research tracks

- `research/paper_replication`: frozen `2013-12-07` through `2025-04-12`
  replication and its generated results.
- `research/oos_validation`: untouched observations beginning `2025-04-13`.
  `accumulation/` holds the frozen accumulation model's OOS read.
- `research/portfolio_allocation`: allocation-model design using only the
  paper horizon before it is frozen and evaluated out of sample.
  `accumulation_results/` holds the never-sell DCA experiments.

## Audit

`research/AUDIT_2026-09-26.md` records a full audit of this model: what was
found, what was verified correct, and what was fixed. Two reporting changes show
up in newly generated artifacts. The column `time_underwater_days` was renamed
to `longest_drawdown_streak_days`, with a new companion `underwater_days_total`,
because the old name read like a total while the value was the longest unbroken
streak. The equity chart annotation no longer appends `x` to dollar values;
`write_portfolio_report` takes `ending_value_suffix` for ledgers that really are
growth multiples. CSVs written before this date keep the old column name and are
not rewritten.

## CVDD limitation

The supplied CDD series and Coin Metrics prices calculate a valid CVDD series,
but it is not identical to Bitcoin Magazine Pro's proprietary series used by
the paper. Consequently, the paper's `±1%` CVDD entry test may produce different
dates.

The paper's CVDD return table is reproduced separately with its Appendix A1
entry and evaluation dates. The 2017 and 2021 cycle-peak exits are known only
after the fact. They are useful for evaluating bottom timing but are not
deployable exit signals. A live CVDD strategy therefore remains entry-only
until an independent exit rule is specified.

The hybrid comparison includes both the literal calculated `±1%` CVDD entry
rule and a clearly labeled proxy using the paper's Appendix entry dates. The
calculated series makes zero trades because its closest price/CVDD distance is
about 4.8%; the proxy uses the causal MVRV-6 exit but is not an independent
replication of the proprietary CVDD entries.

CDD is therefore off the live path. `run_daily_monitor.py` and `run_oos.py`
read Coin Metrics only; pass `--with-cdd` (or `--cdd`) to opt back in. Free CDD
alternatives exist (Glassnode and CryptoQuant behind a key, the BGeometrics
`bitcoin-data.com` endpoints without one, but only a rolling ~4-year window),
and self-computing CDD from UTXO-level data remains possible. None of that is
needed to trade NUPL/MVRV.

## Validation

The supplied data reproduces the six complete strategies closely:

| Strategy | Calculated cumulative log return | Paper | Calculated Sharpe | Paper |
| --- | ---: | ---: | ---: | ---: |
| NUPL 1 | 4.84 | 4.77 | 0.83 | 0.82 |
| NUPL 2 | 5.23 | 5.19 | 0.89 | 0.88 |
| NUPL 3 | 7.19 | 7.12 | 1.13 | 1.12 |
| MVRV 1 | 5.85 | 5.81 | 1.02 | 1.01 |
| MVRV 2 | 7.44 | 7.40 | 1.21 | 1.19 |
| MVRV 3 | 8.16 | 8.09 | 1.29 | 1.28 |

Coin Metrics produces threshold-entry dates one calendar day earlier than the
Bitcoin Magazine Pro dates in Appendix A1. The exits match. Buy-and-hold
volatility and Sharpe differ because the paper uses Investing.com prices.

## Readiness

NUPL and MVRV are complete research rules, but the paper specifies no stop
loss, drawdown halt, or execution controls. CVDD has no live exit and is no
longer required by anything on the live path, so the monitor runs on one public
API with no browser automation. The accumulation-only variants are historical
research candidates; freeze one and evaluate it from the OOS boundary before
treating it as live. This package is ready for historical research, not
unattended live trading.
