"""Audit helper: show that the recorded OOS differences are scale artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.models.bitcoin_onchain_cycles.allocation import (  # noqa: E402
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_static_dca,
)
from model_development.models.bitcoin_onchain_cycles.frozen_accumulation import (  # noqa: E402
    frozen_accumulation_ledger,
)
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_START  # noqa: E402

BOUNDARY = pd.Timestamp("2025-04-13")
LEDGERS = "model_development/models/bitcoin_onchain_cycles/research/oos_validation/accumulation/daily_ledgers.csv.gz"
PREPARED = "model_development/models/bitcoin_onchain_cycles/data/prepared/daily.csv"


def main() -> None:
    daily = pd.read_csv(PREPARED, parse_dates=["date"])
    recorded = pd.read_csv(LEDGERS, parse_dates=["date"])

    full = {
        "frozen_acc_once_base25_deep": frozen_accumulation_ledger(daily),
        "static_dca": simulate_static_dca(
            daily, initial_capital=10_000.0, monthly_contribution=100.0, deployment_months=12
        ),
        "buy_hold_contributions": simulate_buy_and_hold(
            daily, initial_capital=10_000.0, monthly_contribution=100.0
        ),
        "paper_mvrv_3": simulate_mvrv_allocation(
            daily, (1.0, 1.0, 0.0), initial_capital=10_000.0, monthly_contribution=100.0
        ),
    }

    print("history starts:", daily["date"].iloc[0].date(), "| paper start:", PAPER_START)
    print()
    header = (
        f"{'strategy':32s} {'scale':>9s} {'btc_boundary':>13s} "
        f"{'btc_slice_bought':>16s} {'bought/scale':>13s}"
    )
    print(header)
    for name, ledger in full.items():
        boundary = ledger.loc[ledger["date"].eq(BOUNDARY)].iloc[0]
        scale = 10_000.0 / float(boundary["equity"])
        slice_rows = recorded.loc[recorded["strategy"].eq(name)]
        bought = float(slice_rows["btc_bought"].sum())
        print(
            f"{name:32s} {scale:9.6f} {float(boundary['btc_units']):13.6f} "
            f"{bought:16.6f} {bought / scale:13.6f}"
        )

    print()
    print("If every strategy bought the same BTC per real dollar, the last column")
    print("(scaled back to the original account) should agree across strategies.")
    print()
    weights = recorded.groupby("strategy")["actual_btc_weight"].agg(["first", "last", "min"])
    print("BTC weight inside the recorded OOS slice:")
    print(weights.to_string())


if __name__ == "__main__":
    main()
