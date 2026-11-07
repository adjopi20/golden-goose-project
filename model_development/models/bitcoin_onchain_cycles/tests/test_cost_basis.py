from __future__ import annotations

import pandas as pd
import pytest

from model_development.models.bitcoin_onchain_cycles.cost_basis import (
    cost_basis_ledger,
    cost_basis_metrics,
)


def _ledger() -> pd.DataFrame:
    """Five days: buy 5 BTC, add cash, buy 2 BTC lower, then price recovers."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"]
            ),
            "asset_price": [100.0, 100.0, 50.0, 40.0, 120.0],
            "equity": [1000.0, 1000.0, 850.0, 780.0, 1340.0],
            "net_flow": [1000.0, 0.0, 100.0, 0.0, 0.0],
            "btc_bought": [5.0, 0.0, 2.0, 0.0, 0.0],
            "purchase_notional": [500.0, 0.0, 100.0, 0.0, 0.0],
        }
    )


def test_cost_basis_ledger_tracks_contributions_and_average_cost() -> None:
    frame = cost_basis_ledger(_ledger())

    assert frame["cumulative_contributions"].tolist() == [1000.0, 1000.0, 1100.0, 1100.0, 1100.0]
    assert frame["average_cost_price"].tolist() == pytest.approx(
        [100.0, 100.0, 600.0 / 7.0, 600.0 / 7.0, 600.0 / 7.0]
    )
    assert frame["equity_vs_contributions"].tolist() == pytest.approx(
        [0.0, 0.0, 850.0 / 1100.0 - 1.0, 780.0 / 1100.0 - 1.0, 1340.0 / 1100.0 - 1.0]
    )
    assert frame["price_vs_average_cost"].iloc[3] == pytest.approx(40.0 / (600.0 / 7.0) - 1.0)


def test_cost_basis_metrics_measures_both_underwater_questions() -> None:
    metrics = cost_basis_metrics(_ledger())

    assert metrics["total_contributions"] == pytest.approx(1100.0)
    assert metrics["total_withdrawals"] == pytest.approx(0.0)
    assert metrics["ending_equity"] == pytest.approx(1340.0)
    assert metrics["ending_profit_vs_contributions"] == pytest.approx(1340.0 / 1100.0 - 1.0)
    assert metrics["final_average_cost_price"] == pytest.approx(600.0 / 7.0)
    assert metrics["worst_equity_vs_contributions"] == pytest.approx(780.0 / 1100.0 - 1.0)
    assert metrics["equity_trough_date"] == "2020-01-04"
    assert metrics["days_to_recover_equity"] == 1
    assert metrics["equity_underwater_days"] == 2
    assert metrics["equity_underwater_share"] == pytest.approx(0.4)
    assert metrics["equity_longest_underwater_days"] == 2
    assert metrics["worst_price_vs_average_cost"] == pytest.approx(
        40.0 / (600.0 / 7.0) - 1.0
    )
    assert metrics["price_underwater_days"] == 2
    assert metrics["price_longest_underwater_days"] == 2


def test_cost_basis_metrics_reports_never_recovered() -> None:
    ledger = _ledger()
    ledger.loc[4, "asset_price"] = 30.0
    ledger.loc[4, "equity"] = 7.0 * 30.0 + 500.0

    metrics = cost_basis_metrics(ledger)

    assert metrics["days_to_recover_equity"] is None
    assert metrics["equity_underwater_days"] == 3
    assert metrics["equity_longest_underwater_days"] == 3


def test_cost_basis_metrics_rejects_a_ledger_without_prices() -> None:
    ledger = _ledger().drop(columns=["asset_price"])

    with pytest.raises(ValueError):
        cost_basis_ledger(ledger)


def test_cost_basis_metrics_handles_a_strategy_that_never_bought() -> None:
    ledger = _ledger()
    ledger["btc_bought"] = 0.0
    ledger["purchase_notional"] = 0.0

    metrics = cost_basis_metrics(ledger)

    assert pd.isna(metrics["final_average_cost_price"])
    assert pd.isna(metrics["worst_price_vs_average_cost"])
    assert metrics["price_underwater_days"] == 0
