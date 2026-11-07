"""Out-of-sample read of the frozen accumulation model.

Convention (canonical): the read rebases the account at the frozen boundary to
``initial_capital`` and then continues with real monthly contributions, carrying
the position and the rule state across the boundary. This is the only convention
that satisfies the project's own invariant that every model receives identical
cash on identical dates, and the only one under which a locked reserve is
visible at all.

An earlier version of this script scaled the boundary holdings to
``initial_capital`` but left ``net_flow`` in full dollars, so contributions were
charged to the portfolio at roughly 272x their real size. That corrupted the
time-weighted return, drawdown, felt loss, and every flow-based metric. The
scaled-replica numbers are still computed, as ``scaled_replica_crosscheck`` in
the findings, but they are a cross-check, not the headline.
"""

from __future__ import annotations

import argparse
from datetime import date
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
    simulate_accumulation_dca,
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.cost_basis import cost_basis_metrics
from model_development.models.bitcoin_onchain_cycles.data_sources import download_coinmetrics, prepared_window
from model_development.models.bitcoin_onchain_cycles.frozen_accumulation import frozen_accumulation_state
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_START, prepare_daily_data


MODEL_DIR = Path(__file__).resolve().parent
OOS_START = pd.Timestamp("2025-04-13")

# Frozen in ACCUMULATION_CONTRACT.md. Do not edit after reading the OOS result.
FROZEN_RULES: dict[str, object] = {
    "base_share": 0.25,
    "tiers": ((0.50, 0.50), (0.00, 0.75), (-0.20, 1.00)),
    "tier_mode": "once_per_cycle",
    "no_buy_above": 5.00,
    "powder_expiry_days": None,
    "cash_yield_annual": 0.0,
}

FROZEN_NAME = "frozen_acc_once_base25_deep"
BENCHMARK_NAME = "buy_hold_contributions"
LABELS = {
    FROZEN_NAME: "Frozen accumulation model",
    "static_dca": "Static 12-Month DCA",
    BENCHMARK_NAME: "Buy & Hold + Contributions",
    "paper_mvrv_3": "Paper MVRV-3 (sells)",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Out-of-sample read of the frozen accumulation-only model"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=MODEL_DIR / "data" / "source",
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "oos_validation" / "accumulation",
    )
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=100.0)
    parser.add_argument("--static-dca-months", type=int, default=12)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use the prepared daily file instead of refreshing Coin Metrics.",
    )
    args = parser.parse_args()

    daily, source_note = _load_daily(args)
    if OOS_START > daily["date"].iloc[-1]:
        raise ValueError("data does not reach the frozen OOS boundary")

    full_ledgers = _build_full_ledgers(daily, args)
    slice_daily = daily.loc[daily["date"].ge(OOS_START)].reset_index(drop=True)
    ledgers = _resume_ledgers(slice_daily, full_ledgers, daily, args)
    _assert_identical_cash(ledgers, args)

    args.output.mkdir(parents=True, exist_ok=True)
    report = write_portfolio_report(
        ledgers,
        args.output,
        benchmark=BENCHMARK_NAME,
        groups={
            "Frozen candidate": (FROZEN_NAME,),
            "Benchmarks": ("static_dca", BENCHMARK_NAME),
            "Reference (sells)": ("paper_mvrv_3",),
        },
        labels=LABELS,
        title="Frozen Accumulation Model: Out of Sample",
    )
    report = _enrich(report, ledgers)
    # One order for both outputs: the CSV used to be sorted while the findings
    # table kept insertion order, so the same read could be read two ways
    # (audit finding F3).
    report = report.sort_values(
        "ending_portfolio_value", ascending=False
    ).reset_index(drop=True)
    report.to_csv(args.output / "performance_report.csv", index=False)
    _write_ledgers(ledgers, args.output)
    _save_curve(report, args.output / "equity_curves.png")
    _write_findings(
        report,
        ledgers[FROZEN_NAME],
        _scaled_replica_crosscheck(full_ledgers, args),
        args.output / "findings.md",
        slice_daily,
        source_note,
        args.initial_capital,
        args.monthly_contribution,
    )
    print(
        report[
            [
                "strategy",
                "ending_portfolio_value",
                "time_weighted_return",
                "maximum_drawdown_abs",
                "worst_equity_vs_contributions",
                "equity_underwater_days",
                "ending_btc_units",
            ]
        ].to_string(index=False)
    )
    print(f"\nData through {daily['date'].iloc[-1].date()}")
    print(f"Outputs: {args.output.resolve()}")


