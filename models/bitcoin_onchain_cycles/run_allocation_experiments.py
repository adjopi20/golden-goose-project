from __future__ import annotations

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics.backtest_report import write_portfolio_report
from models.bitcoin_onchain_cycles.allocation import (
    allocation_variants,
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_static_dca,
)
from models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_REPORTED, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent
REPRESENTATIVE_TIERS = ("tier_75_50_0", "tier_75_50_25", "tier_50_25_0")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare compounded MVRV allocation variants on the paper horizon"
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "paper_daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "portfolio_allocation" / "results",
    )
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=100.0)
    parser.add_argument("--static-dca-months", type=int, default=12)
    args = parser.parse_args()

    daily = _load_paper_daily(args.prepared)
    variants = allocation_variants()
    args.output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"strategy": name, "weight_at_5": w5, "weight_at_6": w6, "weight_at_7": w7}
            for name, (w5, w6, w7) in variants.items()
        ]
    ).to_csv(args.output / "allocation_variants.csv", index=False)

    lump_sum = {
        "buy_hold": simulate_buy_and_hold(
            daily, initial_capital=args.initial_capital
        ),
        **{
            name: simulate_mvrv_allocation(
                daily, weights, initial_capital=args.initial_capital
            )
            for name, weights in variants.items()
        },
    }
    accumulation = {
        "buy_hold_contributions": simulate_buy_and_hold(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
        ),
        "static_dca": simulate_static_dca(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
            deployment_months=args.static_dca_months,
        ),
        **{
            name: simulate_mvrv_allocation(
                daily,
                weights,
                initial_capital=args.initial_capital,
                monthly_contribution=args.monthly_contribution,
            )
            for name, weights in variants.items()
        },
    }

    lump_report = _write_experiment(
        lump_sum,
        args.output / "tactical_lump_sum",
        chart_benchmark="buy_hold",
        selection_benchmark="mvrv_3",
        title="MVRV Tactical Lump-Sum Allocation",
    )
    accumulation_report = _write_experiment(
        accumulation,
        args.output / "dynamic_accumulation",
        chart_benchmark="buy_hold_contributions",
        selection_benchmark="mvrv_3",
        title="MVRV Dynamic Accumulation",
    )
    _write_findings(
        args.output / "findings.md",
        lump_report,
        accumulation_report,
        args.initial_capital,
        args.monthly_contribution,
        args.static_dca_months,
    )
    print(f"Variants: {len(variants)}")
    print(f"Outputs: {args.output.resolve()}")


def _write_experiment(
    ledgers: dict[str, pd.DataFrame],
    output: Path,
    *,
    chart_benchmark: str,
    selection_benchmark: str,
    title: str,
) -> pd.DataFrame:
    groups = {
        "Paper MVRV rules": ("mvrv_1", "mvrv_2", "mvrv_3"),
        "Representative tiered allocations": REPRESENTATIVE_TIERS,
    }
    if "static_dca" in ledgers:
        groups = {
            "Contribution benchmarks": ("static_dca", "mvrv_3"),
            "Representative tiered allocations": REPRESENTATIVE_TIERS,
        }
    report = write_portfolio_report(
        ledgers,
        output,
        benchmark=chart_benchmark,
        groups=groups,
        labels={
            "buy_hold": "Buy & Hold",
            "buy_hold_contributions": "Buy & Hold + Contributions",
            "static_dca": "Static 12-Month DCA",
            "mvrv_1": "Paper MVRV-1",
            "mvrv_2": "Paper MVRV-2",
            "mvrv_3": "Paper MVRV-3",
            "tier_75_50_0": "75/50/0",
            "tier_75_50_25": "75/50/25",
            "tier_50_25_0": "50/25/0",
        },
        title=title,
    )
    report = _add_allocation_metrics(report, ledgers, selection_benchmark)
    report.sort_values(
        ["pareto_optimal", "ending_portfolio_value"],
        ascending=[False, False],
    ).to_csv(output / "performance_report.csv", index=False)
    _write_ledgers(ledgers, output)
    _save_pareto(report, output / "pareto_frontier.png", title)
    return report


def _add_allocation_metrics(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
    benchmark: str,
) -> pd.DataFrame:
    enriched = report.copy()
    enriched["ending_cash"] = enriched["strategy"].map(
        lambda name: float(ledgers[name]["cash"].iloc[-1])
    )
    enriched["btc_equivalent_total_wealth"] = enriched["strategy"].map(
        lambda name: float(
            ledgers[name]["equity"].iloc[-1] / ledgers[name]["asset_price"].iloc[-1]
        )
    )
    enriched["number_of_transactions"] = enriched["strategy"].map(
        lambda name: int(ledgers[name]["transaction_count"].sum())
    )
    enriched["maximum_drawdown_abs"] = enriched["maximum_drawdown"].abs()
    enriched["pareto_optimal"] = _pareto_mask(enriched)

    reference = enriched.loc[enriched["strategy"].eq(benchmark)].iloc[0]
    enriched["dominates_mvrv_3"] = (
        enriched["ending_portfolio_value"].ge(reference["ending_portfolio_value"])
        & enriched["maximum_drawdown_abs"].le(reference["maximum_drawdown_abs"])
        & (
            enriched["ending_portfolio_value"].gt(reference["ending_portfolio_value"])
            | enriched["maximum_drawdown_abs"].lt(reference["maximum_drawdown_abs"])
        )
    )
    enriched["within_mvrv_3_drawdown"] = enriched["maximum_drawdown_abs"].le(
        reference["maximum_drawdown_abs"]
    )
    return enriched


