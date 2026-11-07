# Portfolio Allocation Research

> Current corrections: `../CORRECTIONS_2026-09-29.md`. Original result folders
> are preserved, not overwritten. Use `../corrections_2026-09-29/` for repaired
> accumulation, ladder, yield, cost-basis and historical holdout results.
> A retrospective holdout is not automatically proven untouched OOS.

Design and select models only on the paper horizon. After selection, freeze
the rules and evaluate them unchanged from `2025-04-13` onward.

Current MVRV-only comparison:

1. Lump-sum buy-and-hold.
2. Exact paper MVRV-1/2/3.
3. Monotone tiered MVRV allocation at Z-score 5, 6, and 7.
4. Contribution buy-and-hold.
5. Static 12-month deployment of initial capital plus contributions.
6. Binary and tiered MVRV allocation with identical contributions.

All accumulation models must receive identical cash on identical dates.
Compare ending wealth, BTC accumulated, money-weighted return, CAGR, maximum
drawdown, time underwater, Calmar, Sharpe, cash drag, and turnover.

Allocation targets are applied to current total portfolio wealth, so realized
cash and later contributions compound into future entries. Threshold events
rebalance to the new target; contributions buy toward the active target
without forcing sales between threshold events.

Run after generating `data/prepared/paper_daily.csv`:

```powershell
python models/bitcoin_onchain_cycles/run_allocation_experiments.py
```

The fixed-capital rollover experiment is separate from contribution research:

```powershell
python models/bitcoin_onchain_cycles/run_rollover_experiment.py
```

It compares paper MVRV-3 with causal MVRV-only, NUPL-only, and combined
post-peak rollover protection. Signal thresholds remain fixed while combined
rollover allocation is tested at 75%, 50%, and 25% BTC.

CVDD-confirmed entry research is also isolated:

```powershell
python models/bitcoin_onchain_cycles/run_cvdd_entry_experiment.py
```

It requires MVRV below `-0.2` and price within 5%, 10%, 15%, or 20% above the
locally calculated CVDD, then compares the paper exit with the two frozen
rollover exits. It uses fixed capital and no contributions.

Accumulation-only research is separate and needs no CDD:

```powershell
python models/bitcoin_onchain_cycles/run_accumulation_experiment.py
```

`accumulation_results/` compares a never-sell DCA plan with static 12-month DCA
on identical contributions. Inflows split into an always-on base tranche and a
dry-powder reserve; powder is spent only when MVRV Z reaches a tier threshold
(`+2.0`, `+0.5`, `-0.2`), and base buying pauses at or above Z `+5.0`. The plan
never sells, so the only levers are purchase price and cash weight. Read the
result with that constraint in mind: it improves average cost and stack size,
not maximum drawdown. `accumulation_results/FROZEN_BASELINE.md` freezes
`acc_once_base25_deep` as the model to beat.

The benchmark's deployment schedule matters more than the model's edge. Static
DCA spreads the initial capital over a chosen number of months, and that number
does not come from the paper: on the design horizon a 1-month schedule ends at
`$2,110,793`, the 12-month default at `$2,506,687`, and a 24-month schedule at
`$3,334,284`, while the frozen variant ends at `$2,775,255`. The frozen model
therefore beats the 12-month benchmark by `10.7%` and loses to the 24-month
benchmark by `16.8%`, and `deployment_months=1` is exactly
`buy_hold_contributions`, which makes two rows of every table in this family the
same strategy at different speeds. `run_accumulation_experiment.py` now writes
that sweep to `benchmark_sensitivity.csv` and into its findings, and refuses to
let the claim stand unqualified; the recorded `accumulation_results/findings.md`
predates that disclosure (audit finding G1,
`research/AUDIT_2026-09-26.md`).

The drawdown-ladder round is separate and needs no CDD either:

```powershell
python models/bitcoin_onchain_cycles/run_ladder_experiment.py
```

`ladder_results/` tests a reserve that is never fully deployed. Each rung buys
a set size when price falls `20%`, `30%`, `40%`, `55%`, `70%`, or `85%` below
its running high, at most one rung per 30 days, each rung once per cycle, and
cumulative deployment capped at `1 - reserve_floor` of every dollar received.
The results show why a budget-relative floor does not cut drawdown much: what
matters is cash relative to *equity at the peak*, which in a long bull market is
tiny no matter how large the unspent budget is.

The follow-up isolates a single variable, the yield earned by unspent cash:

```powershell
python models/bitcoin_onchain_cycles/run_ladder_yield_experiment.py
```

`yield_results/` applies the same rate to every strategy in a scenario
(constants of 0%, 2%, 4%, and 6%, plus the real FRED `DTB3` 3-month bill rate)
and shows that yield is not the missing lever: the real bill rate moves the
low-drawdown ladder variant from `$847,040` to `$849,488`, closing about 0.3
percentage points of a 66-point wealth gap. Yield lowers the cost of waiting,
but it cannot compensate for not owning BTC through a 100x appreciation, because
the reserve is only large in dollars while the portfolio is still small.

Finally, the same strategies are re-scored on cost-basis risk:

```powershell
python models/bitcoin_onchain_cycles/run_cost_basis_experiment.py
```

`cost_basis_results/` measures equity against every dollar contributed, price
against the average price paid, time spent underwater, and the recovery lag. Two
results stand out. First, the frozen baseline beats static DCA and buy-and-hold
on felt loss (`-53.7%` against `-66.5%` and `-73.7%`), on time underwater
(`13.0%` against `19.6%` and `21.1%`), and on the multiple of money put in
(`117.6x` against `106.2x` and `89.4x`); its only weak spot remains
peak-to-trough equity drawdown, where it is roughly level. Second, the ladder's
reserve does exactly what it was designed to do for felt loss, cutting it to
`-23.4%`, but the terminal multiple falls to `32.6x`, and it spends slightly
more time underwater than the baseline. The gap between the two drawdown columns
is the cash cushion: `+47` points for the ladder, `0` for anything fully
invested.

## Reporting corrections (2026-09-26)

`research/AUDIT_2026-09-26.md` records a full audit of this model. Four
corrections change how the recorded artifacts should be read. The recorded CSVs
still hold the old values; re-running the experiments reproduces every headline
number (verified column by column) with the new columns added.

- `time_underwater_days` is now `longest_drawdown_streak_days`, with
  `underwater_days_total` beside it. The old name read like a total; the value
  was the longest unbroken streak — `1,079` days for the frozen model, against a
  real total of `3,971`.
- `tier_purchases` counted `signal` labels, which record only the last action on
  a close, so a tier that fired alongside another action went uncounted (`109`
  instead of `110` for the crossing variants). Base buys were undercounted the
  same way (`127` instead of `132`). Both now come from the flow, the Z-score,
  and the simulator's own `tiers_fired` log.
- The "rank agreement" between the two risk views was `0.90` only because a
  one-ULP difference in drawdown split a tie into ranks `7` and `8`. Ranking on
  rounded values gives `0.92`.
- Cost-basis metrics are not meaningful for the `sells` row, and the reason the
  old text gave does not apply: the paper rule's `total_withdrawals` is `$0`, so
  nothing shrank the denominator. Its `average_cost_price` is a whole-history
  figure (`$14,198`) while the surviving stack's basis is `$19,017`, and its
  worst price-versus-cost reading happened in 2015 against a basis that was later
  sold.
