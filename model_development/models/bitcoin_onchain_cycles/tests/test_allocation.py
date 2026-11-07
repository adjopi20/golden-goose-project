from __future__ import annotations

import pandas as pd
import pytest

from model_development.models.bitcoin_onchain_cycles.allocation import (
    simulate_accumulation_dca,
    simulate_cvdd_confirmed_allocation,
    simulate_ladder_accumulation,
    simulate_mvrv_allocation,
    simulate_rollover_allocation,
    simulate_static_dca,
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


def test_accumulation_only_deploys_powder_on_downward_tier_crossings() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=6, freq="D"),
            "close": [100.0] * 6,
            "mvrv_zscore": [3.0, 1.9, 0.4, -0.3, 1.0, 6.0],
        }
    )

    ledger = simulate_accumulation_dca(
        daily,
        initial_capital=1000.0,
        base_share=0.0,
    )

    assert ledger["btc_units"].tolist() == pytest.approx(
        [0.0, 2.5, 6.25, 9.0625, 9.0625, 9.0625]
    )
    assert ledger["cash"].tolist() == pytest.approx(
        [1000.0, 750.0, 375.0, 93.75, 93.75, 93.75]
    )
    assert ledger["signal"].tolist() == [
        "hold",
        "tier_1",
        "tier_2",
        "tier_3",
        "hold",
        "hold",
    ]
    assert ledger["transaction_count"].sum() == 3
    assert ledger["traded_notional"].ge(0.0).all()


def test_accumulation_base_tranche_pauses_at_euphoria_without_selling() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-15", "2020-02-01", "2020-03-01"]),
            "close": [100.0, 100.0, 100.0],
            "mvrv_zscore": [1.0, 6.0, 0.0],
        }
    )

    ledger = simulate_accumulation_dca(
        daily,
        initial_capital=100.0,
        monthly_contribution=100.0,
        base_share=0.5,
        tiers=(),
    )

    assert ledger["net_flow"].tolist() == [100.0, 100.0, 100.0]
    assert ledger["btc_units"].tolist() == pytest.approx([0.5, 0.5, 1.0])
    assert ledger["cash"].tolist() == pytest.approx([50.0, 150.0, 200.0])
    assert ledger["equity"].tolist() == pytest.approx([100.0, 200.0, 300.0])
    assert ledger["signal"].tolist() == ["base", "no_buy", "base"]


def test_accumulation_powder_expiry_forces_deployment_when_zone_never_triggers() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=4, freq="D"),
            "close": [100.0] * 4,
            "mvrv_zscore": [3.0] * 4,
        }
    )

    ledger = simulate_accumulation_dca(
        daily,
        initial_capital=100.0,
        base_share=0.0,
        tiers=(),
        powder_expiry_days=2,
    )

    assert ledger["btc_units"].tolist() == pytest.approx([0.0, 0.0, 1.0, 1.0])
    assert ledger["cash"].tolist() == pytest.approx([100.0, 100.0, 0.0, 0.0])
    assert ledger["signal"].tolist() == ["hold", "hold", "powder_expiry", "hold"]


def test_accumulation_once_per_cycle_preserves_powder_until_rearm() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=8, freq="D"),
            "close": [100.0] * 8,
            "mvrv_zscore": [6.0, 1.0, 1.5, 1.2, 0.4, 0.3, 6.0, 1.5],
        }
    )

    ledger = simulate_accumulation_dca(
        daily,
        initial_capital=1000.0,
        base_share=0.0,
        tier_mode="once_per_cycle",
    )

    assert ledger["btc_units"].tolist() == pytest.approx(
        [0.0, 2.5, 2.5, 2.5, 6.25, 6.25, 6.25, 7.1875]
    )
    assert ledger["cash"].tolist() == pytest.approx(
        [1000.0, 750.0, 750.0, 750.0, 375.0, 375.0, 375.0, 281.25]
    )
    assert ledger["signal"].tolist() == [
        "hold",
        "tier_1",
        "hold",
        "hold",
        "tier_2",
        "hold",
        "hold",
        "tier_1",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_share": 1.5},
        {"tiers": ((0.5, 0.5), (2.0, 0.25))},
        {"tiers": ((2.0, 0.0),)},
        {"no_buy_above": 1.0},
        {"powder_expiry_days": 0},
        {"tier_mode": "weekly"},
    ],
)
def test_accumulation_rejects_invalid_configuration(overrides: dict) -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=3, freq="D"),
            "close": [100.0] * 3,
            "mvrv_zscore": [1.0] * 3,
        }
    )

    with pytest.raises(ValueError):
        simulate_accumulation_dca(daily, initial_capital=100.0, **overrides)


