from __future__ import annotations

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.phase_3.portfolio_report import calculate_portfolio_metrics, write_portfolio_report
from model_development.models.bitcoin_onchain_cycles.allocation import (
    ACCUMULATION_TIERS,
    NO_BUY_ABOVE,
    simulate_accumulation_dca,
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.cost_basis import cost_basis_metrics
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent

# Pre-declared grid: the literal "buy only in cheap zones" idea plus hybrids
# with an always-on base tranche. The first five keep the crossing rule that a
# first diagnostic pass showed to drain the reserve geometrically; the rest use
# once-per-cycle tiers, which preserve powder across a multi-year bear market.
ACCUMULATION_VARIANTS: dict[str, dict[str, object]] = {
    "acc_threshold_only": {"base_share": 0.0},
    "acc_base25": {"base_share": 0.25},
    "acc_base40": {"base_share": 0.40},
    "acc_base60": {"base_share": 0.60},
    "acc_base25_expiry730": {"base_share": 0.25, "powder_expiry_days": 730},
    "acc_once_threshold_only": {"base_share": 0.0, "tier_mode": "once_per_cycle"},
    "acc_once_base25": {"base_share": 0.25, "tier_mode": "once_per_cycle"},
    "acc_once_base40": {"base_share": 0.40, "tier_mode": "once_per_cycle"},
    "acc_once_base60": {"base_share": 0.60, "tier_mode": "once_per_cycle"},
    "acc_once_base25_deep": {
        "base_share": 0.25,
        "tier_mode": "once_per_cycle",
        "tiers": ((0.5, 0.50), (0.0, 0.75), (-0.2, 1.0)),
    },
}
VARIANT_LABELS = {
    "acc_threshold_only": "crossing, 0% base",
    "acc_base25": "crossing, 25% base",
    "acc_base40": "crossing, 40% base",
    "acc_base60": "crossing, 60% base",
    "acc_base25_expiry730": "crossing, 25% base, powder expires 730d",
    "acc_once_threshold_only": "once/cycle, 0% base",
    "acc_once_base25": "once/cycle, 25% base",
    "acc_once_base40": "once/cycle, 40% base",
    "acc_once_base60": "once/cycle, 60% base",
    "acc_once_base25_deep": "once/cycle, 25% base, deep tiers",
}
SELECTION_BENCHMARK = "static_dca"
# The variant frozen in ACCUMULATION_CONTRACT.md, reported for comparison in the
# benchmark-sensitivity table.
FROZEN_VARIANT = "acc_once_base25_deep"
# Reference strategies that sell, shown for scale but excluded from the
# never-sell Pareto frontier.
SELLING_STRATEGIES = ("paper_mvrv_3",)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Accumulation-only on-chain DCA: buy by condition, never sell"
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "paper_daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            MODEL_DIR / "research" / "portfolio_allocation" / "accumulation_results"
        ),
    )
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=100.0)
    parser.add_argument("--static-dca-months", type=int, default=12)
    args = parser.parse_args()

    daily = _load_paper_daily(args.prepared)
    ledgers: dict[str, pd.DataFrame] = {
        "static_dca": simulate_static_dca(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
            deployment_months=args.static_dca_months,
        ),
        "buy_hold_contributions": simulate_buy_and_hold(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
        ),
        "paper_mvrv_3": simulate_mvrv_allocation(
            daily,
            (1.0, 1.0, 0.0),
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
        ),
    }
    for name, overrides in ACCUMULATION_VARIANTS.items():
        ledgers[name] = simulate_accumulation_dca(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
            **overrides,
        )

    args.output.mkdir(parents=True, exist_ok=True)
    report = write_portfolio_report(
        ledgers,
        args.output,
        benchmark=SELECTION_BENCHMARK,
        groups={
            "Contribution benchmarks": ("static_dca", "buy_hold_contributions"),
            "Paper rules with contributions": ("paper_mvrv_3",),
            "Accumulation-only variants": tuple(ACCUMULATION_VARIANTS),
        },
        labels={
            "static_dca": "Static 12-Month DCA",
            "buy_hold_contributions": "Buy & Hold + Contributions",
            "paper_mvrv_3": "Paper MVRV-3 (with contributions)",
            **{name: f"Acc: {label}" for name, label in VARIANT_LABELS.items()},
        },
        title="Bitcoin On-Chain Accumulation-Only DCA",
    )
    report = _enrich_report(report, ledgers, SELECTION_BENCHMARK)
    report.sort_values(
        ["pareto_optimal", "maximum_drawdown_abs"],
        ascending=[False, True],
    ).to_csv(args.output / "performance_report.csv", index=False)
    _write_ledgers(ledgers, args.output)
    _save_pareto(report, args.output / "pareto_frontier.png")
    _write_findings(
        report,
        daily,
        args.output,
        args.initial_capital,
        args.monthly_contribution,
        args.static_dca_months,
    )
    print(
        report[
            [
                "strategy",
                "ending_portfolio_value",
                "ending_btc_units",
                "average_btc_acquisition_price",
                "maximum_drawdown_abs",
                "btc_weight_at_trough",
                "calmar_ratio",
                "average_cash_weight",
                "tier_purchases",
            ]
        ]
        .sort_values("maximum_drawdown_abs")
        .to_string(index=False)
    )
    print(f"\nOutputs: {args.output.resolve()}")


