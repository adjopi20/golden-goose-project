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
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_rollover_allocation,
)
from models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test conservative MVRV/NUPL rollover protection with fixed capital"
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "paper_daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "portfolio_allocation" / "rollover_results",
    )
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    args = parser.parse_args()

    daily = _load_daily(args.prepared)
    ledgers = {
        "buy_hold": simulate_buy_and_hold(
            daily, initial_capital=args.initial_capital
        ),
        "paper_mvrv_3": simulate_mvrv_allocation(
            daily, (1.0, 1.0, 0.0), initial_capital=args.initial_capital
        ),
        "mvrv_rollover_50": simulate_rollover_allocation(
            daily,
            initial_capital=args.initial_capital,
            rollover_mode="mvrv",
            rollover_target=0.5,
        ),
        "nupl_rollover_50": simulate_rollover_allocation(
            daily,
            initial_capital=args.initial_capital,
            rollover_mode="nupl",
            rollover_target=0.5,
        ),
        "combined_rollover_75": simulate_rollover_allocation(
            daily,
            initial_capital=args.initial_capital,
            rollover_mode="combined",
            rollover_target=0.75,
        ),
        "combined_rollover_50": simulate_rollover_allocation(
            daily,
            initial_capital=args.initial_capital,
            rollover_mode="combined",
            rollover_target=0.5,
        ),
        "combined_rollover_25": simulate_rollover_allocation(
            daily,
            initial_capital=args.initial_capital,
            rollover_mode="combined",
            rollover_target=0.25,
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    report = write_portfolio_report(
        ledgers,
        args.output,
        benchmark="paper_mvrv_3",
        groups={
            "Rollover signal comparison": (
                "mvrv_rollover_50",
                "nupl_rollover_50",
                "combined_rollover_50",
            ),
            "Combined rollover allocation": (
                "combined_rollover_75",
                "combined_rollover_50",
                "combined_rollover_25",
            ),
        },
        labels={
            "paper_mvrv_3": "Paper MVRV-3",
            "mvrv_rollover_50": "MVRV Rollover → 50%",
            "nupl_rollover_50": "NUPL Rollover → 50%",
            "combined_rollover_75": "Combined → 75%",
            "combined_rollover_50": "Combined → 50%",
            "combined_rollover_25": "Combined → 25%",
        },
        title="MVRV/NUPL Rollover Protection",
    )
    report = _enrich_report(report, ledgers)
    report.sort_values("ending_portfolio_value", ascending=False).to_csv(
        args.output / "performance_report.csv", index=False
    )
    _write_ledgers(ledgers, args.output)
    _write_cycle_results(ledgers, args.output / "cycle_results.csv")
    _save_pareto(report, args.output / "pareto_frontier.png")
    _write_findings(
        report,
        ledgers,
        args.output / "findings.md",
        args.initial_capital,
    )
    print(
        report[
            [
                "strategy",
                "ending_portfolio_value",
                "maximum_drawdown",
                "calmar_ratio",
                "pareto_optimal",
            ]
        ].sort_values("ending_portfolio_value", ascending=False).to_string(index=False)
    )
    print(f"\nOutputs: {args.output.resolve()}")


def _enrich_report(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    enriched = report.copy()
    enriched["ending_cash"] = enriched["strategy"].map(
        lambda name: float(ledgers[name]["cash"].iloc[-1])
    )
    enriched["number_of_transactions"] = enriched["strategy"].map(
        lambda name: int(ledgers[name]["transaction_count"].sum())
    )
    enriched["rollover_signals"] = enriched["strategy"].map(
        lambda name: int(ledgers[name].get("signal", pd.Series(dtype=str)).str.endswith("rollover").sum())
    )
    enriched["maximum_drawdown_abs"] = enriched["maximum_drawdown"].abs()
    enriched["pareto_optimal"] = _pareto_mask(enriched)
    benchmark = enriched.loc[enriched["strategy"].eq("paper_mvrv_3")].iloc[0]
    enriched["wealth_vs_paper_mvrv_3"] = (
        enriched["ending_portfolio_value"] / benchmark["ending_portfolio_value"] - 1.0
    )
    enriched["drawdown_improvement_vs_paper_mvrv_3"] = (
        benchmark["maximum_drawdown_abs"] - enriched["maximum_drawdown_abs"]
    )
    return enriched


def _pareto_mask(report: pd.DataFrame) -> pd.Series:
    wealth = report["ending_portfolio_value"].to_numpy(dtype=float)
    risk = report["maximum_drawdown"].abs().to_numpy(dtype=float)
    result = np.ones(len(report), dtype=bool)
    for index in range(len(report)):
        result[index] = not (
            (wealth >= wealth[index] * (1.0 - 1e-12))
            & (risk <= risk[index] + 1e-12)
            & (
                (wealth > wealth[index] * (1.0 + 1e-12))
                | (risk < risk[index] - 1e-12)
            )
        ).any()
    return pd.Series(result, index=report.index)


def _write_ledgers(ledgers: dict[str, pd.DataFrame], output: Path) -> None:
    rows = pd.concat(
        [ledger.assign(strategy=name) for name, ledger in ledgers.items()],
        ignore_index=True,
    )
    rows.to_csv(output / "daily_ledgers.csv.gz", index=False, compression="gzip")
    rows.loc[rows["transaction_count"].gt(0)].to_csv(
        output / "transactions.csv", index=False
    )
    signals = rows.get("signal", pd.Series(index=rows.index, dtype=str)).fillna("hold")
    rows.loc[signals.ne("hold")].to_csv(output / "signals.csv", index=False)


def _write_cycle_results(
    ledgers: dict[str, pd.DataFrame],
    output: Path,
) -> None:
    baseline = ledgers["paper_mvrv_3"]
    starts = list(baseline.loc[baseline["action"].eq("buy"), "date"])
    rows = []
    for cycle, start in enumerate(starts, 1):
        next_start = starts[cycle] if cycle < len(starts) else None
        for strategy, ledger in ledgers.items():
            segment = ledger.loc[ledger["date"].ge(start)]
            if next_start is not None:
                segment = segment.loc[segment["date"].lt(next_start)]
            wealth = segment["equity"].div(segment["equity"].iloc[0])
            drawdown = wealth.div(wealth.cummax()).sub(1.0)
            rows.append(
                {
                    "cycle": cycle,
                    "start_date": start,
                    "end_date": segment["date"].iloc[-1],
                    "strategy": strategy,
                    "return": wealth.iloc[-1] - 1.0,
                    "maximum_drawdown": drawdown.min(),
                    "ending_value": segment["equity"].iloc[-1],
                }
            )
    pd.DataFrame(rows).to_csv(output, index=False)


def _save_pareto(report: pd.DataFrame, output: Path) -> None:
    fig, axis = plt.subplots(figsize=(10, 7))
    frontier = report.loc[report["pareto_optimal"]].sort_values("maximum_drawdown_abs")
    axis.scatter(
        report["maximum_drawdown_abs"],
        report["ending_portfolio_value"],
        color="#9ca3af",
    )
    axis.plot(
        frontier["maximum_drawdown_abs"],
        frontier["ending_portfolio_value"],
        marker="o",
        color="#2563eb",
        label="Pareto frontier",
    )
    for row in report.itertuples():
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
    axis.set_title("Rollover return/drawdown frontier", loc="left", fontweight="bold")
    axis.grid(color="#e5e7eb", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _write_findings(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
    output: Path,
    initial_capital: float,
) -> None:
    paper = report.loc[report["strategy"].eq("paper_mvrv_3")].iloc[0]
    best_calmar = report.loc[report["calmar_ratio"].idxmax()]
    best_drawdown = report.loc[report["maximum_drawdown_abs"].idxmin()]
    best_ledger = ledgers[str(best_drawdown["strategy"])]
    wealth = best_ledger["equity"].div(best_ledger["equity"].iloc[0])
    drawdown = wealth.div(wealth.cummax()).sub(1.0)
    trough_index = drawdown.idxmin()
    peak_index = wealth.loc[:trough_index].idxmax()
    lines = [
        "# MVRV/NUPL Rollover Protection",
        "",
        f"Horizon: `{PAPER_START}` through `{PAPER_END}`.",
        f"Initial capital: `${initial_capital:,.2f}`. No contributions, fees, or slippage.",
        "",
        "Fixed signal parameters:",
        "",
        "- MVRV arm: 2.0.",
        "- NUPL arm: 0.50.",
        "- MVRV rollover: 1.0 below the post-arm running peak.",
        "- NUPL rollover: 0.10 below the post-arm running peak.",
        "- Hard exit: MVRV 7.",
        "- Reset to 100% BTC: MVRV below -0.2.",
        "",
        "## Benchmark",
        "",
        f"- Paper MVRV-3 ending value: `${paper['ending_portfolio_value']:,.2f}`.",
        f"- Paper MVRV-3 maximum drawdown: `{paper['maximum_drawdown_abs']:.2%}`.",
        "",
        "## Leaders",
        "",
        f"- Highest Calmar: `{best_calmar['strategy']}` "
        f"({best_calmar['calmar_ratio']:.2f}).",
        f"- Lowest drawdown: `{best_drawdown['strategy']}` "
        f"({best_drawdown['maximum_drawdown_abs']:.2%}).",
        f"- Pareto candidates: "
        f"`{', '.join(report.loc[report['pareto_optimal'], 'strategy'])}`.",
        "",
        "## Comparison",
        "",
        "| Strategy | Ending value | Max drawdown | Calmar | Wealth vs paper |",
        "| --- | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | ${row.ending_portfolio_value:,.0f} | "
            f"{row.maximum_drawdown_abs:.2%} | {row.calmar_ratio:.2f} | "
            f"{row.wealth_vs_paper_mvrv_3:+.2%} |"
            for row in report.sort_values("ending_portfolio_value", ascending=False).itertuples()
        ],
        "",
        "## Drawdown Floor",
        "",
        f"The lowest observed drawdown remained `{best_drawdown['maximum_drawdown_abs']:.2%}`. "
        f"It ran from `{best_ledger.loc[peak_index, 'date'].date()}` to "
        f"`{best_ledger.loc[trough_index, 'date'].date()}` before any elevated-zone "
        "rollover could arm. Further drawdown reduction therefore requires entry-side "
        "risk control, not a more aggressive top exit.",
        "",
        "These are in-sample rollover hypotheses, not frozen live rules.",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_daily(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run models/bitcoin_onchain_cycles/run_backtest.py first"
        )
    daily = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "close", "mvrv_zscore", "nupl"}
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
