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

from model_development.phase_3.portfolio_report import write_portfolio_report
from model_development.models.bitcoin_onchain_cycles.allocation import (
    ACCUMULATION_TIERS,
    LADDER_REARM_DAYS,
    LADDER_REARM_DRAWDOWN,
    LADDER_RUNGS,
    simulate_accumulation_dca,
    simulate_buy_and_hold,
    simulate_ladder_accumulation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent

# Frozen reference from accumulation_results/. This is the model to beat.
FROZEN_BASELINE = "frozen_acc_once_base25_deep"
FROZEN_BASELINE_KWARGS: dict[str, object] = {
    "base_share": 0.25,
    "tiers": ((0.50, 0.50), (0.00, 0.75), (-0.20, 1.00)),
    "tier_mode": "once_per_cycle",
}

# Pre-declared grid. `unit_divisor` becomes unit_notional = initial_capital /
# unit_divisor, so a fixed unit stays meaningful for any capital scale.
LADDER_VARIANTS: dict[str, dict[str, object]] = {
    "ladder_share_f10_b35": {"base_share": 0.35},
    "ladder_share_f10_b50": {"base_share": 0.50},
    "ladder_unit20_f10_b35": {
        "base_share": 0.35,
        "sizing": "fixed_unit",
        "unit_divisor": 20.0,
    },
    "ladder_unit12_f10_b50": {
        "base_share": 0.50,
        "sizing": "fixed_unit",
        "unit_divisor": 12.0,
    },
    "ladder_unit40_f05_b20": {
        "base_share": 0.20,
        "sizing": "fixed_unit",
        "unit_divisor": 40.0,
        "reserve_floor": 0.05,
    },
    "ladder_share_f25_b35": {"base_share": 0.35, "reserve_floor": 0.25},
    "ladder_share_f40_b35": {"base_share": 0.35, "reserve_floor": 0.40},
    "ladder_share_f60_b35": {"base_share": 0.35, "reserve_floor": 0.60},
}
LADDER_LABELS = {
    "ladder_share_f10_b35": "share sizing, floor 10%, base 35%",
    "ladder_share_f10_b50": "share sizing, floor 10%, base 50%",
    "ladder_unit20_f10_b35": "unit C/20, floor 10%, base 35%",
    "ladder_unit12_f10_b50": "unit C/12, floor 10%, base 50%",
    "ladder_unit40_f05_b20": "unit C/40, floor 5%, base 20%",
    "ladder_share_f25_b35": "share sizing, floor 25%, base 35%",
    "ladder_share_f40_b35": "share sizing, floor 40%, base 35%",
    "ladder_share_f60_b35": "share sizing, floor 60%, base 35%",
}
SELECTION_BENCHMARK = "static_dca"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Drawdown-ladder accumulation with a permanent reserve: never sell, "
            "never deploy the whole budget, re-arm at most once per window"
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
        default=MODEL_DIR / "research" / "portfolio_allocation" / "ladder_results",
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
        FROZEN_BASELINE: simulate_accumulation_dca(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
            **FROZEN_BASELINE_KWARGS,
        ),
    }
    for name, overrides in LADDER_VARIANTS.items():
        ledgers[name] = _ladder_ledger(
            daily, overrides, args.initial_capital, args.monthly_contribution
        )

    args.output.mkdir(parents=True, exist_ok=True)
    report = write_portfolio_report(
        ledgers,
        args.output,
        benchmark=SELECTION_BENCHMARK,
        groups={
            "Benchmarks": (
                "static_dca",
                "buy_hold_contributions",
                FROZEN_BASELINE,
            ),
            "Ladder variants": tuple(LADDER_VARIANTS),
        },
        labels={
            "static_dca": "Static 12-Month DCA",
            "buy_hold_contributions": "Buy & Hold + Contributions",
            FROZEN_BASELINE: "Frozen baseline (once/cycle tier)",
            **{
                name: f"Ladder: {label}" for name, label in LADDER_LABELS.items()
            },
        },
        title="Drawdown-Ladder Accumulation with a Permanent Reserve",
    )
    report = _enrich_report(report, ledgers, args.initial_capital)
    report.sort_values("maximum_drawdown_abs").to_csv(
        args.output / "performance_report.csv", index=False
    )
    _write_ledgers(ledgers, args.output)
    _save_frontier(report, args.output / "frontier.png")
    _write_findings(
        report,
        args.output / "findings.md",
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
                "maximum_drawdown_abs",
                "cash_weight_at_peak",
                "deployed_ratio",
                "beats_dca_wealth",
                "beats_dca_drawdown",
                "dominates_dca",
            ]
        ]
        .sort_values("maximum_drawdown_abs")
        .to_string(index=False)
    )
    print(f"\nOutputs: {args.output.resolve()}")


