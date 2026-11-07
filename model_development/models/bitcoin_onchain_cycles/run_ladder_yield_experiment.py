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

from model_development.phase_3.portfolio_report import (
    calculate_portfolio_metrics,
    write_portfolio_report,
)
from model_development.models.bitcoin_onchain_cycles.allocation import (
    simulate_accumulation_dca,
    simulate_buy_and_hold,
    simulate_ladder_accumulation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.data_sources import (
    download_treasury_yield,
    load_treasury_yield,
)
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_END, PAPER_START


MODEL_DIR = Path(__file__).resolve().parent

# One variable only: what the reserve earns while it waits. Every strategy in a
# scenario receives the same rate, including the benchmarks, so the comparison
# stays honest.
CONSTANT_YIELDS: tuple[float, ...] = (0.00, 0.02, 0.04, 0.06)
TREASURY_SCENARIO = "treasury"

STRATEGIES: dict[str, dict[str, object]] = {
    "static_dca": {"kind": "static"},
    "buy_hold_contributions": {"kind": "buy_hold"},
    "frozen_acc_once_base25_deep": {
        "kind": "frozen",
        "base_share": 0.25,
        "tiers": ((0.50, 0.50), (0.00, 0.75), (-0.20, 1.00)),
        "tier_mode": "once_per_cycle",
    },
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
}
REFERENCE = "static_dca"
LOW_DRAWDOWN_REFERENCE = "ladder_share_f60_b35"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Isolate one variable: the cash yield earned by an unspent reserve, "
            "applied equally to every strategy in a scenario"
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
        default=MODEL_DIR / "research" / "portfolio_allocation" / "yield_results",
    )
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=100.0)
    parser.add_argument("--static-dca-months", type=int, default=12)
    parser.add_argument(
        "--yields",
        default=",".join(f"{value:g}" for value in CONSTANT_YIELDS),
    )
    parser.add_argument(
        "--treasury-cache",
        type=Path,
        default=MODEL_DIR / "data" / "source" / "treasury_yield.csv",
    )
    parser.add_argument(
        "--no-treasury",
        action="store_true",
        help="Skip the real 3-month Treasury bill scenario.",
    )
    parser.add_argument("--treasury-offline", action="store_true",
                        help="Use the saved Treasury series without refreshing it.")
    args = parser.parse_args()

    daily = _load_paper_daily(args.prepared)
    scenarios: dict[str, object] = {
        _yield_label(float(value)): float(value)
        for value in args.yields.split(",")
        if value.strip()
    }
    if not args.no_treasury:
        rates = _treasury_rates(
            args.treasury_cache, daily["date"].iloc[0], daily["date"].iloc[-1],
            offline=args.treasury_offline,
        )
        scenarios[TREASURY_SCENARIO] = rates
    treasury_mean = float(pd.Series(rates).mean()) if not args.no_treasury else float("nan")

    args.output.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, object]] = []
    for scenario, yield_value in scenarios.items():
        ledgers = _build_ledgers(
            daily,
            yield_value,
            args.initial_capital,
            args.monthly_contribution,
            args.static_dca_months,
        )
        write_portfolio_report(
            ledgers,
            args.output / scenario,
            benchmark=REFERENCE,
            groups={
                "Benchmarks": (REFERENCE, "buy_hold_contributions"),
                "Never-sell candidates": (
                    "frozen_acc_once_base25_deep",
                    *[name for name in STRATEGIES if name.startswith("ladder_")],
                ),
            },
            labels={
                "static_dca": "Static 12-Month DCA",
                "buy_hold_contributions": "Buy & Hold + Contributions",
                "frozen_acc_once_base25_deep": "Frozen baseline",
                "ladder_unit40_f05_b20": "Ladder unit C/40, floor 5%, base 20%",
                "ladder_share_f60_b35": "Ladder share, floor 60%, base 35%",
                "ladder_share_f40_b35": "Ladder share, floor 40%, base 35%",
            },
            title=f"Cash Yield Scenario: {scenario}",
        )
        for name, ledger in ledgers.items():
            metrics = calculate_portfolio_metrics(ledger)
            summary.append(
                {
                    "scenario": scenario,
                    "strategy": name,
                    "ending_portfolio_value": metrics["ending_portfolio_value"],
                    "ending_btc_units": metrics["ending_btc_units"],
                    "maximum_drawdown_abs": abs(metrics["maximum_drawdown"]),
                    "calmar_ratio": metrics["calmar_ratio"],
                    "average_cash_weight": metrics["average_cash_weight"],
                    "contributions": metrics["contributions"],
                }
            )

    swept = pd.DataFrame(summary)
    swept["cash_yield_annual"] = swept["scenario"].map(
        lambda name: (
            treasury_mean
            if name == TREASURY_SCENARIO
            else float(scenarios[name])
            if name in scenarios
            else np.nan
        )
    )
    reference_wealth = swept.loc[
        swept["strategy"].eq(REFERENCE), ["scenario", "ending_portfolio_value"]
    ].rename(columns={"ending_portfolio_value": "reference_wealth"})
    frozen_wealth = swept.loc[
        swept["strategy"].eq("frozen_acc_once_base25_deep"),
        ["scenario", "ending_portfolio_value"],
    ].rename(columns={"ending_portfolio_value": "frozen_wealth"})
    swept = swept.merge(reference_wealth, on="scenario").merge(frozen_wealth, on="scenario")
    swept["wealth_vs_static_dca"] = (
        swept["ending_portfolio_value"] / swept["reference_wealth"] - 1.0
    )
    swept["wealth_vs_frozen"] = (
        swept["ending_portfolio_value"] / swept["frozen_wealth"] - 1.0
    )
    swept.sort_values(["scenario", "maximum_drawdown_abs"]).to_csv(
        args.output / "yield_sweep.csv", index=False
    )
    _save_wealth_curve(swept, args.output / "wealth_vs_yield.png", treasury_mean)
    _write_findings(
        swept,
        args.output / "findings.md",
        args.initial_capital,
        args.monthly_contribution,
        scenarios,
    )
    print(
        swept.loc[
            swept["strategy"].isin([REFERENCE, LOW_DRAWDOWN_REFERENCE, "frozen_acc_once_base25_deep"]),
            [
                "scenario",
                "strategy",
                "ending_portfolio_value",
                "maximum_drawdown_abs",
                "wealth_vs_static_dca",
            ],
        ]
        .sort_values(["scenario", "strategy"])
        .to_string(index=False)
    )
    print(f"\nOutputs: {args.output.resolve()}")


