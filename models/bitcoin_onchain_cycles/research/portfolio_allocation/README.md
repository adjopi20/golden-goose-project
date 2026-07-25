# Portfolio Allocation Research

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