def _ladder_ledger(
    daily: pd.DataFrame,
    overrides: dict[str, object],
    initial_capital: float,
    monthly_contribution: float,
) -> pd.DataFrame:
    kwargs = dict(overrides)
    divisor = kwargs.pop("unit_divisor", None)
    if divisor is not None:
        kwargs["unit_notional"] = initial_capital / float(divisor)
    return simulate_ladder_accumulation(
        daily,
        initial_capital=initial_capital,
        monthly_contribution=monthly_contribution,
        **kwargs,
    )


def _enrich_report(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
    initial_capital: float,
) -> pd.DataFrame:
    enriched = report.copy()
    diagnostics = {
        name: _drawdown_diagnostics(ledger) for name, ledger in ledgers.items()
    }
    enriched["drawdown_trough_date"] = enriched["strategy"].map(
        lambda name: diagnostics[name]["trough_date"]
    )
    enriched["peak_date"] = enriched["strategy"].map(
        lambda name: diagnostics[name]["peak_date"]
    )
    enriched["btc_weight_at_trough"] = enriched["strategy"].map(
        lambda name: diagnostics[name]["btc_weight_at_trough"]
    )
    enriched["cash_weight_at_peak"] = enriched["strategy"].map(
        lambda name: diagnostics[name]["cash_weight_at_peak"]
    )
    enriched["btc_price_drawdown"] = enriched["strategy"].map(
        lambda name: diagnostics[name]["btc_price_drawdown"]
    )
    # The drawdown identity is an approximation, not an identity: contributions
    # keep buying through the decline and the reported max DD is the
    # flow-stripped time-weighted drawdown. Measured here so the text cannot
    # present it as exact (audit finding G7).
    enriched["equity_max_drawdown"] = enriched["strategy"].map(
        lambda name: diagnostics[name]["equity_max_drawdown"]
    )
    enriched["drawdown_identity_residual"] = (
        (1.0 - enriched["cash_weight_at_peak"]) * enriched["btc_price_drawdown"]
        - enriched["equity_max_drawdown"]
    )
    enriched["cash_weight_for_50pct_dd"] = enriched["strategy"].map(
        lambda name: _required_cash_weight(diagnostics[name]["btc_price_drawdown"], 0.50)
    )
    enriched["cash_weight_for_70pct_dd"] = enriched["strategy"].map(
        lambda name: _required_cash_weight(diagnostics[name]["btc_price_drawdown"], 0.70)
    )
    enriched["deployed_ratio"] = enriched["strategy"].map(
        lambda name: float(
            ledgers[name]["purchase_notional"].sum() / ledgers[name]["net_flow"].sum()
        )
    )
    enriched["ladder_fires"] = enriched["strategy"].map(
        lambda name: int(
            ledgers[name]
            .get("signal", pd.Series(dtype=str))
            .fillna("hold")
            .str.startswith("ladder_")
            .sum()
        )
    )
    enriched["cooldown_days"] = enriched["strategy"].map(
        lambda name: int(
            ledgers[name]
            .get("signal", pd.Series(dtype=str))
            .fillna("hold")
            .eq("cooldown")
            .sum()
        )
    )
    enriched["maximum_drawdown_abs"] = enriched["maximum_drawdown"].abs()
    enriched["pareto_optimal"] = _pareto_mask(enriched)

    dca = enriched.loc[enriched["strategy"].eq(SELECTION_BENCHMARK)].iloc[0]
    frozen = enriched.loc[enriched["strategy"].eq(FROZEN_BASELINE)].iloc[0]
    enriched["wealth_vs_dca"] = (
        enriched["ending_portfolio_value"] / dca["ending_portfolio_value"] - 1.0
    )
    # Positive is always an improvement, matching the accumulation round. The
    # column used to be ``drawdown_vs_dca``, where negative meant better, so the
    # same phrase carried opposite signs in two findings files (audit finding
    # G10).
    enriched["drawdown_improvement_vs_dca"] = (
        dca["maximum_drawdown_abs"] - enriched["maximum_drawdown_abs"]
    )
    enriched["wealth_vs_frozen"] = (
        enriched["ending_portfolio_value"] / frozen["ending_portfolio_value"] - 1.0
    )
    enriched["drawdown_improvement_vs_frozen"] = (
        frozen["maximum_drawdown_abs"] - enriched["maximum_drawdown_abs"]
    )
    # A benchmark cannot beat itself; the old columns did not exclude it, so
    # static DCA carried ``beats_dca_wealth`` and ``dominates_dca`` = True
    # (audit finding G11).
    is_benchmark = enriched["strategy"].eq(SELECTION_BENCHMARK)
    enriched["beats_dca_wealth"] = (~is_benchmark) & (
        enriched["ending_portfolio_value"] > dca["ending_portfolio_value"]
    )
    enriched["beats_dca_drawdown"] = (~is_benchmark) & (
        enriched["maximum_drawdown_abs"] < dca["maximum_drawdown_abs"]
    )
    enriched["dominates_dca"] = (
        enriched["beats_dca_wealth"] & enriched["beats_dca_drawdown"]
    )
    enriched["beats_frozen"] = (
        enriched["ending_portfolio_value"] >= frozen["ending_portfolio_value"]
    ) & (enriched["maximum_drawdown_abs"] < frozen["maximum_drawdown_abs"])
    return enriched


