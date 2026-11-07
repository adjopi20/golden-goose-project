"""Audit helper: quantify the OOS normalization bug under three conventions.

Nothing here is a source of truth yet. It exists to show how much the recorded
out-of-sample read moves when the flow units are made consistent, and to price
the choice of convention before one is frozen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.phase_3.portfolio_report import calculate_portfolio_metrics  # noqa: E402
from model_development.models.bitcoin_onchain_cycles.allocation import (  # noqa: E402
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.cost_basis import (  # noqa: E402
    cost_basis_ledger,
    cost_basis_metrics,
)
from model_development.models.bitcoin_onchain_cycles.frozen_accumulation import (  # noqa: E402
    frozen_accumulation_ledger,
)

OOS_START = pd.Timestamp("2025-04-13")
PREPARED = "model_development/models/bitcoin_onchain_cycles/data/prepared/daily.csv"
RECORDED = (
    "model_development/models/bitcoin_onchain_cycles/research/oos_validation/accumulation/performance_report.csv"
)
INITIAL = 10_000.0
MONTHLY = 100.0


def build_full_ledgers(daily: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "frozen": frozen_accumulation_ledger(daily),
        "static_dca": simulate_static_dca(
            daily,
            initial_capital=INITIAL,
            monthly_contribution=MONTHLY,
            deployment_months=12,
        ),
        "buy_hold": simulate_buy_and_hold(
            daily, initial_capital=INITIAL, monthly_contribution=MONTHLY
        ),
        "paper_mvrv_3": simulate_mvrv_allocation(
            daily, (1.0, 1.0, 0.0), initial_capital=INITIAL, monthly_contribution=MONTHLY
        ),
    }


def summarize(ledger: pd.DataFrame, *, start: pd.Timestamp | None = None) -> dict[str, float]:
    frame = ledger if start is None else ledger.loc[ledger["date"].ge(start)].copy()
    frame = frame.reset_index(drop=True)
    portfolio = calculate_portfolio_metrics(frame)
    basis = cost_basis_metrics(frame)
    return {
        "ending_value": portfolio["ending_portfolio_value"],
        "money_in": basis["total_contributions"],
        "twr": portfolio["time_weighted_return"],
        "max_dd": portfolio["maximum_drawdown"],
        "felt_loss": basis["worst_equity_vs_contributions"],
        "underwater_share": basis["equity_underwater_share"],
        "ending_btc": portfolio["ending_btc_units"],
    }


def scaled_replica(full: pd.DataFrame, cut: pd.Timestamp) -> pd.DataFrame:
    """Scale stocks and flows together: the recorded convention, but consistent."""
    result = full.loc[full["date"].ge(cut)].copy().reset_index(drop=True)
    scale = INITIAL / float(result["equity"].iloc[0])
    for column in ("equity", "cash", "btc_units", "traded_notional", "btc_bought", "purchase_notional"):
        result[column] = result[column].mul(scale)
    result["net_flow"] = result["net_flow"].mul(scale)
    result.loc[0, "net_flow"] = INITIAL
    return result


def rebased_account(
    daily: pd.DataFrame,
    full: pd.DataFrame,
    cut: pd.Timestamp,
    *,
    base_share: float,
) -> pd.DataFrame:
    """A fresh $10,000 account at the boundary, $100/month after.

    ``base_share`` is the share of each inflow invested immediately: 0.25 for the
    frozen accumulation rule (the other 75% is undeployable while the cycle is
    locked), 1.0 for the benchmarks that invest every contribution.
    """
    start_row = full.loc[full["date"].ge(cut)].iloc[0]
    price = float(start_row["asset_price"])
    weight = float(start_row["btc_units"]) * price / float(start_row["equity"])
    btc = INITIAL * weight / price
    cash = INITIAL * (1.0 - weight)

    dates = daily.loc[daily["date"].ge(cut), "date"].reset_index(drop=True)
    prices = daily.loc[daily["date"].ge(cut), "close"].reset_index(drop=True)
    zscores = daily.loc[daily["date"].ge(cut), "mvrv_zscore"].reset_index(drop=True)
    month_start = dates.dt.to_period("M").ne(dates.dt.to_period("M").shift())
    rows = []
    for index in range(len(dates)):
        flow = MONTHLY if (month_start.iat[index] and index) else 0.0
        cash += flow
        budget = flow * base_share if index else 0.0
        if budget > 0 and float(zscores.iat[index]) < 5.0:
            spend = min(budget, cash)
            cash -= spend
            btc += spend / float(prices.iat[index])
        close = float(prices.iat[index])
        rows.append(
            {
                "date": dates.iat[index],
                "equity": cash + btc * close,
                "cash": cash,
                "btc_units": btc,
                "asset_price": close,
                "net_flow": flow,
                "traded_notional": 0.0,
                "btc_bought": 0.0,
                "purchase_notional": 0.0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.loc[0, "net_flow"] = INITIAL
    return frame


def summarize_continuation(full: pd.DataFrame, cut: pd.Timestamp) -> dict[str, float]:
    """Convention C: cost basis cumulates from 2013, so the slice cannot reset it."""
    window = full.loc[full["date"].ge(cut)].reset_index(drop=True)
    portfolio = calculate_portfolio_metrics(window)
    basis = cost_basis_ledger(full)
    inside = basis.loc[basis["date"].ge(cut)]
    return {
        "ending_value": portfolio["ending_portfolio_value"],
        "money_in": float(inside["net_flow"].clip(lower=0).sum()),
        "twr": portfolio["time_weighted_return"],
        "max_dd": portfolio["maximum_drawdown"],
        "felt_loss": float(inside["equity_vs_contributions"].min()),
        "underwater_share": float(inside["equity_vs_contributions"].lt(0).mean()),
        "ending_btc": portfolio["ending_btc_units"],
    }


def main() -> None:
    daily = pd.read_csv(PREPARED, parse_dates=["date"])
    full = build_full_ledgers(daily)
    recorded = pd.read_csv(RECORDED).set_index("strategy")

    print("Conventions")
    print("  RECORDED  : stocks scaled 272x, flows left at $100/month (the recorded read)")
    print("  A scaled  : stocks AND flows scaled (internally consistent replica)")
    print("  B rebased : fresh $10,000 account at the boundary, real $100/month, locked reserve")
    print("  C continue: the same account simply continues, nothing rescaled")
    print()

    name_map = {
        "frozen": "frozen_acc_once_base25_deep",
        "static_dca": "static_dca",
        "buy_hold": "buy_hold_contributions",
        "paper_mvrv_3": "paper_mvrv_3",
    }
    for key, recorded_name in name_map.items():
        print(f"=== {recorded_name}")
        row = recorded.loc[recorded_name]
        print(
            f"  RECORDED   ending {row['ending_portfolio_value']:>12,.2f} | "
            f"twr {row['time_weighted_return']:>+8.2%} | dd {row['maximum_drawdown_abs']:>6.2%} | "
            f"felt {row['worst_equity_vs_contributions']:>+8.2%} | btc {row['ending_btc_units']:.6f}"
        )
        for label, stats in (
            ("A scaled  ", summarize(scaled_replica(full[key], OOS_START))),
            ("C continue", summarize_continuation(full[key], OOS_START)),
        ):
            print(
                f"  {label} ending {stats['ending_value']:>12,.2f} | twr {stats['twr']:>+8.2%} | "
                f"dd {stats['max_dd']:>6.2%} | felt {stats['felt_loss']:>+8.2%} | "
                f"btc {stats['ending_btc']:.6f} | money in {stats['money_in']:,.2f}"
            )
        base_share = 0.25 if key == "frozen" else 1.0
        stats = summarize(rebased_account(daily, full[key], OOS_START, base_share=base_share))
        print(
            f"  B rebased  ending {stats['ending_value']:>12,.2f} | twr {stats['twr']:>+8.2%} | "
            f"dd {stats['max_dd']:>6.2%} | felt {stats['felt_loss']:>+8.2%} | "
            f"btc {stats['ending_btc']:.6f} | money in {stats['money_in']:,.2f}"
            f" | base_share {base_share:.2f}"
        )
        print()


if __name__ == "__main__":
    main()