def _variant_base_share(name: str) -> float:
    """The base-tranche share a variant actually runs, 0 for the benchmarks."""
    overrides = ACCUMULATION_VARIANTS.get(name)
    if overrides is None:
        return 0.0
    return float(overrides.get("base_share", 0.25))  # type: ignore[arg-type]


def _purchase_counts(ledger: pd.DataFrame, base_share: float) -> dict[str, int]:
    """Count real purchases instead of reading them off the ``signal`` label.

    ``signal`` records only the last action taken on a close and is overwritten
    when several fire together, so counting it undercounts tiers and base buys
    (audit finding G2). Base buys are counted from the inflow and the Z-score,
    tier fires from the per-close ``tiers_fired`` list. A variant with
    ``base_share = 0`` has no base tranche to buy or pause, so both base counters
    stay at zero for it.
    """
    empty = {
        "tier_purchases": 0,
        "base_purchases": 0,
        "paused_contribution_days": 0,
        "powder_expiry_days": 0,
    }
    if "mvrv_zscore" not in ledger or "net_flow" not in ledger:
        return empty
    inflow = ledger["net_flow"].gt(0)
    zscore = pd.to_numeric(ledger["mvrv_zscore"], errors="coerce")
    fired = ledger["tiers_fired"].astype(str) if "tiers_fired" in ledger else None
    if fired is not None:
        tier_purchases = int(
            sum(
                len([n for n in entry.split(",") if n and n != "nan"])
                for entry in fired
            )
        )
    else:
        tier_purchases = int(ledger["signal"].fillna("hold").str.startswith("tier_").sum())
    has_base = base_share > 0.0
    return {
        "tier_purchases": tier_purchases,
        "base_purchases": int((inflow & zscore.lt(NO_BUY_ABOVE)).sum()) if has_base else 0,
        "paused_contribution_days": (
            int((inflow & zscore.ge(NO_BUY_ABOVE)).sum()) if has_base else 0
        ),
        # Exact, from the simulator's own log: a close that expires powder and
        # also fires a tier records only the tier in ``signal``.
        "powder_expiry_days": int(
            pd.to_numeric(
                ledger.get("powder_expired_notional", pd.Series(dtype=float)),
                errors="coerce",
            )
            .fillna(0.0)
            .gt(0)
            .sum()
        ),
    }


def _enrich_report(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
    benchmark: str,
) -> pd.DataFrame:
    enriched = report.copy()
    enriched["ending_cash"] = enriched["strategy"].map(
        lambda name: float(ledgers[name]["cash"].iloc[-1])
    )
    enriched["number_of_transactions"] = enriched["strategy"].map(
        lambda name: int(ledgers[name]["transaction_count"].sum())
    )
    counts = {
        name: _purchase_counts(ledger, _variant_base_share(name))
        for name, ledger in ledgers.items()
    }
    for column in (
        "tier_purchases",
        "base_purchases",
        "paused_contribution_days",
        "powder_expiry_days",
    ):
        enriched[column] = enriched["strategy"].map(
            lambda name, column=column: counts[name][column]
        )
    enriched["maximum_drawdown_abs"] = enriched["maximum_drawdown"].abs()
    enriched["drawdown_trough_date"] = enriched["strategy"].map(
        lambda name: _drawdown_diagnostics(ledgers[name])["date"]
    )
    enriched["btc_weight_at_trough"] = enriched["strategy"].map(
        lambda name: _drawdown_diagnostics(ledgers[name])["btc_weight"]
    )
    enriched["pareto_optimal"] = _pareto_mask(
        enriched, eligible=~enriched["strategy"].isin(SELLING_STRATEGIES)
    )

    reference = enriched.loc[enriched["strategy"].eq(benchmark)].iloc[0]
    enriched["btc_stack_vs_static_dca"] = (
        enriched["ending_btc_units"] / reference["ending_btc_units"] - 1.0
    )
    enriched["wealth_vs_static_dca"] = (
        enriched["ending_portfolio_value"] / reference["ending_portfolio_value"] - 1.0
    )
    enriched["drawdown_improvement_vs_static_dca"] = (
        reference["maximum_drawdown_abs"] - enriched["maximum_drawdown_abs"]
    )
    return enriched


