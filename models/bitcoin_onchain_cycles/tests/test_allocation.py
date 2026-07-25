from __future__ import annotations

import pandas as pd
import pytest

from models.bitcoin_onchain_cycles.allocation import (
    simulate_cvdd_confirmed_allocation,
    simulate_mvrv_allocation,
    simulate_rollover_allocation,
)


def test_mvrv_allocation_compounds_sales_cash_and_contributions() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2020-01-01", "2020-02-01", "2020-03-01", "2020-04-01"]
            ),
            "close": [100.0, 200.0, 300.0, 100.0],
            "mvrv_zscore": [-0.3, 5.1, 7.1, -0.3],
        }
    )

    ledger = simulate_mvrv_allocation(
        daily,
        (0.75, 0.5, 0.25),
        initial_capital=100.0,
        monthly_contribution=20.0,
    )

    assert ledger["target_btc_weight"].tolist() == [1.0, 0.75, 0.25, 1.0]
    assert ledger["equity"].tolist() == pytest.approx([100.0, 220.0, 322.5, 288.75])
    assert ledger["cash"].tolist() == pytest.approx([0.0, 55.0, 241.875, 0.0])
    assert ledger["btc_units"].tolist() == pytest.approx([1.0, 0.825, 0.26875, 2.8875])
    assert ledger["net_flow"].tolist() == [100.0, 20.0, 20.0, 20.0]
    assert ledger["transaction_count"].sum() == 4


def test_combined_rollover_waits_for_both_indicators_and_resets_at_value() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=6, freq="D"),
            "close": [100.0, 200.0, 180.0, 160.0, 300.0, 100.0],
            "mvrv_zscore": [-0.3, 2.1, 1.4, 1.0, 7.1, -0.3],
            "nupl": [-0.1, 0.51, 0.44, 0.40, 0.70, -0.1],
        }
    )

    ledger = simulate_rollover_allocation(
        daily,
        initial_capital=100.0,
        rollover_mode="combined",
        rollover_target=0.5,
    )

    assert ledger["target_btc_weight"].tolist() == [1.0, 1.0, 1.0, 0.5, 0.0, 1.0]
    assert ledger["signal"].tolist() == [
        "cycle_reset",
        "rollover_armed",
        "hold",
        "combined_rollover",
        "hard_exit_mvrv_7",
        "cycle_reset",
    ]


def test_cvdd_confirmation_delays_entry_and_keeps_frozen_exit_logic() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=6, freq="D"),
            "close": [150.0, 110.0, 200.0, 160.0, 300.0, 110.0],
            "mvrv_zscore": [-0.3, -0.4, 2.1, 1.0, 7.1, -0.3],
            "nupl": [-0.2, -0.3, 0.51, 0.40, 0.70, -0.2],
            "cvdd": [100.0] * 6,
        }
    )

    ledger = simulate_cvdd_confirmed_allocation(
        daily,
        initial_capital=100.0,
        cvdd_band=0.15,
        exit_mode="mvrv_rollover_50",
    )

    assert ledger["target_btc_weight"].tolist() == [0.0, 1.0, 1.0, 0.5, 0.0, 1.0]
    assert ledger["signal"].tolist() == [
        "hold",
        "cvdd_entry",
        "rollover_armed",
        "mvrv_rollover",
        "hard_exit_mvrv_7",
        "cvdd_entry",
    ]