def _drawdown_diagnostics(ledger: pd.DataFrame) -> dict[str, object]:
    """Locate the equity trough, the peak before it, and the identity residual."""
    equity = ledger["equity"].astype(float)
    trough = int(equity.div(equity.cummax()).idxmin())
    peak = int(equity.iloc[: trough + 1].idxmax())
    peak_price = float(ledger["asset_price"].iloc[peak])
    trough_price = float(ledger["asset_price"].iloc[trough])
    return {
        "trough_date": pd.Timestamp(ledger["date"].iloc[trough]).date().isoformat(),
        "peak_date": pd.Timestamp(ledger["date"].iloc[peak]).date().isoformat(),
        "btc_weight_at_trough": float(ledger["actual_btc_weight"].iloc[trough]),
        "cash_weight_at_peak": float(
            ledger["cash"].iloc[peak] / equity.iloc[peak]
        ),
        "btc_price_drawdown": (
            1.0 - trough_price / peak_price if peak_price > 0 else np.nan
        ),
        "equity_max_drawdown": abs(
            float(equity.div(equity.cummax()).sub(1.0).min())
        ),
    }


def _required_cash_weight(btc_price_drawdown: float, target_drawdown: float) -> float:
    """Cash weight at the peak that a no-sell portfolio needs for a DD target.

    Portfolio drawdown equals the BTC price drawdown scaled by the BTC weight
    held at the peak, so `cash = 1 - target / price_drawdown`.
    """
    if not np.isfinite(btc_price_drawdown) or btc_price_drawdown <= 0:
        return np.nan
    return float(np.clip(1.0 - target_drawdown / btc_price_drawdown, 0.0, 1.0))


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
    signals = long.get("signal", pd.Series(index=long.index, dtype=str)).fillna("hold")
    long.loc[signals.ne("hold")].to_csv(output / "signals.csv", index=False)


