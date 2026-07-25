# Bitcoin On-Chain Cycles

Daily, long-or-cash Bitcoin strategies from Grobys, Nasman, and Sandretto
(2026), "Using on-chain data to predict Bitcoin cycles."

## Required data

- `btc.csv`: `time`, `PriceUSD`, `CapMrktCurUSD`, and `CapMVRVCur`.
- `data.tsv`: full-history daily Coin Days Destroyed.

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
- `research/portfolio_allocation`: allocation-model design using only the
  paper horizon before it is frozen and evaluated out of sample.

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
loss, drawdown halt, or execution controls. CVDD has no live exit. This package
is ready for historical research, not unattended live trading.
