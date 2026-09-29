# BNBUSDC — TF-PRENY-BNB-001

Recorded 2026-09-27. **Paper candidate with weak/small-sample evidence; not live-approved.** Incorporates [COMMON_V1.md](COMMON_V1.md).

## 1. Entry and exit

- Baseline first supported candidate + **C2 initiative** at the signal (`c2_initiative`). No absorption-only/persistence-only union, no additional EMA/ADX entry gate.
- TP1: **10% at 2R**. Remaining 90%: original POC stop or next-day 08:59 NY. No trail/breakeven/re-entry.

## 2. Risk and reward

- Initial stop = Pre-NY POC; D and R from actual fill; notional/equity cap 5. TP1 is not a guaranteed net-profit floor.

## 3. Position sizing

- Completed 15m Wilder **ADX(14) ≥ 30**: risk **0.50%** current allocated equity.
- ADX < 30: risk **0.25%**. ADX has no direction and does not reverse/reject the baseline signal.
- Research missing-feature fallback: **0.50%**, flagged; live data-health interlock requires separate validation.
- No EMA or volatility sizing combination; no intratrade resize.

## Evidence and fragility

Selected-sizing cohort: **25 trades**, 2026-06-01–2026-08-30; only **8 full-size trades**. Initial 1,000 → 1,071.63; +7.16%; money PF 2.441; WR 40.0%; net expectancy +0.5502R/trade; realized max DD 2.71%.

Same 2R/10% cohort: fixed 0.5% sizing DD **3.66%**; train-risk-matched fixed sizing DD **2.44%**. Those are different controls. ADX sizing reduced DD versus full fixed risk, but increased DD versus risk-matched control.

June/July negative, August positive; only one of three positive months. Early 12 trades lost money; later 13 produced the gain. Missing the largest winner leaves approximately **+0.40%** return. Combined stress +2.24% is not proof of safety: the small sample still contains its key winner.

Retain the requested ADX policy as an explicitly fragile paper hypothesis, not a reason to increase capital. Row key `BNBUSDC__c2_initiative__time_exit__tp1_fraction_0p1__tp1_r_2`, policy `indicator_sizing`. See [checkpoint](README.md).
