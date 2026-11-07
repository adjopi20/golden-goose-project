"""Cost-basis frontier over the recorded in-sample accumulation variants.

Descriptive only: it reads the recorded ledgers, computes the cost-basis metrics
for every variant, and reports the trade-off between felt loss and ending wealth.
No rule is changed, no parameter is chosen, and nothing is re-simulated.

Run from the repository root:
    python model_development/models/bitcoin_onchain_cycles/research/audit_2026-09-26/felt_loss_frontier.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.phase_3.portfolio_report import calculate_portfolio_metrics  # noqa: E402
from model_development.models.bitcoin_onchain_cycles.cost_basis import cost_basis_metrics  # noqa: E402

LEDGERS = (
    "model_development/models/bitcoin_onchain_cycles/research/portfolio_allocation/"
    "accumulation_results/daily_ledgers.csv.gz"
)


def main() -> None:
    ledgers = pd.read_csv(LEDGERS, parse_dates=["date"])
    rows = []
    for name, ledger in ledgers.groupby("strategy"):
        ledger = ledger.sort_values("date").reset_index(drop=True)
        portfolio = calculate_portfolio_metrics(ledger)
        basis = cost_basis_metrics(ledger)
        rows.append(
            {
                "strategy": name,
                "ending_value": portfolio["ending_portfolio_value"],
                "felt_loss": basis["worst_equity_vs_contributions"],
                "underwater_share": basis["equity_underwater_share"],
                "max_dd": abs(portfolio["maximum_drawdown"]),
                "average_cash_weight": portfolio["average_cash_weight"],
            }
        )
    table = pd.DataFrame(rows).sort_values("felt_loss", ascending=False)

    dca = table.loc[table["strategy"].eq("static_dca")].iloc[0]
    hold = table.loc[table["strategy"].eq("buy_hold_contributions")].iloc[0]
    print(
        f"Benchmarks: static DCA felt loss {dca['felt_loss']:.2%} on "
        f"${dca['ending_value']:,.0f}; buy-and-hold {hold['felt_loss']:.2%} on "
        f"${hold['ending_value']:,.0f}"
    )
    print()
    print(
        f"{'strategy':32s} {'felt loss':>10s} {'vs DCA':>9s} {'ending value':>16s} "
        f"{'vs DCA':>9s} {'max DD':>8s} {'avg cash':>9s}"
    )
    for row in table.itertuples():
        print(
            f"{row.strategy:32s} {row.felt_loss:>10.2%} "
            f"{row.felt_loss - dca['felt_loss']:>+9.2%} {row.ending_value:>16,.0f} "
            f"{row.ending_value / dca['ending_value'] - 1.0:>+9.2%} "
            f"{row.max_dd:>8.2%} {row.average_cash_weight:>9.2%}"
        )

    print()
    best_loss = table.loc[table["felt_loss"].idxmax()]
    best_wealth = table.loc[table["ending_value"].idxmax()]
    print(
        f"Best felt loss: {best_loss['strategy']} ({best_loss['felt_loss']:.2%}), "
        f"keeping {best_loss['ending_value'] / dca['ending_value']:.1%} of DCA's wealth"
    )
    print(
        f"Best wealth:    {best_wealth['strategy']} (${best_wealth['ending_value']:,.0f}), "
        f"felt loss {best_wealth['felt_loss']:.2%}"
    )

    # Pareto set in (felt loss higher is better, wealth higher is better), among
    # never-sell variants only: the selling reference dominates both axes and
    # would otherwise collapse the frontier to a single, off-family point.
    eligible = ~table["strategy"].eq("paper_mvrv_3")
    loss = table["felt_loss"].to_numpy(dtype=float)
    wealth = table["ending_value"].to_numpy(dtype=float)
    allowed = eligible.to_numpy(dtype=bool)
    frontier = []
    for index in range(len(table)):
        if not allowed[index]:
            continue
        dominated = (
            allowed
            & (loss >= loss[index] + 1e-12)
            & (wealth >= wealth[index] * (1 + 1e-12))
        ) & ((loss > loss[index] + 1e-12) | (wealth > wealth[index] * (1 + 1e-12)))
        if not dominated.any():
            frontier.append(table["strategy"].iloc[index])
    print(f"Pareto set, never-sell only (felt loss vs wealth): {', '.join(frontier)}")
    reference = table.loc[table["strategy"].eq("paper_mvrv_3")].iloc[0]
    print(
        f"Excluded reference: {reference['strategy']} (sells) has the best felt loss "
        f"({reference['felt_loss']:.2%}) and the best wealth "
        f"(${reference['ending_value']:,.0f}), so it dominates every never-sell "
        "variant on both axes; see the ideation note for why that is not actionable "
        "inside this product."
    )


if __name__ == "__main__":
    main()
