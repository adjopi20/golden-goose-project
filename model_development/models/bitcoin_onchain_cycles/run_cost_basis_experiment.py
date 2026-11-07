from __future__ import annotations

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.phase_3.portfolio_report import calculate_portfolio_metrics
from model_development.models.bitcoin_onchain_cycles.allocation import (
    simulate_accumulation_dca,
    simulate_buy_and_hold,
    simulate_ladder_accumulation,
    simulate_mvrv_allocation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.cost_basis import cost_basis_metrics
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent

# Same paper horizon and identical contributions for every row. `sells` marks
# strategies that are outside the never-sell constraint and are shown only as a
# reference for the denominator effect of withdrawals.
STRATEGIES: dict[str, dict[str, object]] = {
    "static_dca": {"kind": "static"},
    "buy_hold_contributions": {"kind": "buy_hold"},
    "frozen_acc_once_base25_deep": {
        "kind": "accumulation",
        "base_share": 0.25,
        "tiers": ((0.50, 0.50), (0.00, 0.75), (-0.20, 1.00)),
        "tier_mode": "once_per_cycle",
    },
    "acc_threshold_only": {"kind": "accumulation", "base_share": 0.0},
    "ladder_unit40_f05_b20": {
        "kind": "ladder",
        "base_share": 0.20,
        "sizing": "fixed_unit",
        "unit_divisor": 40.0,
        "reserve_floor": 0.05,
    },
    "ladder_share_f60_b35": {
        "kind": "ladder",
        "base_share": 0.35,
        "reserve_floor": 0.60,
    },
    "ladder_share_f40_b35": {
        "kind": "ladder",
        "base_share": 0.35,
        "reserve_floor": 0.40,
    },
    "paper_mvrv_3_with_contributions": {
        "kind": "mvrv",
        "weights": (1.0, 1.0, 0.0),
        "sells": True,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-score the same strategies on cost-basis risk: equity versus "
            "contributions, and price versus the average price paid"
        )
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "paper_daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "portfolio_allocation" / "cost_basis_results",
    )
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=100.0)
    parser.add_argument("--static-dca-months", type=int, default=12)
    args = parser.parse_args()

    daily = _load_paper_daily(args.prepared)
    args.output.mkdir(parents=True, exist_ok=True)
    ledgers = {
        name: _build_ledger(
            daily,
            overrides,
            args.initial_capital,
            args.monthly_contribution,
            args.static_dca_months,
        )
        for name, overrides in STRATEGIES.items()
    }

    rows = []
    for name, ledger in ledgers.items():
        portfolio = calculate_portfolio_metrics(ledger)
        basis = cost_basis_metrics(ledger)
        rows.append(
            {
                "strategy": name,
                "sells": bool(STRATEGIES[name].get("sells", False)),
                # A round-tripping strategy makes the cost-basis columns describe
                # a stack it no longer holds: the average cost is a whole-history
                # figure and the worst price-versus-cost reading can come from a
                # cycle that was sold. Flagged so the columns are not read as if
                # they meant the same thing for every row (audit finding G4).
                "cost_basis_metrics_valid": not bool(
                    STRATEGIES[name].get("sells", False)
                ),
                "ending_portfolio_value": portfolio["ending_portfolio_value"],
                "ending_btc_units": portfolio["ending_btc_units"],
                "equity_max_drawdown": abs(portfolio["maximum_drawdown"]),
                "average_btc_acquisition_price": portfolio[
                    "average_btc_acquisition_price"
                ],
                "total_contributions": basis["total_contributions"],
                "total_withdrawals": basis["total_withdrawals"],
                "ending_profit_vs_contributions": basis[
                    "ending_profit_vs_contributions"
                ],
                "worst_equity_vs_contributions": basis[
                    "worst_equity_vs_contributions"
                ],
                "equity_trough_date": basis["equity_trough_date"],
                "days_to_recover_equity": basis["days_to_recover_equity"],
                "equity_underwater_days": basis["equity_underwater_days"],
                "equity_underwater_share": basis["equity_underwater_share"],
                "equity_longest_underwater_days": basis[
                    "equity_longest_underwater_days"
                ],
                "worst_price_vs_average_cost": basis["worst_price_vs_average_cost"],
                "price_underwater_days": basis["price_underwater_days"],
                "price_underwater_share": basis["price_underwater_share"],
                "price_longest_underwater_days": basis[
                    "price_longest_underwater_days"
                ],
            }
        )
    report = pd.DataFrame(rows)
    # Round before ranking. Two strategies whose drawdowns differ by one ULP
    # would otherwise rank 7 and 8 instead of tying, which turned a near-perfect
    # rank agreement into 0.90 (audit finding G3).
    report["equity_drawdown_rank"] = report["equity_max_drawdown"].round(9).rank()
    report["cost_basis_drawdown_rank"] = (
        report["worst_equity_vs_contributions"].round(9).rank(ascending=False)
    )
    report["rank_change"] = (
        report["equity_drawdown_rank"] - report["cost_basis_drawdown_rank"]
    )
    # Both rank columns are oriented best = 1, so a value near 1 means the two
    # risk views agree on the ordering and a value near -1 means they invert it.
    rank_agreement = float(
        report["equity_drawdown_rank"].corr(
            report["cost_basis_drawdown_rank"], method="spearman"
        )
    )
    report.sort_values("worst_equity_vs_contributions", ascending=False).to_csv(
        args.output / "cost_basis_report.csv", index=False
    )
    _save_charts(report, args.output)
    _write_findings(
        report,
        rank_agreement,
        args.output / "findings.md",
        args.initial_capital,
        args.monthly_contribution,
    )
    print(
        report[
            [
                "strategy",
                "equity_max_drawdown",
                "worst_equity_vs_contributions",
                "worst_price_vs_average_cost",
                "equity_underwater_share",
                "ending_profit_vs_contributions",
                "ending_portfolio_value",
            ]
        ]
        .sort_values("worst_equity_vs_contributions", ascending=False)
        .to_string(index=False)
    )
    print(f"\nRank agreement between the two risk views (best = 1): {rank_agreement:.2f}")
    print(f"Outputs: {args.output.resolve()}")


