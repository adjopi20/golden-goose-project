from __future__ import annotations

import pandas as pd
import pytest

from analytics.backtest_report import calculate_portfolio_metrics, write_portfolio_report


def test_cash_flow_aware_metrics_and_report(tmp_path) -> None:
    dates = pd.date_range("2025-01-01", periods=4, freq="D")
    ledger = pd.DataFrame(
        {
            "date": dates,
            "equity": [100.0, 110.0, 150.0, 165.0],
            "cash": [100.0, 0.0, 0.0, 0.0],
            "net_flow": [100.0, 0.0, 50.0, 0.0],
            "btc_units": [0.0, 1.0, 1.0, 1.0],
            "btc_bought": [0.0, 1.0, 0.0, 0.0],
            "purchase_notional": [0.0, 110.0, 0.0, 0.0],
            "traded_notional": [0.0, 110.0, 0.0, 0.0],
        }
    )

    metrics = calculate_portfolio_metrics(ledger)

    assert metrics["ending_portfolio_value"] == 165.0
    assert metrics["ending_btc_units"] == 1.0
    assert metrics["average_btc_acquisition_price"] == 110.0
    assert metrics["contributions"] == 150.0
    assert metrics["withdrawals"] == 0.0
    assert metrics["time_weighted_return"] == pytest.approx(0.10)
    assert metrics["maximum_drawdown"] == pytest.approx(-1 / 11)

    report = write_portfolio_report(
        {"buy_hold": ledger, "strategy": ledger},
        tmp_path,
        benchmark="buy_hold",
        groups={"Comparison": ("strategy",)},
    )

    assert len(report) == 2
    assert (tmp_path / "performance_report.csv").exists()
    assert (tmp_path / "equity_curves.png").exists()
