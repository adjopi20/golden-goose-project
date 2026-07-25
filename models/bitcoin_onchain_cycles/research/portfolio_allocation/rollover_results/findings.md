# MVRV/NUPL Rollover Protection

Horizon: `2013-12-07` through `2025-04-12`.
Initial capital: `$10,000.00`. No contributions, fees, or slippage.

Fixed signal parameters:

- MVRV arm: 2.0.
- NUPL arm: 0.50.
- MVRV rollover: 1.0 below the post-arm running peak.
- NUPL rollover: 0.10 below the post-arm running peak.
- Hard exit: MVRV 7.
- Reset to 100% BTC: MVRV below -0.2.

## Benchmark

- Paper MVRV-3 ending value: `$34,945,142.32`.
- Paper MVRV-3 maximum drawdown: `61.45%`.

## Leaders

- Highest Calmar: `mvrv_rollover_50` (2.32).
- Lowest drawdown: `mvrv_rollover_50` (40.97%).
- Pareto candidates: `paper_mvrv_3, mvrv_rollover_50, combined_rollover_75`.

## Comparison

| Strategy | Ending value | Max drawdown | Calmar | Wealth vs paper |
| --- | ---: | ---: | ---: | ---: |
| paper_mvrv_3 | $34,945,142 | 61.45% | 1.71 | +0.00% |
| combined_rollover_75 | $27,760,693 | 51.10% | 1.98 | -20.56% |
| mvrv_rollover_50 | $19,449,615 | 40.97% | 2.32 | -44.34% |
| combined_rollover_50 | $18,542,885 | 40.97% | 2.30 | -46.94% |
| nupl_rollover_50 | $14,242,655 | 40.97% | 2.19 | -59.24% |
| combined_rollover_25 | $9,168,134 | 40.97% | 2.01 | -73.76% |
| buy_hold | $1,211,644 | 83.78% | 0.63 | -96.53% |

## Drawdown Floor

The lowest observed drawdown remained `40.97%`. It ran from `2015-01-07` to `2015-01-14` before any elevated-zone rollover could arm. Further drawdown reduction therefore requires entry-side risk control, not a more aggressive top exit.

These are in-sample rollover hypotheses, not frozen live rules.