def _treasury_rates(
    cache: Path, start: pd.Timestamp, end: pd.Timestamp, *, offline: bool = False
) -> pd.Series:
    """Treasury bill rates covering ``start`` through ``end``, gaps stated.

    The FRED series can begin a day or two after the ledger does, and only
    forward filling is causal. Rather than let the simulator silently import a
    future rate, leading dates earn zero until an observed rate is available.
    """
    try:
        rates = load_treasury_yield(cache) if offline else download_treasury_yield(
            cache, start.date().isoformat(), end.date().isoformat()
        )
    except Exception as error:  # noqa: BLE001 - report and fall back to cache
        if not cache.exists():
            raise
        print(f"warning: treasury download failed; using cache: {error}")
        rates = load_treasury_yield(cache)
    rates = rates.copy()
    rates.index = pd.to_datetime(rates.index)
    if rates.empty:
        raise ValueError("treasury rate series is empty")
    covered = rates.loc[rates.index <= end]
    if covered.empty:
        raise ValueError("treasury rate series starts after the ledger ends")
    leading = pd.date_range(start, min(end, covered.index.min() - pd.Timedelta(days=1)))
    leading = leading.difference(covered.index)
    if len(leading):
        print(
            f"note: treasury rates start {covered.index.min().date()}, after the "
            f"ledger start {start.date()}; using zero yield for "
            f"{len(leading)} leading day(s), not a future rate"
        )
        covered = pd.concat(
            [pd.Series(0.0, index=leading), covered]
        ).sort_index()
    return covered


def _build_ledgers(
    daily: pd.DataFrame,
    yield_value: object,
    initial_capital: float,
    monthly_contribution: float,
    static_dca_months: int,
) -> dict[str, pd.DataFrame]:
    ledgers: dict[str, pd.DataFrame] = {}
    for name, overrides in STRATEGIES.items():
        kind = overrides["kind"]
        if kind == "static":
            ledgers[name] = simulate_static_dca(
                daily,
                initial_capital=initial_capital,
                monthly_contribution=monthly_contribution,
                deployment_months=static_dca_months,
                cash_yield_annual=yield_value,
            )
            continue
        if kind == "buy_hold":
            ledgers[name] = simulate_buy_and_hold(
                daily,
                initial_capital=initial_capital,
                monthly_contribution=monthly_contribution,
            )
            continue
        kwargs = {
            key: value for key, value in overrides.items() if key != "kind"
        }
        divisor = kwargs.pop("unit_divisor", None)
        if divisor is not None:
            kwargs["unit_notional"] = initial_capital / float(divisor)
        simulator = (
            simulate_accumulation_dca if kind == "frozen" else simulate_ladder_accumulation
        )
        ledgers[name] = simulator(
            daily,
            initial_capital=initial_capital,
            monthly_contribution=monthly_contribution,
            cash_yield_annual=yield_value,
            **kwargs,
        )
    return ledgers


def _yield_label(value: float) -> str:
    return f"yield_{int(round(value * 100)):02d}"