def _build_full_ledgers(
    daily: pd.DataFrame, args: argparse.Namespace
) -> dict[str, pd.DataFrame]:
    """Run every strategy from the paper start so state exists at the boundary."""
    return {
        FROZEN_NAME: simulate_accumulation_dca(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
            **FROZEN_RULES,
        ),
        "static_dca": simulate_static_dca(
            daily,
            initial_capital=args.initial_capital,
            monthly_contribution=args.monthly_contribution,
            deployment_months=args.static_dca_months,
        ),
        BENCHMARK_NAME: simulate_buy_and_hold(
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


def _boundary_position(
    ledger: pd.DataFrame, boundary: pd.Timestamp, initial_capital: float
) -> tuple[float, float]:
    """Holdings at the boundary, rebased so the account is worth the start value."""
    rows = ledger.loc[ledger["date"].eq(boundary)]
    if len(rows) != 1:
        raise ValueError(f"ledger must hold exactly one row for {boundary.date()}")
    row = rows.iloc[0]
    equity = float(row["equity"])
    if equity <= 0:
        raise ValueError("boundary equity must be positive")
    scale = initial_capital / equity
    return float(row["btc_units"]) * scale, float(row["cash"]) * scale


def _frozen_tier_state(daily: pd.DataFrame, boundary: pd.Timestamp) -> tuple[int, ...]:
    """Tiers already spent in the cycle open at the boundary."""
    history = daily.loc[daily["date"].le(boundary)].reset_index(drop=True)
    state = frozen_accumulation_state(history)
    return tuple(state["tiers_spent_this_cycle"])


def _paper_position_state(
    ledger: pd.DataFrame, daily: pd.DataFrame, boundary: pd.Timestamp
) -> tuple[float, int]:
    """Carried target and ratchet tier for the paper rule, with the check shown.

    The tier is asserted rather than assumed: it is only 0 when the last target
    change was an entry and no elevated level has printed since.
    """
    history = ledger.loc[ledger["date"].le(boundary)]
    changes = history.loc[
        history["target_btc_weight"].ne(history["target_btc_weight"].shift())
    ]
    if changes.empty:
        raise ValueError("paper strategy never changed target before the boundary")
    last = changes.iloc[-1]
    target = float(last["target_btc_weight"])
    if target != 1.0:
        raise ValueError("paper strategy is not fully long at the boundary")
    zscore = daily.set_index("date")["mvrv_zscore"]
    since = zscore.loc[(zscore.index > last["date"]) & (zscore.index <= boundary)]
    if len(since) and float(since.max()) >= 5.0:
        raise ValueError(
            "paper strategy has seen an elevated level since its last entry, so its "
            "ratchet tier at the boundary is not 0"
        )
    return target, 0


def _resume_ledgers(
    slice_daily: pd.DataFrame,
    full_ledgers: dict[str, pd.DataFrame],
    daily: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, pd.DataFrame]:
    """Rebase every account at the boundary and continue with real contributions."""
    capital = args.initial_capital
    contribution = args.monthly_contribution
    positions = {
        name: _boundary_position(ledger, OOS_START, capital)
        for name, ledger in full_ledgers.items()
    }

    ledgers = {
        FROZEN_NAME: simulate_accumulation_dca(
            slice_daily,
            initial_capital=0.0,
            monthly_contribution=contribution,
            initial_btc_units=positions[FROZEN_NAME][0],
            initial_cash=positions[FROZEN_NAME][1],
            initial_spent_tiers=_frozen_tier_state(daily, OOS_START),
            **FROZEN_RULES,
        ),
        "static_dca": simulate_static_dca(
            slice_daily,
            initial_capital=0.0,
            monthly_contribution=contribution,
            deployment_months=args.static_dca_months,
            initial_btc_units=positions["static_dca"][0],
            initial_cash=positions["static_dca"][1],
        ),
        BENCHMARK_NAME: simulate_buy_and_hold(
            slice_daily,
            initial_capital=0.0,
            monthly_contribution=contribution,
            initial_btc_units=positions[BENCHMARK_NAME][0],
            initial_cash=positions[BENCHMARK_NAME][1],
        ),
    }
    target, tier = _paper_position_state(full_ledgers["paper_mvrv_3"], daily, OOS_START)
    ledgers["paper_mvrv_3"] = simulate_mvrv_allocation(
        slice_daily,
        (1.0, 1.0, 0.0),
        initial_capital=0.0,
        monthly_contribution=contribution,
        initial_btc_units=positions["paper_mvrv_3"][0],
        initial_cash=positions["paper_mvrv_3"][1],
        initial_target=target,
        initial_tier=tier,
    )

    for name, ledger in ledgers.items():
        # The boundary holding is opening capital, not a contribution earned
        # inside the window, but it is still money the investor put in, so every
        # cost-basis denominator must include it.
        ledger.loc[0, "net_flow"] = float(ledger.loc[0, "net_flow"]) + capital
        # Economic cost basis is explicitly reset at the comparison boundary.
        # It is not a purchase and must not inflate turnover or transaction counts.
        ledger["opening_btc_units"] = 0.0
        ledger["opening_cost_basis_usd"] = 0.0
        ledger.loc[0, "opening_btc_units"] = positions[name][0]
        ledger.loc[0, "opening_cost_basis_usd"] = positions[name][0] * float(slice_daily["close"].iloc[0])
    return ledgers


def _assert_identical_cash(
    ledgers: dict[str, pd.DataFrame], args: argparse.Namespace
) -> None:
    """Every model must receive identical cash on identical dates."""
    reference_name, reference = next(iter(ledgers.items()))
    for name, ledger in ledgers.items():
        if not ledger["date"].equals(reference["date"]):
            raise ValueError(f"{name} does not share {reference_name}'s dates")
        if not np.array_equal(
            ledger["net_flow"].to_numpy(dtype=float),
            reference["net_flow"].to_numpy(dtype=float),
        ):
            raise ValueError(f"{name} does not receive the same cash as {reference_name}")
    expected_total = args.initial_capital + float(reference["net_flow"].iloc[1:].sum())
    actual_total = float(reference["net_flow"].sum())
    if not np.isclose(actual_total, expected_total):
        raise ValueError(
            f"cash total {actual_total:,.2f} does not match {expected_total:,.2f}"
        )


def _load_daily(args: argparse.Namespace) -> tuple[pd.DataFrame, str]:
    if not args.no_refresh:
        args.source.mkdir(parents=True, exist_ok=True)
        try:
            btc_end = download_coinmetrics(args.source / "btc.csv", args.end)
            daily = prepare_daily_data(
                args.source / "btc.csv",
                None,
                start=PAPER_START,
                end=btc_end.strftime("%Y-%m-%d"),
            )
            return daily, f"Coin Metrics refreshed through {btc_end.date()}"
        # Network and parsing failures only; a programming error must surface.
        except (OSError, ValueError, KeyError) as error:
            print(f"warning: Coin Metrics refresh failed; using prepared data: {error}")
    if not args.prepared.exists():
        raise FileNotFoundError(f"{args.prepared} is missing")
    daily = pd.read_csv(args.prepared, parse_dates=["date"])
    required = {"date", "close", "mvrv_zscore"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"prepared data missing columns: {', '.join(sorted(missing))}")
    daily = prepared_window(daily, PAPER_START, args.end)
    return daily, f"prepared data through {daily['date'].iloc[-1].date()}"


def _scaled_replica_crosscheck(
    full_ledgers: dict[str, pd.DataFrame], args: argparse.Namespace
) -> dict[str, dict[str, float]]:
    """The old convention, with the flow units fixed, as a robustness check.

    Stocks and flows are scaled together, so the replica is internally
    consistent, but the accounts then receive different cash per month and the
    locked reserve becomes 0.1% of the portfolio. Reported for transparency.
    """
    crosscheck: dict[str, dict[str, float]] = {}
    for name, ledger in full_ledgers.items():
        window = ledger.loc[ledger["date"].ge(OOS_START)].copy().reset_index(drop=True)
        scale = args.initial_capital / float(window["equity"].iloc[0])
        for column in (
            "equity",
            "cash",
            "btc_units",
            "traded_notional",
            "btc_bought",
            "purchase_notional",
        ):
            window[column] = window[column].mul(scale)
        window["net_flow"] = window["net_flow"].mul(scale)
        window.loc[0, "net_flow"] = args.initial_capital
        portfolio = calculate_portfolio_metrics(window)
        basis = cost_basis_metrics(window)
        crosscheck[name] = {
            "ending_portfolio_value": portfolio["ending_portfolio_value"],
            "time_weighted_return": portfolio["time_weighted_return"],
            "maximum_drawdown_abs": abs(portfolio["maximum_drawdown"]),
            "worst_equity_vs_contributions": basis["worst_equity_vs_contributions"],
        }
    return crosscheck


def _enrich(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    enriched = report.copy()
    enriched["maximum_drawdown_abs"] = enriched["maximum_drawdown"].abs()
    enriched["number_of_transactions"] = enriched["strategy"].map(
        lambda name: int(ledgers[name]["transaction_count"].sum())
    )
    basis = {name: cost_basis_metrics(ledger) for name, ledger in ledgers.items()}
    for column in (
        "worst_equity_vs_contributions",
        "worst_price_vs_average_cost",
        "equity_underwater_days",
        "equity_underwater_share",
        "ending_profit_vs_contributions",
    ):
        enriched[column] = enriched["strategy"].map(lambda name: basis[name][column])
    return enriched


def _write_ledgers(ledgers: dict[str, pd.DataFrame], output: Path) -> None:
    long = pd.concat(
        [ledger.assign(strategy=name) for name, ledger in ledgers.items()],
        ignore_index=True,
    )
    long.to_csv(output / "daily_ledgers.csv.gz", index=False, compression="gzip")


def _save_curve(report: pd.DataFrame, output: Path) -> None:
    fig, axis = plt.subplots(figsize=(11, 6.5))
    for row in report.itertuples():
        axis.scatter(
            [row.maximum_drawdown_abs],
            [row.ending_portfolio_value],
            s=60,
            marker="s" if row.strategy == "paper_mvrv_3" else "o",
        )
        axis.annotate(
            row.strategy,
            (row.maximum_drawdown_abs, row.ending_portfolio_value),
            xytext=(6, 4),
            textcoords="offset points",
            fontsize=8,
        )
    axis.set_xlabel("Maximum time-weighted drawdown")
    axis.set_ylabel("Ending portfolio value (USD)")
    axis.set_title("Frozen accumulation model, out of sample", loc="left", fontweight="bold")
    axis.grid(color="#e5e7eb", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _write_findings(
    report: pd.DataFrame,
    frozen_ledger: pd.DataFrame,
    crosscheck: dict[str, dict[str, float]],
    output: Path,
    slice_daily: pd.DataFrame,
    source_note: str,
    initial_capital: float,
    monthly_contribution: float,
) -> None:
    frozen = report.loc[report["strategy"].eq(FROZEN_NAME)].iloc[0]
    dca = report.loc[report["strategy"].eq("static_dca")].iloc[0]
    hold = report.loc[report["strategy"].eq(BENCHMARK_NAME)].iloc[0]

    ledger = frozen_ledger.sort_values("date").reset_index(drop=True)
    signals = ledger["signal"].fillna("hold")
    fired = ledger["tiers_fired"].astype(str) if "tiers_fired" in ledger else None
    if fired is not None:
        tier_buys = int(sum(len(entry.split(",")) for entry in fired if entry))
    else:
        tier_buys = int(signals.str.startswith("tier_").sum())
    base_buys = int(signals.eq("base").sum())
    negative_trades = int(ledger["traded_notional"].lt(0).sum())
    inventory_never_falls = bool(ledger["btc_units"].diff().dropna().ge(0).all())

    price = slice_daily.set_index("date")["close"]
    zscore = slice_daily.set_index("date")["mvrv_zscore"]
    trough_date = price.idxmin()
    peak_date = price.loc[:trough_date].idxmax()

    opening = float(ledger.loc[0, "net_flow"])
    contributions = float(ledger["net_flow"].iloc[1:].sum())
    money_in = opening + contributions
    frozen_end = float(frozen["ending_portfolio_value"])
    hold_end = float(hold["ending_portfolio_value"])
    gap = frozen_end - hold_end
    best_name = report.loc[report["ending_portfolio_value"].idxmax(), "strategy"]
    outcome = "ahead of" if gap > 0 else ("behind" if gap < 0 else "level with")

    lines = [
        "# Frozen Accumulation Model: Historical Holdout Replay",
        "",
        "**2026-09-29 correction:** opening BTC receives a boundary-date economic",
        "cost basis, not tax basis, without adding fictitious purchases or turnover.",
        "This retrospective window is not independently certified untouched OOS.",
        "Same-close fills and zero transaction costs remain research assumptions.",
        "",
        "**Revision note.** This file replaces the first read of 2026-09-26, whose",
        "normalization charged contributions to the portfolio without scaling them.",
        "That version reported a time-weighted return of `-15.12%`, a `57.05%`",
        "drawdown, and `-38.50%` felt loss on a `$10,000` base. Every flow-based",
        "number in it was wrong; the numbers below are the corrected read. The",
        "frozen parameters were not touched, so the boundary, the rules, and the",
        "recorded in-sample results are unchanged. See",
        "`research/AUDIT_2026-09-26.md` (findings A1-A4).",
        "",
        "## Convention",
        "",
        f"- Frozen boundary: `{OOS_START.date()}` (see `ACCUMULATION_CONTRACT.md`).",
        f"- Source: {source_note}.",
        f"- Every strategy starts with `${initial_capital:,.0f}` of equity at the",
        "  boundary, carrying its own position and rule state across it, then",
        f"  receives `${monthly_contribution:,.0f}` on the first day of each month.",
        f"- Observable rows: `{len(slice_daily)}` "
        f"(`{slice_daily['date'].iloc[0].date()}` through "
        f"`{slice_daily['date'].iloc[-1].date()}`).",
        f"- Money in over the window: `${money_in:,.2f}` "
        f"(`${opening:,.0f}` opening capital plus `${contributions:,.2f}` contributed).",
        "- External cash inflows are identical for every strategy on every date, and the script",
        "  refuses to write a report when that is not true.",
        "- No parameter was changed after this read.",
        "",
        "## Result",
        "",
        "| Strategy | Ending value | Time-weighted return | Max time-weighted DD | "
        "Felt loss | Days underwater | Share underwater | Ending BTC | Transactions |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | ${row.ending_portfolio_value:,.2f} | "
            f"{row.time_weighted_return:+.2%} | {row.maximum_drawdown_abs:.2%} | "
            f"{row.worst_equity_vs_contributions:.2%} | "
            f"{row.equity_underwater_days} | {row.equity_underwater_share:.1%} | "
            f"{row.ending_btc_units:,.4f} | {row.number_of_transactions} |"
            for row in report.itertuples()
        ],
        "",
        f"The frozen model ends `${frozen_end:,.2f}` against buy-and-hold's "
        f"`${hold_end:,.2f}`, so it is `${abs(gap):,.2f}` {outcome} the best",
        f"benchmark (`{best_name}`). On `${money_in:,.2f}` of money in, the result is",
        f"`{frozen_end / money_in - 1:+.2%}` for the model against "
        f"`{hold_end / money_in - 1:+.2%}` for buy-and-hold: "
        f"{'a small gain' if frozen_end > money_in else 'a small loss'}, not a",
        "meaningful return either way.",
        "",
        "Static DCA and buy-and-hold land on the same cent here, and the paper rule",
        "joins them: a resumed account has no deployment schedule left to run, and no",
        "MVRV exit or entry printed, so all three simply held BTC and invested every",
        "contribution on arrival. Only the frozen model behaves differently, and only",
        "because its reserve is locked.",
        "",
        "## Rule Integrity",
        "",
        f"- Negative trades: `{negative_trades}`; the inventory never falls: "
        f"`{'yes' if inventory_never_falls else 'no'}`.",
        f"- Tier deployments in the window: `{tier_buys}`. Base-tranche buys: `{base_buys}`.",
        f"- MVRV Z-score range in the window: `{zscore.min():.3f}` to "
        f"`{zscore.max():.3f}`, with `{int(zscore.lt(0.50).sum())}` rows below `+0.50`, "
        f"`{int(zscore.lt(0.00).sum())}` below `0.00`, "
        f"`{int(zscore.lt(-0.20).sum())}` below `-0.20`, and "
        f"`{int(zscore.ge(5.00).sum())}` at or above `+5.00`.",
        f"- Price ran from `${price.iloc[0]:,.0f}` to a peak of `${price.loc[peak_date]:,.0f}` "
        f"on `{peak_date.date()}` and a trough of `${price.loc[trough_date]:,.0f}` on "
        f"`{trough_date.date()}`.",
        "",
        "## What This Does And Does Not Say",
        "",
        f"- Time-weighted return: `{frozen['time_weighted_return']:+.2%}` for the model, "
        f"`{dca['time_weighted_return']:+.2%}` for static DCA, "
        f"`{hold['time_weighted_return']:+.2%}` for buy-and-hold. The gap is "
        f"`${abs(gap):,.2f}` on `${initial_capital:,.0f}`, so this is a tie in wealth.",
        f"- Risk: the model's drawdown is `{frozen['maximum_drawdown_abs']:.2%}` against "
        f"`{hold['maximum_drawdown_abs']:.2%}` for buy-and-hold, and its felt loss is "
        f"`{frozen['worst_equity_vs_contributions']:.2%}` against "
        f"`{hold['worst_equity_vs_contributions']:.2%}`. That edge is the cash cushion:",
        "  the locked reserve means only 25% of each contribution was invested, so the",
        "  account carried cash through the decline. It is a consequence of the reserve",
        "  being unusable, not of the on-chain signal being right.",
        "- The premise was never exercised. A buy-the-capitulation rule needs a",
        "  capitulation, and the Z-score never fell below `0.00` in this window, so",
        "  the paper's own `-0.20` entry level never printed.",
        f"- The read exposes a rule weakness: `{tier_buys}` tier deployments during a",
        "  `50%`-plus price drawdown. Re-arming only at Z `+5.00` left the reserve",
        "  locked for the whole decline, because the cycle never re-armed. That is an",
        "  observation about the rule, not a licence to change it: any change turns",
        "  this read into development history.",
        f"- `{len(slice_daily)}` rows cannot test a cycle rule. Treat the window as an",
        "  integrity check that passed, not as validation of the on-chain premise.",
        "",
        "## Scaled-Replica Cross-Check",
        "",
        "The same window under the older convention, with the flow units fixed so it",
        "is at least internally consistent. Stocks and flows are both divided by the",
        "boundary equity, which means each strategy receives a different amount of",
        "cash per month and the locked reserve is about `0.1%` of the portfolio. It is",
        "reported because it is the convention the first read used.",
        "",
        "| Strategy | Ending value | Time-weighted return | Max time-weighted DD | Felt loss |",
        "| --- | ---: | ---: | ---: | ---: |",
        *[
            f"| {name} | ${row['ending_portfolio_value']:,.2f} | "
            f"{row['time_weighted_return']:+.2%} | {row['maximum_drawdown_abs']:.2%} | "
            f"{row['worst_equity_vs_contributions']:.2%} |"
            for name, row in crosscheck.items()
        ],
        "",
        "## Status",
        "",
        "This read is now observed data. Any change to the frozen parameters turns",
        "it into development history, and genuine out-of-sample evidence would then",
        "require later unseen dates or prospective paper trading.",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
