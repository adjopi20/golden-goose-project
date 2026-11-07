# Out-of-Sample Validation

Frozen boundary: `2025-04-13`.

Two reads live here, under different capital conventions.

## `results/` — fixed capital, paper long/cash rules

Recorded `2026-07-26` with data through `2026-07-24`. An earlier draft of this
note said `2026-05-23` and 406 days, and claimed all six strategies were scored;
both were wrong. Three strategies are in `results/findings.md`: `buy_hold`,
`paper_mvrv_3`, and `cvdd_15_paper_exit`. None of them changed position inside
the window, so all three carried their already-open BTC forward and their
out-of-sample return equals buy-and-hold.

This read is internally consistent: the ledger scales the boundary holdings to
`$10,000` and zeroes every later flow, so stocks and flows share one unit.

## `accumulation/` — frozen accumulation model, contributions continue

The frozen model against static DCA, buy-and-hold, and the paper rule, starting
with `$10,000` of equity at the boundary and `$100` of contributions on the
first day of each month, with position and rule state carried across the
boundary. Cash is identical for every strategy on every date.

Re-issued `2026-09-26`: the first version scaled the holdings without scaling
the contributions, which made every flow-based number wrong and turned the
differences between strategies into scale noise. See
`research/AUDIT_2026-09-26.md` and section 6 of `ACCUMULATION_CONTRACT.md`.

Thresholds and allocation rules must be frozen before either track is scored.
