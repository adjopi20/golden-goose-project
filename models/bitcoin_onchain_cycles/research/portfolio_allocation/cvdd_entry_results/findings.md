# CVDD-Confirmed MVRV Entry

Horizon: `2013-12-07` through `2025-04-12`.
Initial capital: `$10,000.00`. No contributions, fees, or slippage.

Entry requires MVRV below -0.2 and price within the configured band above
the locally calculated CVDD. Tested bands: 5%, 10%, 15%, and 20%.

## Benchmark

- Paper MVRV-3 ending value: `$34,945,142.32`.
- Paper MVRV-3 maximum drawdown: `61.45%`.

## Leaders

- Highest wealth: `cvdd_15_paper_exit` ($71,773,856.13).
- Highest Calmar: `cvdd_15_mvrv_rollover_50` (2.32).
- Lowest drawdown: `cvdd_20_mvrv_rollover_50` (40.75%).
- Full-coverage Pareto candidates: `cvdd_15_paper_exit, cvdd_15_mvrv_rollover_50, cvdd_15_combined_rollover_75`.

The 5% and 10% bands are excluded from leader selection because they
captured only one and two cycle entries respectively.

## Comparison

| Strategy | Entries | Ending value | Max drawdown | Calmar | Wealth vs paper |
| --- | ---: | ---: | ---: | ---: | ---: |
| cvdd_15_paper_exit | 3 | $71,773,856 | 61.45% | 1.93 | +105.39% |
| cvdd_20_paper_exit | 3 | $71,339,095 | 61.45% | 1.93 | +104.15% |
| cvdd_15_combined_rollover_75 | 3 | $40,256,222 | 51.10% | 2.11 | +15.20% |
| cvdd_20_combined_rollover_75 | 3 | $40,012,375 | 51.10% | 2.11 | +14.50% |
| paper_mvrv_3 | 0 | $34,945,142 | 61.45% | 1.71 | +0.00% |
| cvdd_15_mvrv_rollover_50 | 3 | $18,864,407 | 40.75% | 2.32 | -46.02% |
| cvdd_20_mvrv_rollover_50 | 3 | $18,750,138 | 40.75% | 2.31 | -46.34% |
| buy_hold | 0 | $1,211,644 | 83.78% | 0.63 | -96.53% |
| cvdd_10_paper_exit | 2 | $944,213 | 61.45% | 0.80 | -97.30% |
| cvdd_10_combined_rollover_75 | 2 | $697,360 | 51.10% | 0.89 | -98.00% |
| cvdd_10_mvrv_rollover_50 | 2 | $478,322 | 40.75% | 1.00 | -98.63% |
| cvdd_05_paper_exit | 1 | $54,043 | 28.05% | 0.57 | -99.85% |
| cvdd_05_combined_rollover_75 | 1 | $50,140 | 24.01% | 0.64 | -99.86% |
| cvdd_05_mvrv_rollover_50 | 1 | $46,237 | 21.68% | 0.67 | -99.87% |

The 15% and 20% bands both captured all three cycles and produced very
similar results. The sharp failure below 15% is still a threshold cliff:
the 2015 trough was 13.81% above locally calculated CVDD.

These bands are local sensitivity tests, not paper-reported CVDD rules or
frozen live parameters.
