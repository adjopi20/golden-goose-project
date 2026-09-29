# ETHUSDC — TF-PRENY-ETH-001

Recorded 2026-09-27. **Paper candidate; not live-approved.** Incorporates [COMMON_V1.md](COMMON_V1.md). Independent model/equity, not combined with BNB/HYPE.

## 1. Entry and exit

- Baseline first 15m supported Pre-NY outside-value candidate, then **C2 initiative OR C2 possible absorption OR C2 SUPPORT_PERSISTING** (`c2_union_i_a_p`). OR means any one qualifies; overlapping labels produce only one trade.
- Possible absorption is an inclusion group in this ETH-specific empirical selector, not an assertion that all absorption is bullish/bearish. Baseline direction remains unchanged.
- No extra C1, MA-stack, ADX or EMA entry filter. No replacement candidate after rejection.
- TP1: **10% at 1R**, explicitly selected by the user on 2026-09-27. Remaining 90%: original POC stop or next-day 08:59 NY time exit. No trailing or breakeven.

## 2. Risk and reward

- POC initial stop; R measured from actual fill. Original stop remains for the remainder, so hitting TP1 does not guarantee a net winning trade.
- Quantity cap: notional/equity ≤ 5. Fees and gaps can exceed planned stop risk.

## 3. Position sizing

Completed **15m EMA200**, not MA200, not 200-day EMA. Directional gap = s × 100 × (signal close / EMA200 − 1).

- Gap ≥ 0.5%: risk **0.50%** current allocated equity.
- Gap < 0.5%: risk **0.25%**.
- Research missing-feature fallback: **0.50%**, flagged. Live data-health policy remains an operational readiness item.
- No ADX/volatility sizing overlay. No intratrade resizing.

## Evidence at this checkpoint

Selected-sizing evaluated cohort: 317 trades, 2024-11-01–2026-08-30. Initial 1,000 → 1,502.24; return +50.22%; money PF 1.506; net WR 35.65%; expectancy +0.2634R/trade; realized max DD 5.88%. Not annualized; after modeled fees, not every live cost.

For this exact 1R/10% choice: combined adverse stress return −14.11%, DD 21.31%; missing largest winner return +35.54%. The 1R choice is a smoother historical trade-off versus 2R/10%, not proof of superior future performance.

Important: EMA sizing affects money-weighted PF/equity, not the net-R win rate of identical trades. Historical comparisons have been inspected repeatedly. This is the most established of the three evidence sets, but still not a live guarantee.

Reference row key: `ETHUSDC__c2_union_i_a_p__time_exit__tp1_fraction_0p1__tp1_r_1`, policy `indicator_sizing`. Source and limitations: [checkpoint](README.md).