def _drawdown_diagnostics(ledger: pd.DataFrame) -> dict[str, object]:
    """Locate the equity trough and the BTC weight held there."""
    equity = ledger["equity"].astype(float)
    trough = equity.div(equity.cummax()).idxmin()
    return {
        "date": pd.Timestamp(ledger["date"].iloc[trough]).date().isoformat(),
        "btc_weight": float(ledger["actual_btc_weight"].iloc[trough]),
    }


def _pareto_mask(report: pd.DataFrame, eligible: pd.Series | None = None) -> pd.Series:
    """Non-dominated set among the eligible rows only.

    Strategies that sell are excluded, because the frontier is meant to describe
    what the never-sell family can achieve. Including them left a frontier with a
    single point that was not part of the family at all (audit finding G9).
    """
    wealth = report["ending_portfolio_value"].to_numpy(dtype=float)
    risk = report["maximum_drawdown"].abs().to_numpy(dtype=float)
    allowed = (
        np.ones(len(report), dtype=bool)
        if eligible is None
        else eligible.to_numpy(dtype=bool)
    )
    optimal = np.zeros(len(report), dtype=bool)
    for index in range(len(report)):
        if not allowed[index]:
            continue
        dominated = (
            allowed
            & (wealth >= wealth[index] * (1.0 - 1e-12))
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
    signals = long.get("signal", pd.Series(index=long.index, dtype=str)).fillna("hold")
    long.loc[signals.ne("hold")].to_csv(output / "signals.csv", index=False)


def _save_pareto(report: pd.DataFrame, output: Path) -> None:
    fig, axis = plt.subplots(figsize=(11, 7))
    regular = report.loc[~report["pareto_optimal"]]
    frontier = report.loc[report["pareto_optimal"]].sort_values("maximum_drawdown_abs")
    axis.scatter(
        regular["maximum_drawdown_abs"],
        regular["ending_portfolio_value"],
        color="#9ca3af",
        alpha=0.7,
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
    axis.set_xlabel("Maximum drawdown of portfolio equity")
    axis.set_ylabel("Ending portfolio value")
    axis.set_yscale("log")
    axis.set_title(
        "Accumulation-only DCA return/drawdown frontier", loc="left", fontweight="bold"
    )
    axis.grid(color="#e5e7eb", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _benchmark_deployment_sweep(
    daily: pd.DataFrame,
    initial_capital: float,
    monthly_contribution: float,
    current_months: int,
) -> pd.DataFrame:
    """Measure how much the benchmark's deployment schedule moves the answer.

    ``deployment_months`` is a free parameter, not something the paper fixes, and
    it turns out to matter more than the model's entire edge (audit finding G1).
    ``deployment_months=1`` is identical to buy-and-hold, which is asserted here
    rather than only described.
    """
    buy_hold = simulate_buy_and_hold(
        daily,
        initial_capital=initial_capital,
        monthly_contribution=monthly_contribution,
    )
    rows = []
    for months in sorted({1, 6, current_months, 24, 36}):
        ledger = simulate_static_dca(
            daily,
            initial_capital=initial_capital,
            monthly_contribution=monthly_contribution,
            deployment_months=months,
        )
        portfolio = calculate_portfolio_metrics(ledger)
        basis = cost_basis_metrics(ledger)
        if months == 1:
            if not np.allclose(
                ledger["btc_units"].to_numpy(dtype=float),
                buy_hold["btc_units"].to_numpy(dtype=float),
            ):
                raise ValueError("a one-month deployment should equal buy-and-hold")
        rows.append(
            {
                "deployment_months": months,
                "ending_portfolio_value": portfolio["ending_portfolio_value"],
                "average_btc_acquisition_price": portfolio[
                    "average_btc_acquisition_price"
                ],
                "worst_equity_vs_contributions": basis["worst_equity_vs_contributions"],
                "equity_underwater_share": basis["equity_underwater_share"],
                "equals_buy_and_hold": months == 1,
            }
        )
    return pd.DataFrame(rows)


def _write_findings(
    report: pd.DataFrame,
    daily: pd.DataFrame,
    output: Path,
    initial_capital: float,
    monthly_contribution: float,
    dca_months: int,
) -> None:
    dca = report.loc[report["strategy"].eq(SELECTION_BENCHMARK)].iloc[0]
    frozen = report.loc[report["strategy"].eq(FROZEN_VARIANT)].iloc[0]
    variants = report.loc[report["strategy"].isin(ACCUMULATION_VARIANTS)]
    lowest_drawdown = variants.loc[variants["maximum_drawdown_abs"].idxmin()]
    highest_calmar = variants.loc[variants["calmar_ratio"].idxmax()]
    largest_stack = variants.loc[variants["ending_btc_units"].idxmax()]
    highest_wealth = variants.loc[variants["ending_portfolio_value"].idxmax()]
    sweep = _benchmark_deployment_sweep(
        daily, initial_capital, monthly_contribution, dca_months
    )
    sweep.to_csv(output / "benchmark_sensitivity.csv", index=False)
    fastest = sweep.loc[sweep["deployment_months"].eq(1)].iloc[0]
    slowest = sweep.loc[sweep["deployment_months"].idxmax()]
    best_benchmark = sweep.loc[sweep["ending_portfolio_value"].idxmax()]
    lines = [
        "# Accumulation-Only On-Chain DCA",
        "",
        f"Horizon: `{PAPER_START}` through `{PAPER_END}` (in-sample design).",
        f"Initial capital: `${initial_capital:,.2f}`; "
        f"monthly contribution: `${monthly_contribution:,.2f}`.",
        f"Static DCA deploys the initial capital over `{dca_months}` months.",
        "Cash earns 0%; fees and slippage are excluded, matching the paper.",
        "",
        "## Rules Under Test",
        "",
        "The model never sells. Each inflow splits into an always-on base tranche",
        "and a dry-powder reserve. Powder is spent only when the MVRV Z-score",
        "reaches a tier threshold, where each fraction is a share of the powder",
        "still available at that moment:",
        "",
        *[
            f"- `{threshold:+.2f}` -> buy `{fraction:.0%}` of remaining powder"
            for threshold, fraction in ACCUMULATION_TIERS
        ],
        f"- Base buying pauses at or above Z `{NO_BUY_ABOVE:.2f}`.",
        "",
        "Two tier modes are compared. `crossing` spends on every downward",
        "crossing of a threshold; `once_per_cycle` lets each tier spend at most",
        "once between re-arms at or above the no-buy level. The `deep tiers`",
        "variant also drops the highest tier to Z `0.50`, because Z `2.00` was",
        "reached in early 2014 while price was still near the cycle top.",
        "",
        "## Benchmark",
        "",
        f"- Static 12-month DCA ending value: `${dca['ending_portfolio_value']:,.2f}`.",
        f"- Static DCA maximum drawdown: `{dca['maximum_drawdown_abs']:.2%}`.",
        f"- Static DCA ending BTC: `{dca['ending_btc_units']:,.4f}`.",
        "",
        "## Benchmark Sensitivity",
        "",
        f"The benchmark deploys the initial capital over `{dca_months}` months. That",
        "number is a free parameter, not something the paper fixes, and it moves the",
        "benchmark by more than the model's entire edge (audit finding G1).",
        "`deployment_months=1` is identical to `buy_hold_contributions`, verified",
        "programmatically below, so two rows in every table of this family are the",
        "same strategy at different speeds.",
        "",
        "| Deployment months | Ending value | Avg cost | Felt loss | Underwater share | Note |",
        "| ---: | ---: | ---: | ---: | ---: | --- |",
        *[
            f"| {row.deployment_months} | ${row.ending_portfolio_value:,.2f} | "
            f"${row.average_btc_acquisition_price:,.2f} | "
            f"{row.worst_equity_vs_contributions:.2%} | "
            f"{row.equity_underwater_share:.1%} | "
            f"{'equals buy-and-hold' if row.equals_buy_and_hold else ''} |"
            for row in sweep.itertuples()
        ],
        "",
        f"- The fastest schedule (`{int(fastest['deployment_months'])}` month) ends at "
        f"`${fastest['ending_portfolio_value']:,.2f}`; the `{dca_months}`-month default "
        f"ends at `${dca['ending_portfolio_value']:,.2f}`; the best tested schedule is "
        f"`{int(best_benchmark['deployment_months'])}` months at "
        f"`${best_benchmark['ending_portfolio_value']:,.2f}`.",
        f"- Frozen variant `{FROZEN_VARIANT}` ends at "
        f"`${frozen['ending_portfolio_value']:,.2f}`: "
        f"`{frozen['wealth_vs_static_dca']:+.2%}` against the `{dca_months}`-month "
        f"benchmark, but "
        f"`{frozen['ending_portfolio_value'] / best_benchmark['ending_portfolio_value'] - 1:+.2%}` "
        f"against the best tested one. Any claim that this family beats static DCA has",
        "  to name the deployment schedule it beat.",
        "",
        "## Leaders",
        "",
        f"- Lowest drawdown: `{lowest_drawdown['strategy']}` "
        f"({lowest_drawdown['maximum_drawdown_abs']:.2%}), "
        f"{lowest_drawdown['drawdown_improvement_vs_static_dca']:+.2%} vs static DCA.",
        f"- Highest Calmar: `{highest_calmar['strategy']}` "
        f"({highest_calmar['calmar_ratio']:.2f}).",
        f"- Largest BTC stack: `{largest_stack['strategy']}` "
        f"({largest_stack['ending_btc_units']:,.4f} BTC, "
        f"{largest_stack['btc_stack_vs_static_dca']:+.2%} vs static DCA).",
        f"- Highest wealth: `{highest_wealth['strategy']}` "
        f"(${highest_wealth['ending_portfolio_value']:,.2f}, "
        f"{highest_wealth['wealth_vs_static_dca']:+.2%} vs static DCA).",
        f"- Pareto candidates: "
        f"`{', '.join(report.loc[report['pareto_optimal'], 'strategy'])}` "
        f"(never-sell variants only; strategies that sell, "
        f"`{', '.join(SELLING_STRATEGIES)}`, are excluded because they are not part",
        "  of this family).",
        "",
        "## Comparison",
        "",
        "| Strategy | Purchase days | Tier fires | Base buys | Ending value | BTC stack | "
        "Avg cost | Max drawdown | Trough | BTC weight at trough | Calmar | "
        "Cash weight |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | {row.number_of_transactions} | {row.tier_purchases} | "
            f"{row.base_purchases} | "
            f"${row.ending_portfolio_value:,.0f} | {row.ending_btc_units:,.4f} | "
            f"${row.average_btc_acquisition_price:,.0f} | "
            f"{row.maximum_drawdown_abs:.2%} | {row.drawdown_trough_date} | "
            f"{row.btc_weight_at_trough:.1%} | {row.calmar_ratio:.2f} | "
            f"{row.average_cash_weight:.1%} |"
            for row in report.sort_values(
                "maximum_drawdown_abs"
            ).itertuples()
        ],
        "",
        "`Purchase days` counts closes with any purchase; `Tier fires` counts every",
        "tier that fired, so it can exceed the number of days. `Base buys` counts",
        "inflow days on which the base tranche was allowed to buy. "
        f"`powder_expiry_days` is in the metrics CSV: the expiry variant has "
        f"`{int(report.loc[report['strategy'].eq('acc_base25_expiry730'), 'powder_expiry_days'].iloc[0])}` "
        "closes on which unspent powder expired.",
        "",
        "## Reading Rules",
        "",
        "- The honest benchmark for an accumulator is static DCA with identical",
        "  cash on identical dates, not buy-and-hold. The deployment schedule of",
        "  that DCA matters more than this family's edge (see Benchmark",
        "  Sensitivity), so a win here is only a win against a named schedule.",
        "- A never-sell model cannot reduce the drawdown of BTC it already owns;",
        "  the only levers are the purchase price and the cash weight. Check",
        "  `BTC weight at trough`: when it approaches 100%, the model simply owned",
        "  the bear market.",
        "- The crossing rule drains the reserve geometrically. In the first pass",
        "  the 0% base crossing variant held 99.4% BTC by 2014-09-15, months before",
        "  the 2015 low, so every later cycle started fully invested.",
        "- Cash drag is the cost of the levers. Compare `average_cash_weight`",
        "  and `cash_drag_vs_benchmark` against the drawdown gain before choosing.",
        "- This horizon is in-sample. Freeze one variant here, then evaluate it",
        "  unchanged from the OOS boundary before treating it as live.",
        "",
        "These are historical research candidates, not live trading rules.",
    ]
    (output / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