def _save_frontier(report: pd.DataFrame, output: Path) -> None:
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
        "Ladder accumulation with a permanent reserve", loc="left", fontweight="bold"
    )
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
    monthly_contribution: float,
    dca_months: int,
) -> None:
    dca = report.loc[report["strategy"].eq(SELECTION_BENCHMARK)].iloc[0]
    frozen = report.loc[report["strategy"].eq(FROZEN_BASELINE)].iloc[0]
    ladder = report.loc[report["strategy"].isin(LADDER_VARIANTS)]
    lowest_drawdown = ladder.loc[ladder["maximum_drawdown_abs"].idxmin()]
    highest_wealth = ladder.loc[ladder["ending_portfolio_value"].idxmax()]
    dominators = ladder.loc[ladder["dominates_dca"], "strategy"]
    peak = float(frozen["btc_price_drawdown"])
    lines = [
        "# Drawdown-Ladder Accumulation with a Permanent Reserve",
        "",
        f"Horizon: `{PAPER_START}` through `{PAPER_END}` (in-sample design).",
        f"Initial capital: `${initial_capital:,.2f}`; "
        f"monthly contribution: `${monthly_contribution:,.2f}`.",
        f"Static DCA deploys the initial capital over `{dca_months}` months.",
        "Cash earns 0%; fees and slippage are excluded, matching the paper.",
        "",
        "## Rules Under Test",
        "",
        "No selling and no exit. Inflows split into an always-on base tranche and",
        "a reserve. The reserve is spent only when price falls a given distance",
        "below its running all-time high:",
        "",
        *[
            f"- `{threshold:.0%}` from the high -> buy `{share:.0%}` of the reserve"
            for threshold, share in LADDER_RUNGS
        ],
        "",
        f"- At most one ladder rung per `{LADDER_REARM_DAYS}` days.",
        "- Each rung fires at most once per cycle, re-armed when price comes back",
        f"  within `{abs(LADDER_REARM_DRAWDOWN):.0%}` of its high.",
        "- Cumulative deployment is capped at `1 - reserve_floor` of every dollar",
        "  ever received, so the plan never reaches 100% invested.",
        "",
        "## Frozen Benchmark To Beat",
        "",
        f"- `{FROZEN_BASELINE}`: `${frozen['ending_portfolio_value']:,.2f}`, "
        f"`{frozen['ending_btc_units']:,.4f}` BTC, "
        f"`{frozen['maximum_drawdown_abs']:.2%}` drawdown.",
        f"- Static DCA: `${dca['ending_portfolio_value']:,.2f}`, "
        f"`{dca['ending_btc_units']:,.4f}` BTC, "
        f"`{dca['maximum_drawdown_abs']:.2%}` drawdown.",
        "",
        "## Leaders",
        "",
        f"- Lowest drawdown: `{lowest_drawdown['strategy']}` "
        f"({lowest_drawdown['maximum_drawdown_abs']:.2%}, "
        f"{lowest_drawdown['drawdown_improvement_vs_dca']:+.2%} versus DCA, where "
        f"positive is an improvement), "
        f"cash at peak `{lowest_drawdown['cash_weight_at_peak']:.1%}`, "
        f"deployed `{lowest_drawdown['deployed_ratio']:.1%}` of inflows.",
        f"- Highest wealth: `{highest_wealth['strategy']}` "
        f"(${highest_wealth['ending_portfolio_value']:,.2f}, "
        f"{highest_wealth['wealth_vs_dca']:+.2%} vs DCA, "
        f"drawdown {highest_wealth['maximum_drawdown_abs']:.2%}).",
        f"- Variants that beat static DCA on wealth AND drawdown: "
        f"`{', '.join(dominators) if len(dominators) else 'none'}`.",
        f"- Variants that beat the frozen baseline on both: "
        f"`{', '.join(ladder.loc[ladder['beats_frozen'], 'strategy']) or 'none'}`.",
        f"- Pareto candidates: "
        f"`{', '.join(report.loc[report['pareto_optimal'], 'strategy'])}`.",
        "",
        "## The Drawdown Identity",
        "",
        "For a portfolio that never sells, drawdown is the BTC price drawdown",
        "scaled by the BTC weight held at the peak:",
        "",
        "```",
        "portfolio_drawdown ~= (1 - cash_weight_at_peak) x btc_price_drawdown",
        "cash_weight_at_peak ~= 1 - target_drawdown / btc_price_drawdown",
        "```",
        "",
        "It is an approximation, not an identity, and it is measured rather than",
        "asserted: the largest residual across the variants is "
        f"`{report['drawdown_identity_residual'].abs().max():.2%}`. Contributions",
        "keep buying through the decline, and `Max DD` above is the flow-stripped",
        "time-weighted drawdown, so the raw equity drawdown it is compared against",
        "sits a few basis points away (audit finding G7).",
        "",
        f"The benchmark's peak-to-trough price drawdown was `{peak:.2%}`, so:",
        "",
        f"- a `70%` portfolio drawdown needs `{frozen['cash_weight_for_70pct_dd']:.1%}` "
        "cash at the peak,",
        f"- a `50%` portfolio drawdown needs `{frozen['cash_weight_for_50pct_dd']:.1%}` "
        "cash at the peak,",
        f"- the frozen baseline held `{frozen['cash_weight_at_peak']:.1%}`.",
        "",
        "## Comparison",
        "",
        "| Strategy | Deployed | Cash at peak | BTC w. trough | Ending value | BTC stack | "
        "Max DD | DD gain vs DCA | vs DCA wealth | Fires |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | {row.deployed_ratio:.1%} | "
            f"{row.cash_weight_at_peak:.1%} | {row.btc_weight_at_trough:.1%} | "
            f"${row.ending_portfolio_value:,.0f} | {row.ending_btc_units:,.4f} | "
            f"{row.maximum_drawdown_abs:.2%} | {row.drawdown_improvement_vs_dca:+.2%} | "
            f"{row.wealth_vs_dca:+.2%} | {row.ladder_fires} |"
            for row in report.sort_values(
                "maximum_drawdown_abs"
            ).itertuples()
        ],
        "",
        "## Reading Rules",
        "",
        "- Drawdown improvement requires cash that is still unspent at the peak.",
        "  Read `Cash at peak` before `Max DD`: the approximation above explains most",
        "  of the table, and a low `Deployed` ratio only helps if the unspent cash",
        "  is large relative to the portfolio, not relative to the budget.",
        "- Beating DCA on wealth requires owning a similar BTC stack, which",
        "  requires being long through the bull market that precedes the peak.",
        "- This horizon is in-sample. The OOS boundary stays untouched until one",
        "  variant is frozen.",
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
