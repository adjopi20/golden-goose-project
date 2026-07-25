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
    simulate_cvdd_confirmed_allocation,
    simulate_mvrv_allocation,
)
from models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent
CVDD_BANDS = (0.05, 0.10, 0.15, 0.20)
EXIT_MODES = ("paper", "mvrv_rollover_50", "combined_rollover_75")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test CVDD-confirmed MVRV entries with fixed exit rules"
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "paper_daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "portfolio_allocation" / "cvdd_entry_results",
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
    }
    for exit_mode in EXIT_MODES:
        for band in CVDD_BANDS:
            name = _strategy_name(exit_mode, band)
            ledgers[name] = simulate_cvdd_confirmed_allocation(
                daily,
                initial_capital=args.initial_capital,
                cvdd_band=band,
                exit_mode=exit_mode,
            )

    args.output.mkdir(parents=True, exist_ok=True)
    report = write_portfolio_report(
        ledgers,
        args.output,
        benchmark="paper_mvrv_3",
        groups={
            "CVDD entry + paper MVRV-3 exit": tuple(
                _strategy_name("paper", band) for band in CVDD_BANDS
            ),
            "CVDD entry + MVRV rollover to 50%": tuple(
                _strategy_name("mvrv_rollover_50", band) for band in CVDD_BANDS
            ),
            "CVDD entry + combined rollover to 75%": tuple(
                _strategy_name("combined_rollover_75", band) for band in CVDD_BANDS
            ),
        },
        labels={
            "paper_mvrv_3": "Paper MVRV-3",
            **{
                _strategy_name(mode, band): f"{int(band * 100)}% CVDD"
                for mode in EXIT_MODES
                for band in CVDD_BANDS
            },
        },
        title="CVDD-Confirmed MVRV Entry",
    )
    report = _enrich_report(report, ledgers)
    report.sort_values("ending_portfolio_value", ascending=False).to_csv(
        args.output / "performance_report.csv", index=False
    )
    _write_ledgers(ledgers, args.output)
    _save_pareto(report, args.output / "pareto_frontier.png")
    _write_findings(report, args.output / "findings.md", args.initial_capital)
    print(
        report[
            [
                "strategy",
                "ending_portfolio_value",
                "maximum_drawdown",
                "calmar_ratio",
                "cvdd_entries",
                "pareto_full_coverage",
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
    enriched["cvdd_entries"] = enriched["strategy"].map(
        lambda name: int(
            ledgers[name].get("signal", pd.Series(dtype=str)).eq("cvdd_entry").sum()
        )
    )
    enriched["average_entry_price_to_cvdd"] = enriched["strategy"].map(
        lambda name: _average_entry_ratio(ledgers[name])
    )
    enriched["maximum_drawdown_abs"] = enriched["maximum_drawdown"].abs()
    enriched["pareto_optimal"] = _pareto_mask(enriched)
    enriched["full_cycle_coverage"] = enriched["cvdd_entries"].ge(3) | enriched[
        "strategy"
    ].isin(["paper_mvrv_3", "buy_hold"])
    enriched["pareto_full_coverage"] = False
    eligible = enriched.loc[enriched["full_cycle_coverage"]]
    enriched.loc[eligible.index, "pareto_full_coverage"] = _pareto_mask(eligible)
    benchmark = enriched.loc[enriched["strategy"].eq("paper_mvrv_3")].iloc[0]
    enriched["wealth_vs_paper_mvrv_3"] = (
        enriched["ending_portfolio_value"] / benchmark["ending_portfolio_value"] - 1.0
    )
    enriched["drawdown_improvement_vs_paper_mvrv_3"] = (
        benchmark["maximum_drawdown_abs"] - enriched["maximum_drawdown_abs"]
    )
    return enriched


def _average_entry_ratio(ledger: pd.DataFrame) -> float:
    if "signal" not in ledger or "price_to_cvdd" not in ledger:
        return np.nan
    entries = ledger.loc[ledger["signal"].eq("cvdd_entry"), "price_to_cvdd"]
    return float(entries.mean()) if len(entries) else np.nan


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


def _save_pareto(report: pd.DataFrame, output: Path) -> None:
    fig, axis = plt.subplots(figsize=(11, 7))
    eligible = report.loc[report["full_cycle_coverage"]]
    frontier = eligible.loc[eligible["pareto_full_coverage"]].sort_values(
        "maximum_drawdown_abs"
    )
    axis.scatter(
        eligible["maximum_drawdown_abs"],
        eligible["ending_portfolio_value"],
        color="#9ca3af",
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
    axis.set_title("CVDD entry return/drawdown frontier", loc="left", fontweight="bold")
    axis.grid(color="#e5e7eb", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _write_findings(
    report: pd.DataFrame,
    output: Path,
    initial_capital: float,
) -> None:
    paper = report.loc[report["strategy"].eq("paper_mvrv_3")].iloc[0]
    eligible = report.loc[report["full_cycle_coverage"]]
    best_calmar = eligible.loc[eligible["calmar_ratio"].idxmax()]
    best_drawdown = eligible.loc[eligible["maximum_drawdown_abs"].idxmin()]
    highest_wealth = eligible.loc[eligible["ending_portfolio_value"].idxmax()]
    lines = [
        "# CVDD-Confirmed MVRV Entry",
        "",
        f"Horizon: `{PAPER_START}` through `{PAPER_END}`.",
        f"Initial capital: `${initial_capital:,.2f}`. No contributions, fees, or slippage.",
        "",
        "Entry requires MVRV below -0.2 and price within the configured band above",
        "the locally calculated CVDD. Tested bands: 5%, 10%, 15%, and 20%.",
        "",
        "## Benchmark",
        "",
        f"- Paper MVRV-3 ending value: `${paper['ending_portfolio_value']:,.2f}`.",
        f"- Paper MVRV-3 maximum drawdown: `{paper['maximum_drawdown_abs']:.2%}`.",
        "",
        "## Leaders",
        "",
        f"- Highest wealth: `{highest_wealth['strategy']}` "
        f"(${highest_wealth['ending_portfolio_value']:,.2f}).",
        f"- Highest Calmar: `{best_calmar['strategy']}` "
        f"({best_calmar['calmar_ratio']:.2f}).",
        f"- Lowest drawdown: `{best_drawdown['strategy']}` "
        f"({best_drawdown['maximum_drawdown_abs']:.2%}).",
        f"- Full-coverage Pareto candidates: "
        f"`{', '.join(report.loc[report['pareto_full_coverage'], 'strategy'])}`.",
        "",
        "The 5% and 10% bands are excluded from leader selection because they",
        "captured only one and two cycle entries respectively.",
        "",
        "## Comparison",
        "",
        "| Strategy | Entries | Ending value | Max drawdown | Calmar | Wealth vs paper |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | {row.cvdd_entries} | "
            f"${row.ending_portfolio_value:,.0f} | "
            f"{row.maximum_drawdown_abs:.2%} | {row.calmar_ratio:.2f} | "
            f"{row.wealth_vs_paper_mvrv_3:+.2%} |"
            for row in report.sort_values("ending_portfolio_value", ascending=False).itertuples()
        ],
        "",
        "The 15% and 20% bands both captured all three cycles and produced very",
        "similar results. The sharp failure below 15% is still a threshold cliff:",
        "the 2015 trough was 13.81% above locally calculated CVDD.",
        "",
        "These bands are local sensitivity tests, not paper-reported CVDD rules or",
        "frozen live parameters.",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _strategy_name(exit_mode: str, band: float) -> str:
    suffix = {
        "paper": "paper_exit",
        "mvrv_rollover_50": "mvrv_rollover_50",
        "combined_rollover_75": "combined_rollover_75",
    }[exit_mode]
    return f"cvdd_{int(band * 100):02d}_{suffix}"


def _load_daily(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run models/bitcoin_onchain_cycles/run_backtest.py first"
        )
    daily = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "close", "mvrv_zscore", "nupl", "cvdd"}
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