def _ladder_daily() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=6, freq="D"),
            "close": [100.0, 80.0, 60.0, 59.0, 110.0, 70.0],
            "mvrv_zscore": [1.0] * 6,
        }
    )


def test_ladder_fires_each_rung_once_per_cycle_from_shallow_to_deep() -> None:
    ledger = simulate_ladder_accumulation(
        _ladder_daily(),
        initial_capital=1000.0,
        base_share=0.0,
        rearm_days=1,
    )

    assert ledger["btc_units"].tolist() == pytest.approx(
        [0.0, 1.25, 3.5, 6.093220338983051, 6.093220338983051, 6.967505982160926]
    )
    assert ledger["cash"].tolist() == pytest.approx(
        [1000.0, 900.0, 765.0, 612.0, 612.0, 550.8]
    )
    assert ledger["signal"].tolist() == [
        "hold",
        "ladder_1",
        "ladder_2",
        "ladder_3",
        "hold",
        "ladder_1",
    ]
    assert ledger["traded_notional"].ge(0.0).all()


def test_ladder_cooldown_limits_rearms_to_one_per_window() -> None:
    ledger = simulate_ladder_accumulation(
        _ladder_daily(),
        initial_capital=1000.0,
        base_share=0.0,
        rearm_days=30,
    )

    assert ledger["btc_units"].tolist() == pytest.approx([0.0, 1.25, 1.25, 1.25, 1.25, 1.25])
    assert ledger["cash"].tolist() == pytest.approx([1000.0, 900.0, 900.0, 900.0, 900.0, 900.0])
    assert ledger["signal"].tolist() == [
        "hold",
        "ladder_1",
        "cooldown",
        "cooldown",
        "hold",
        "cooldown",
    ]


def test_ladder_reserve_floor_never_deploys_the_whole_budget() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=3, freq="D"),
            "close": [100.0, 100.0, 100.0],
            "mvrv_zscore": [1.0, 1.0, 1.0],
        }
    )

    ledger = simulate_ladder_accumulation(
        daily,
        initial_capital=1000.0,
        base_share=1.0,
        rungs=(),
        reserve_floor=0.25,
    )

    assert ledger["cash"].tolist() == pytest.approx([250.0, 250.0, 250.0])
    assert ledger["btc_units"].tolist() == pytest.approx([7.5, 7.5, 7.5])
    assert ledger["signal"].tolist() == ["base", "hold", "hold"]
    assert ledger["cash"].iloc[-1] / 1000.0 == pytest.approx(0.25)


def test_ladder_fixed_unit_sizing_keeps_rungs_from_draining_the_reserve() -> None:
    ledger = simulate_ladder_accumulation(
        _ladder_daily(),
        initial_capital=1000.0,
        base_share=0.0,
        sizing="fixed_unit",
        unit_notional=50.0,
        reserve_floor=0.0,
        rearm_days=1,
    )

    assert ledger["btc_units"].tolist() == pytest.approx(
        [0.0, 0.625, 1.4583333333333333, 2.3057909604519775, 2.3057909604519775, 3.020076674737692]
    )
    assert ledger["cash"].tolist() == pytest.approx(
        [1000.0, 950.0, 900.0, 850.0, 850.0, 800.0]
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_share": 1.5},
        {"reserve_floor": 1.0},
        {"rungs": ((-0.30, 0.15), (-0.20, 0.10))},
        {"rungs": ((-0.20, 0.0),)},
        {"rungs": ((0.2, 0.1),)},
        {"sizing": "fixed_unit"},
        {"sizing": "weekly"},
        {"rearm_days": 0},
        {"rearm_drawdown": 0.0},
    ],
)
def test_ladder_rejects_invalid_configuration(overrides: dict) -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=3, freq="D"),
            "close": [100.0] * 3,
            "mvrv_zscore": [1.0] * 3,
        }
    )

    with pytest.raises(ValueError):
        simulate_ladder_accumulation(daily, initial_capital=100.0, **overrides)