def _save_wealth_curve(
    swept: pd.DataFrame, output: Path, treasury_mean: float = float("nan")
) -> None:
    constants = swept.loc[swept["scenario"].ne(TREASURY_SCENARIO)].copy()
    constants["rate"] = (
        constants["scenario"].str.replace("yield_", "", regex=False).astype(float)
    )
    fig, axis = plt.subplots(figsize=(11, 7))
    for name, group in constants.groupby("strategy"):
        group = group.sort_values("rate")
        axis.plot(
            group["rate"],
            group["ending_portfolio_value"],
            marker="o",
            label=name,
        )
    treasury = swept.loc[swept["scenario"].eq(TREASURY_SCENARIO)]
    for row in treasury.itertuples():
        # The real bill rate is not a point on the constant-rate axis, so it is
        # drawn as a horizontal level at its own average rate. It used to be
        # scattered at x = NaN, which put it in the legend but nowhere on the
        # chart (audit finding G12).
        axis.axhline(
            row.ending_portfolio_value,
            color="#dc2626",
            linewidth=1.0,
            linestyle=":",
        )
        axis.scatter(
            [treasury_mean],
            [row.ending_portfolio_value],
            marker="*",
            s=110,
            color="#dc2626",
            label=f"{row.strategy} (real DTB3, mean {treasury_mean:.2%})",
        )
    axis.set_xlabel("Annual cash yield on the reserve (fraction)")
    axis.set_ylabel("Ending portfolio value")
    axis.set_yscale("log")
    axis.set_title("One variable: cash yield on an unspent reserve", loc="left", fontweight="bold")
    axis.grid(color="#e5e7eb", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _write_findings(
    swept: pd.DataFrame,
    output: Path,
    initial_capital: float,
    monthly_contribution: float,
    scenarios: dict[str, object],
) -> None:
    dca_by_scenario = swept.loc[swept["strategy"].eq(REFERENCE)].set_index("scenario")
    ladder_by_scenario = swept.loc[
        swept["strategy"].eq(LOW_DRAWDOWN_REFERENCE)
    ].set_index("scenario")
    frozen_by_scenario = swept.loc[
        swept["strategy"].eq("frozen_acc_once_base25_deep")
    ].set_index("scenario")
    treasury_note = (
        "`treasury` uses the real FRED `DTB3` 3-month bill rate, forward-filled "
        "across weekends and holidays."
        if TREASURY_SCENARIO in scenarios
        else "The real Treasury scenario was skipped."
    )
    lines = [
        "# Cash Yield on the Unspent Reserve",
        "",
        f"Horizon: `{PAPER_START}` through `{PAPER_END}`.",
        f"Initial capital: `${initial_capital:,.2f}`; "
        f"monthly contribution: `${monthly_contribution:,.2f}`.",
        "",
        "One variable changes between scenarios: the annual yield earned by cash",
        "that has not been deployed. Every strategy inside a scenario receives the",
        "same rate, including the benchmarks, so the comparison stays honest.",
        "",
        treasury_note,
        "",
        "## The Question",
        "",
        "The low-drawdown ladder variants lose most of their wealth to cash that",
        "earns nothing while it waits. How much of that gap does a realistic yield",
        "close, at the same drawdown?",
        "",
        "## Answer",
        "",
        "| Scenario | Static DCA | Frozen baseline | Ladder floor 60% | Ladder share of DCA | Ladder DD |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {scenario} | ${dca_by_scenario.loc[scenario, 'ending_portfolio_value']:,.0f} | "
            f"${frozen_by_scenario.loc[scenario, 'ending_portfolio_value']:,.0f} | "
            f"${ladder_by_scenario.loc[scenario, 'ending_portfolio_value']:,.0f} | "
            f"{ladder_by_scenario.loc[scenario, 'ending_portfolio_value'] / dca_by_scenario.loc[scenario, 'ending_portfolio_value']:.1%} | "
            f"{ladder_by_scenario.loc[scenario, 'maximum_drawdown_abs']:.2%} |"
            for scenario in scenarios
        ],
        "",
        "## Full Sweep",
        "",
        "| Scenario | Strategy | Ending value | Max DD | Calmar | Cash weight | vs static DCA |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.scenario} | {row.strategy} | ${row.ending_portfolio_value:,.0f} | "
            f"{row.maximum_drawdown_abs:.2%} | {row.calmar_ratio:.2f} | "
            f"{row.average_cash_weight:.1%} | {row.wealth_vs_static_dca:+.1%} |"
            for row in swept.sort_values(
                ["scenario", "maximum_drawdown_abs"]
            ).itertuples()
        ],
        "",
        "## Reading Rules",
        "",
        "- The drawdown identity is unchanged by yield: drawdown is still set by",
        "  the BTC weight held at the peak. Yield only makes the waiting cheaper.",
        "- A constant rate across 2013-2025 is generous history: bills paid near",
        "  zero for years after 2013. Trust the `treasury` scenario over the",
        "  constants, and treat the constants as an upper-bound sensitivity.",
        "- Yield on cash is portfolio return, not free money: taxes, spread, and",
        "  the need for a cash instrument that tracks the bill rate are excluded,",
        "  exactly as the paper excludes fees.",
        "- The OOS boundary stays untouched until one variant is frozen.",
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
