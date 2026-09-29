# HYPEUSDT — TF-PRENY-HYPE-001

Recorded 2026-09-27. **Paper candidate; not live-approved.** Exact historical symbol is HYPEUSDT, not HYPEUSDC. Incorporates [COMMON_V1.md](COMMON_V1.md).

## 1. Entry and exit

- Baseline first supported candidate + **C1 delta × weak result exclusion** (`c1_delta`). This is not an unfiltered baseline and not a C2 union.
- At signal, use up to the last four completed 15m evaluation bars from 09:00. Directional delta imbalance = s × sum(buy−sell) / sum(buy+sell). Directional result = s × (latest close−first selected open) / causal completed 1h ATR14.
- Reject only when imbalance ≥ training median AND result < training median. Otherwise retain. Missing research feature/threshold keeps baseline and is flagged. No replacement entry after rejection.
- Calibration method in the source study: 180-day past training, minimum 25 usable candidates, one-day embargo, 60-day test blocks. Save each threshold artifact and validity interval. No outcome optimization or full-sample median.
- TP1: **10% at 1R**; 90% at original POC stop or next-day 08:59 NY. No trailing/breakeven.

## 2. Risk and reward

POC initial stop, R from actual fill, notional/equity cap 5. Do not tighten stop using bubbles or change holding horizon in this version.

## 3. Position sizing

**Fixed 0.50%** of current allocated equity. No volatility, EMA or ADX sizing overlay. Recalculate for each new trade; no intratrade resizing.

## Evidence and calibration handoff

Selected-sizing comparable cohort: 97 trades, 2026-03-01–2026-08-30. Initial 1,000 → 1,232.76; +23.28%; money PF 1.771; net WR 37.11%; expectancy +0.4502R/trade; realized max DD 4.75%. Four of six months positive. Combined adverse stress return −0.47%; missing largest winner +12.46%.

Indicator sizing did not improve the chosen historical trade-off enough to justify complexity. This does not prove HYPE is permanently in a healthy trend or that fixed sizing will always dominate.

Last observed historical C1 artifact: training `[2026-01-29, 2026-07-28)`, test `[2026-07-29, 2026-09-27)`, imbalance median `0.10225915461923937`, result/ATR median `0.8001515880360514`. These are **recorded historical values, not timeless constants**. On/after the test interval's exclusive end, do not silently reuse them indefinitely or fit to future data. Before paper start, export/validate the next causal calibration artifact using the same schedule; if unavailable, record that the historical unknown-threshold fallback is being exercised, and obtain approval for operational handling. This is a handoff requirement, not a new optimized threshold.

Row key `HYPEUSDT__c1_delta__time_exit__tp1_fraction_0p1__tp1_r_1`, policy `fixed_base`. See [checkpoint](README.md).
