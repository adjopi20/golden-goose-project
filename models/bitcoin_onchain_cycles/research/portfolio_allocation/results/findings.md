# MVRV Allocation Experiments

Paper horizon: `2013-12-07` through `2025-04-12`.

All results are in-sample allocation research using only MVRV Z-score.
Signals are applied causally to the following close-to-close return.
Cash earns 0%; fees and slippage are excluded to match the paper.

## Paper Benchmark

- Reported MVRV-3 cumulative log return: `8.09`.
- Reported MVRV-3 ending wealth multiple: `3,261.69x`.
- Reported MVRV-3 Sharpe ratio: `1.28`.
- Local MVRV-3 is the apples-to-apples benchmark for allocation variants.

## Capital Assumptions

- Initial capital: `$10,000.00`.
- Monthly contribution: `$100.00`.
- Static DCA deploys initial capital over `12` months.

## Tactical Lump-Sum Leaders

- Highest wealth: `mvrv_3` ($34,945,142.32, 61.45% drawdown).
- Highest Calmar: `mvrv_3` (1.71).
- Highest wealth within MVRV-3 drawdown: `mvrv_3` ($34,945,142.32).
- Pareto candidates: `mvrv_3`.

## Dynamic Accumulation Leaders

- Highest wealth: `mvrv_3` ($46,302,018.39, 61.45% drawdown).
- Highest Calmar: `mvrv_3` (1.71).
- Highest wealth within MVRV-3 drawdown: `mvrv_3` ($46,302,018.39).
- Pareto candidates: `mvrv_3`.

A Pareto-optimal result is not automatically a future optimum. This run
selects historical candidates on the same horizon used by the paper.