def _pareto_mask(report: pd.DataFrame) -> pd.Series:
    wealth = report["ending_portfolio_value"].to_numpy(dtype=float)
    risk = report["maximum_drawdown"].abs().to_numpy(dtype=float)
    optimal = np.ones(len(report), dtype=bool)
    for index in range(len(report)):
        dominated = (
            (wealth >= wealth[index] * (1.0 - 1e-12))
            & (risk <= risk[index] + 1e-12)
            & (
                (wealth > wealth[index] * (1.0 + 1e-12))
                | (risk < risk[index] - 1e-12)
            )
        )
        optimal[index] = not dominated.any()
    return pd.Series(optimal, index=report.index)


def _write_ledgers(ledgers: dict[str, pd.DataFrame], output: Path) -> None:
    long = pd.concat(
        [ledger.assign(strategy=name) for name, ledger in ledgers.items()],
        ignore_index=True,
    )
    long.to_csv(output / "daily_ledgers.csv.gz", index=False, compression="gzip")
    long.loc[long["transaction_count"].gt(0)].to_csv(
        output / "transactions.csv", index=False
    )


def _save_pareto(report: pd.DataFrame, output: Path, title: str) -> None:
    fig, axis = plt.subplots(figsize=(10, 7))
    regular = report.loc[~report["pareto_optimal"]]
    frontier = report.loc[report["pareto_optimal"]].sort_values("maximum_drawdown_abs")
    axis.scatter(
        regular["maximum_drawdown_abs"],
        regular["ending_portfolio_value"],
        color="#9ca3af",
        alpha=0.65,
        label="Dominated",
    )
    axis.plot(
        frontier["maximum_drawdown_abs"],
        frontier["ending_portfolio_value"],
        marker="o",
        color="#2563eb",
        label="Pareto frontier",
    )
    for row in frontier.itertuples():
        axis.annotate(
            row.strategy,
            (row.maximum_drawdown_abs, row.ending_portfolio_value),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=8,
        )
    axis.set_xlabel("Maximum drawdown")
    axis.set_ylabel("Ending portfolio value")
    axis.set_yscale("log")
    axis.set_title(title, loc="left", fontweight="bold")
    axis.grid(color="#e5e7eb", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _write_findings(
    output: Path,
    lump: pd.DataFrame,
    accumulation: pd.DataFrame,
    initial_capital: float,
    monthly_contribution: float,
    dca_months: int,
) -> None:
    paper_log_return, paper_sharpe = PAPER_REPORTED["mvrv_3"]
    lines = [
        "# MVRV Allocation Experiments",
        "",
        f"Paper horizon: `{PAPER_START}` through `{PAPER_END}`.",
        "",
        "All results are in-sample allocation research using only MVRV Z-score.",
        "Signals are applied causally to the following close-to-close return.",
        "Cash earns 0%; fees and slippage are excluded to match the paper.",
        "",
        "## Paper Benchmark",
        "",
        f"- Reported MVRV-3 cumulative log return: `{paper_log_return:.2f}`.",
        f"- Reported MVRV-3 ending wealth multiple: `{np.exp(paper_log_return):,.2f}x`.",
        f"- Reported MVRV-3 Sharpe ratio: `{paper_sharpe:.2f}`.",
        "- Local MVRV-3 is the apples-to-apples benchmark for allocation variants.",
        "",
        "## Capital Assumptions",
        "",
        f"- Initial capital: `${initial_capital:,.2f}`.",
        f"- Monthly contribution: `${monthly_contribution:,.2f}`.",
        f"- Static DCA deploys initial capital over `{dca_months}` months.",
        "",
        "## Tactical Lump-Sum Leaders",
        "",
        _leader_lines(lump),
        "",
        "## Dynamic Accumulation Leaders",
        "",
        _leader_lines(accumulation),
        "",
        "A Pareto-optimal result is not automatically a future optimum. This run",
        "selects historical candidates on the same horizon used by the paper.",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _leader_lines(report: pd.DataFrame) -> str:
    highest_wealth = report.loc[report["ending_portfolio_value"].idxmax()]
    highest_calmar = report.loc[report["calmar_ratio"].idxmax()]
    eligible = report.loc[report["within_mvrv_3_drawdown"]]
    constrained = eligible.loc[eligible["ending_portfolio_value"].idxmax()]
    return "\n".join(
        [
            f"- Highest wealth: `{highest_wealth['strategy']}` "
            f"(${highest_wealth['ending_portfolio_value']:,.2f}, "
            f"{highest_wealth['maximum_drawdown_abs']:.2%} drawdown).",
            f"- Highest Calmar: `{highest_calmar['strategy']}` "
            f"({highest_calmar['calmar_ratio']:.2f}).",
            f"- Highest wealth within MVRV-3 drawdown: `{constrained['strategy']}` "
            f"(${constrained['ending_portfolio_value']:,.2f}).",
            f"- Pareto candidates: "
            f"`{', '.join(report.loc[report['pareto_optimal'], 'strategy'])}`.",
        ]
    )


def _load_paper_daily(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run models/bitcoin_onchain_cycles/run_backtest.py first"
        )
    daily = pd.read_csv(path, parse_dates=["date"])
    expected = {"date", "close", "mvrv_zscore"}
    missing = expected - set(daily.columns)
    if missing:
        raise ValueError(f"prepared data missing columns: {', '.join(sorted(missing))}")
    start, end = pd.Timestamp(PAPER_START), pd.Timestamp(PAPER_END)
    daily = daily.loc[daily["date"].between(start, end)].reset_index(drop=True)
    if daily.empty or daily["date"].iloc[0] != start or daily["date"].iloc[-1] != end:
        raise ValueError("prepared data does not cover the complete paper horizon")
    return daily


if __name__ == "__main__":
    main()