def _build_ledger(
    daily: pd.DataFrame,
    overrides: dict[str, object],
    initial_capital: float,
    monthly_contribution: float,
    static_dca_months: int,
) -> pd.DataFrame:
    kind = overrides["kind"]
    if kind == "static":
        return simulate_static_dca(
            daily,
            initial_capital=initial_capital,
            monthly_contribution=monthly_contribution,
            deployment_months=static_dca_months,
        )
    if kind == "buy_hold":
        return simulate_buy_and_hold(
            daily,
            initial_capital=initial_capital,
            monthly_contribution=monthly_contribution,
        )
    if kind == "mvrv":
        return simulate_mvrv_allocation(
            daily,
            overrides["weights"],  # type: ignore[arg-type]
            initial_capital=initial_capital,
            monthly_contribution=monthly_contribution,
        )
    kwargs = {key: value for key, value in overrides.items() if key != "kind"}
    divisor = kwargs.pop("unit_divisor", None)
    if divisor is not None:
        kwargs["unit_notional"] = initial_capital / float(divisor)
    simulator = (
        simulate_accumulation_dca if kind == "accumulation" else simulate_ladder_accumulation
    )
    return simulator(
        daily,
        initial_capital=initial_capital,
        monthly_contribution=monthly_contribution,
        **kwargs,
    )