def _flat_daily(days: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=days, freq="D"),
            "close": [100.0] * days,
            "mvrv_zscore": [1.0] * days,
        }
    )


def test_cash_yield_compounds_daily_on_an_unspent_reserve() -> None:
    ledger = simulate_ladder_accumulation(
        _flat_daily(366),
        initial_capital=1000.0,
        base_share=0.0,
        rungs=(),
        reserve_floor=0.0,
        cash_yield_annual=0.365,
    )

    # 365 accruals at (1 + 0.365) ** (1 / 365) reproduce the annual rate.
    assert ledger["cash"].iloc[-1] == pytest.approx(1365.0, rel=1e-9)
    assert ledger["btc_units"].iloc[-1] == 0.0


def test_cash_yield_accepts_a_dated_series_aligned_to_the_ledger() -> None:
    daily = _flat_daily(366)
    scalar = simulate_ladder_accumulation(
        daily,
        initial_capital=1000.0,
        base_share=0.0,
        rungs=(),
        reserve_floor=0.0,
        cash_yield_annual=0.05,
    )
    series = simulate_ladder_accumulation(
        daily,
        initial_capital=1000.0,
        base_share=0.0,
        rungs=(),
        reserve_floor=0.0,
        cash_yield_annual=pd.Series(
            0.05, index=pd.date_range("2019-01-01", periods=800, freq="D")
        ),
    )

    assert series["equity"].tolist() == pytest.approx(scalar["equity"].tolist())


def test_static_dca_cash_yield_lifts_equity_without_changing_the_stack() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=400, freq="D"),
            "close": [100.0] * 400,
            "mvrv_zscore": [1.0] * 400,
        }
    )
    without = simulate_static_dca(
        daily, initial_capital=1000.0, monthly_contribution=0.0
    )
    with_yield = simulate_static_dca(
        daily,
        initial_capital=1000.0,
        monthly_contribution=0.0,
        cash_yield_annual=0.05,
    )

    assert with_yield["btc_units"].iloc[-1] == pytest.approx(
        without["btc_units"].iloc[-1]
    )
    assert with_yield["equity"].iloc[-1] > without["equity"].iloc[-1]
    # Fixed dollar tranches leave the interest itself unspent.
    assert without["cash"].iloc[-1] == pytest.approx(0.0)
    assert with_yield["cash"].iloc[-1] > 0.0


@pytest.mark.parametrize(
    "cash_yield_annual",
    [-1.0, float("inf"), pd.Series(dtype=float)],
)
def test_cash_yield_rejects_unusable_rates(cash_yield_annual: object) -> None:
    with pytest.raises(ValueError):
        simulate_ladder_accumulation(
            _flat_daily(3),
            initial_capital=100.0,
            rungs=(),
            cash_yield_annual=cash_yield_annual,
        )


def test_cash_yield_accepts_the_slightly_negative_bill_rates_of_2015_and_2020() -> None:
    ledger = simulate_ladder_accumulation(
        _flat_daily(366),
        initial_capital=1000.0,
        base_share=0.0,
        rungs=(),
        reserve_floor=0.0,
        cash_yield_annual=-0.0005,
    )

    assert ledger["cash"].iloc[-1] == pytest.approx(999.5, rel=1e-6)
    assert ledger["cash"].iloc[-1] < 1000.0