def _save_charts(report: pd.DataFrame, output: Path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(14, 6.5))
    for axis, (x_column, y_column, x_label, y_label, title) in zip(
        (left, right),
        (
            (
                "equity_max_drawdown",
                "worst_equity_vs_contributions",
                "Maximum drawdown of portfolio equity",
                "Worst equity versus contributions",
                "Does the ranking change?",
            ),
            (
                "equity_underwater_share",
                "ending_profit_vs_contributions",
                "Share of days below contributions",
                "Ending profit versus contributions",
                "Felt loss against final gain",
            ),
        ),
    ):
        for row in report.itertuples():
            axis.scatter(
                getattr(row, x_column),
                getattr(row, y_column),
                marker="s" if row.sells else "o",
                color="#dc2626" if row.sells else "#2563eb",
                s=55,
            )
            axis.annotate(
                row.strategy,
                (getattr(row, x_column), getattr(row, y_column)),
                xytext=(5, 5),
                textcoords="offset points",
                fontsize=7.5,
            )
        axis.set_xlabel(x_label)
        axis.set_ylabel(y_label)
        axis.set_title(title, loc="left", fontweight="bold")
        axis.grid(color="#e5e7eb", linewidth=0.8)
        axis.spines[["top", "right"]].set_visible(False)
    left.axhline(0.0, color="#9ca3af", linewidth=0.8, linestyle="--")
    right.axhline(0.0, color="#9ca3af", linewidth=0.8, linestyle="--")
    fig.suptitle(
        "Bitcoin on-chain accumulation: cost-basis risk view", fontweight="bold"
    )
    fig.tight_layout()
    fig.savefig(output / "cost_basis_view.png", dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _write_findings(
    report: pd.DataFrame,
    rank_agreement: float,
    output: Path,
    initial_capital: float,
    monthly_contribution: float,
) -> None:
    ranked = report.sort_values("worst_equity_vs_contributions", ascending=False)
    best_basis = ranked.iloc[0]
    best_equity = report.loc[report["equity_max_drawdown"].idxmin()]
    best_time = report.loc[report["equity_underwater_share"].idxmin()]
    dca = report.loc[report["strategy"].eq("static_dca")].iloc[0]
    frozen = report.loc[report["strategy"].eq("frozen_acc_once_base25_deep")].iloc[0]
    ladder = report.loc[report["strategy"].eq("ladder_share_f60_b35")].iloc[0]
    selling = report.loc[report["sells"]]
    if len(selling):
        sold_name = str(selling.iloc[0]["strategy"])
        sold_withdrawals = float(selling.iloc[0]["total_withdrawals"])
    else:
        sold_name, sold_withdrawals = "no row sells", 0.0
    lines = [
        "# Cost-Basis Risk View",
        "",
        f"Horizon: `{PAPER_START}` through `{PAPER_END}`.",
        f"Initial capital: `${initial_capital:,.2f}`; "
        f"monthly contribution: `${monthly_contribution:,.2f}`.",
        "Cash earns 0%; fees and slippage are excluded. Every strategy receives",
        "identical cash on identical dates, which is what makes the contribution",
        "denominator comparable. The OOS boundary is untouched.",
        "",
        "## Metrics",
        "",
        "- `equity_max_drawdown`: the existing peak-to-trough measure of the",
        "  portfolio's own equity curve. It is what a risk desk would call risk.",
        "- `worst_equity_vs_contributions`: the deepest point where portfolio",
        "  equity sat below every dollar put in. This is the paper loss an",
        "  accumulator actually feels, and it is the metric that improves simply",
        "  by buying cheaper.",
        "- `worst_price_vs_average_cost`: how far market price traded below the",
        "  average price paid for the stack.",
        "- `equity_underwater_share`: share of days below contributions;",
        "  `days_to_recover_equity`: how long the deepest shortfall took to close.",
        "",
        "Rows marked `sells` are flagged `cost_basis_metrics_valid = False`. They",
        "violate the never-sell constraint, and the cost-basis columns do not mean",
        "the same thing for them: `average_cost_price` is a whole-history figure",
        "while the surviving stack's basis can be much higher, and the worst",
        "price-versus-cost reading can come from a cycle that was sold.",
        f"`{sold_name}` has `total_withdrawals = ${sold_withdrawals:,.2f}`, so the",
        "old note that withdrawals shrink the denominator does not apply here.",
        "",
        "## Leaders",
        "",
        f"- Best cost-basis loss: `{best_basis['strategy']}` "
        f"({best_basis['worst_equity_vs_contributions']:.2%} below contributions).",
        f"- Best equity drawdown: `{best_equity['strategy']}` "
        f"({best_equity['equity_max_drawdown']:.2%}).",
        f"- Least time under contributions: `{best_time['strategy']}` "
        f"({best_time['equity_underwater_share']:.1%} of days).",
        f"- Rank agreement between the two risk views, both oriented best = 1: "
        f"`{rank_agreement:.2f}`.",
        "",
        "## Comparison",
        "",
        "| Strategy | Sells | Equity max DD | Worst equity vs contributions | Worst price vs avg cost | "
        "Days underwater | Share underwater | Longest | Recovery days | Ending profit |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | {'yes' if row.sells else 'no'} | "
            f"{row.equity_max_drawdown:.2%} | "
            f"{row.worst_equity_vs_contributions:.2%} | "
            f"{row.worst_price_vs_average_cost:.2%} | "
            f"{row.equity_underwater_days} | {row.equity_underwater_share:.1%} | "
            f"{row.equity_longest_underwater_days} | "
            f"{'never' if pd.isna(row.days_to_recover_equity) else int(row.days_to_recover_equity)} | "
            f"{row.ending_profit_vs_contributions:+.1%} |"
            for row in ranked.itertuples()
        ],
        "",
        "## What This Shows",
        "",
        f"- Static DCA fell `{abs(dca['worst_equity_vs_contributions']):.2%}` below its own",
        f"  contributions and spent `{dca['equity_underwater_share']:.1%}` of the sample",
        "  underwater.",
        f"- The frozen baseline fell `{abs(frozen['worst_equity_vs_contributions']):.2%}`",
        f"  below contributions and spent `{frozen['equity_underwater_share']:.1%}` of the",
        "  sample underwater.",
        f"- The low-equity-drawdown ladder fell "
        f"`{abs(ladder['worst_equity_vs_contributions']):.2%}` below contributions and",
        f"  spent `{ladder['equity_underwater_share']:.1%}` of the sample underwater.",
        "",
        "## Cash Cushion",
        "",
        "The gap between the two drawdown columns is how much cash absorbed the",
        "fall. When the two are identical, the strategy was fully invested and had",
        "no cushion at all.",
        "",
        "| Strategy | Felt loss | Price vs average cost | Cushion |",
        "| --- | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | {row.worst_equity_vs_contributions:.2%} | "
            f"{row.worst_price_vs_average_cost:.2%} | {row.cushion:+.2%} |"
            for row in report.assign(
                cushion=report["worst_equity_vs_contributions"]
                - report["worst_price_vs_average_cost"]
            )
            .sort_values("cushion", ascending=False)
            .itertuples()
        ],
        "",
        "## Caveats",
        "",
        "- Cost-basis drawdown is not spendable risk. It is the investor's felt",
        "  loss and the thing that decides whether a plan survives contact with a",
        "  bear market, but it cannot be used as a stop or a margin limit.",
        "- It improves mechanically while contributions are still large relative",
        "  to the portfolio, so only compare strategies fed identical cash.",
        "- A strategy that never falls below contributions may simply have bought",
        "  late, so read it together with ending wealth, not instead of it.",
        "",
        "These are historical research candidates, not live trading rules.",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_paper_daily(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run model_development/models/bitcoin_onchain_cycles/run_backtest.py first"
        )
    daily = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "close", "mvrv_zscore"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"prepared data missing columns: {', '.join(sorted(missing))}")
    start, end = pd.Timestamp(PAPER_START), pd.Timestamp(PAPER_END)
    daily = daily.loc[daily["date"].between(start, end)].reset_index(drop=True)
    if daily.empty or daily["date"].iloc[0] != start or daily["date"].iloc[-1] != end:
        raise ValueError("prepared data does not cover the complete paper horizon")
    return daily


if __name__ == "__main__":
    main()
