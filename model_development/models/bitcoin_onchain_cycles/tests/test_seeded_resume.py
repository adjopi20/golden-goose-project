"""Tests for resuming a window with carried holdings and carried rule state.

An out-of-sample slice must continue the account that existed at the boundary
instead of restarting it. These tests prove the resume path reproduces an
unbroken run exactly, which is the guarantee the OOS read depends on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model_development.models.bitcoin_onchain_cycles.allocation import (
    simulate_accumulation_dca,
    simulate_buy_and_hold,
    simulate_mvrv_allocation,
    simulate_static_dca,
)

FROZEN_RULES: dict[str, object] = {
    "base_share": 0.25,
    "tiers": ((0.50, 0.50), (0.00, 0.75), (-0.20, 1.00)),
    "tier_mode": "once_per_cycle",
    "no_buy_above": 5.00,
}


def _wavy_history(periods: int = 1500, amplitude: float = 2.5) -> pd.DataFrame:
    """A deterministic price/Z history that crosses every tier and re-arms."""
    index = np.arange(periods)
    return pd.DataFrame(
        {
            "date": pd.date_range("2018-01-01", periods=periods, freq="D"),
            "close": 1000.0 * np.exp(0.0008 * index) * (1.0 + 0.35 * np.sin(index / 61.0)),
            "mvrv_zscore": amplitude * np.sin(index / 90.0) + 0.6 * np.sin(index / 17.0),
        }
    )


def _split_date(daily: pd.DataFrame) -> pd.Timestamp:
    return pd.Timestamp(daily["date"].iloc[int(len(daily) * 0.7)])


def _spent_tiers_before(ledger: pd.DataFrame, boundary: pd.Timestamp) -> tuple[int, ...]:
    """Tiers already spent in the cycle that is open just before ``boundary``."""
    before = ledger.loc[ledger["date"].lt(boundary)].reset_index(drop=True)
    rearm = np.flatnonzero(before["mvrv_zscore"].to_numpy(dtype=float) >= 5.0)
    start = int(rearm[-1]) + 1 if rearm.size else 0
    spent = {
        int(number)
        for entry in before["tiers_fired"].iloc[start:].astype(str)
        for number in entry.split(",")
        if number
    }
    return tuple(sorted(spent))


def test_zero_initial_capital_is_allowed_and_negative_is_not() -> None:
    daily = _wavy_history(60)
    assert len(simulate_buy_and_hold(daily, initial_capital=0.0)) == 60
    with pytest.raises(ValueError, match="non-negative"):
        simulate_buy_and_hold(daily, initial_capital=-1.0)


def test_default_seeding_is_a_no_op() -> None:
    daily = _wavy_history(400)
    without = simulate_accumulation_dca(
        daily, initial_capital=10_000.0, monthly_contribution=100.0, **FROZEN_RULES
    )
    explicit = simulate_accumulation_dca(
        daily,
        initial_capital=10_000.0,
        monthly_contribution=100.0,
        initial_btc_units=0.0,
        initial_cash=0.0,
        initial_spent_tiers=(),
        **FROZEN_RULES,
    )
    assert without.equals(explicit)


def test_resumed_accumulation_reproduces_an_unbroken_run() -> None:
    daily = _wavy_history()
    split = _split_date(daily)
    full = simulate_accumulation_dca(
        daily, initial_capital=10_000.0, monthly_contribution=100.0, **FROZEN_RULES
    )
    before = full.loc[full["date"].lt(split)].iloc[-1]
    resumed = simulate_accumulation_dca(
        daily.loc[daily["date"].ge(split)].reset_index(drop=True),
        initial_capital=0.0,
        monthly_contribution=100.0,
        initial_btc_units=float(before["btc_units"]),
        initial_cash=float(before["cash"]),
        initial_spent_tiers=_spent_tiers_before(full, split),
        **FROZEN_RULES,
    )

    expected = full.loc[full["date"].ge(split)].reset_index(drop=True)
    assert len(resumed) == len(expected)
    for column in ("equity", "cash", "btc_units", "tiers_fired", "signal"):
        assert resumed[column].tolist() == expected[column].tolist(), column


def test_carried_tier_state_changes_the_answer_for_the_accumulator() -> None:
    """Without the carried tier state the slice invents reserve it cannot spend."""
    daily = _wavy_history()
    split = _split_date(daily)
    full = simulate_accumulation_dca(
        daily, initial_capital=10_000.0, monthly_contribution=100.0, **FROZEN_RULES
    )
    before = full.loc[full["date"].lt(split)].iloc[-1]
    slice_daily = daily.loc[daily["date"].ge(split)].reset_index(drop=True)
    spent = _spent_tiers_before(full, split)

    faithful = simulate_accumulation_dca(
        slice_daily,
        initial_capital=0.0,
        monthly_contribution=100.0,
        initial_btc_units=float(before["btc_units"]),
        initial_cash=float(before["cash"]),
        initial_spent_tiers=spent,
        **FROZEN_RULES,
    )
    naive = simulate_accumulation_dca(
        slice_daily,
        initial_capital=0.0,
        monthly_contribution=100.0,
        initial_btc_units=float(before["btc_units"]),
        initial_cash=float(before["cash"]),
        **FROZEN_RULES,
    )

    assert spent, "the fixture should leave at least one tier spent"
    assert faithful["btc_units"].iloc[-1] < naive["btc_units"].iloc[-1]


@pytest.mark.parametrize("simulator", ["buy_hold", "static_dca"])
def test_resumed_benchmarks_reproduce_an_unbroken_run(simulator: str) -> None:
    daily = _wavy_history(900)
    split = _split_date(daily)
    build = simulate_buy_and_hold if simulator == "buy_hold" else simulate_static_dca
    extra = {} if simulator == "buy_hold" else {"deployment_months": 12}
    full = build(daily, initial_capital=10_000.0, monthly_contribution=100.0, **extra)
    before = full.loc[full["date"].lt(split)].iloc[-1]
    resumed = build(
        daily.loc[daily["date"].ge(split)].reset_index(drop=True),
        initial_capital=0.0,
        monthly_contribution=100.0,
        initial_btc_units=float(before["btc_units"]),
        initial_cash=float(before["cash"]),
        **extra,
    )

    expected = full.loc[full["date"].ge(split)].reset_index(drop=True)
    assert resumed["btc_units"].tolist() == expected["btc_units"].tolist()
    assert resumed["equity"].tolist() == expected["equity"].tolist()


def test_resumed_mvrv_keeps_a_carried_long_position() -> None:
    daily = _wavy_history(900, amplitude=2.5)
    split = _split_date(daily)
    full = simulate_mvrv_allocation(
        daily, (1.0, 1.0, 0.0), initial_capital=10_000.0, monthly_contribution=100.0
    )
    before = full.loc[full["date"].lt(split)].iloc[-1]
    slice_daily = daily.loc[daily["date"].ge(split)].reset_index(drop=True)

    carried = simulate_mvrv_allocation(
        slice_daily,
        (1.0, 1.0, 0.0),
        initial_capital=0.0,
        monthly_contribution=100.0,
        initial_btc_units=float(before["btc_units"]),
        initial_cash=float(before["cash"]),
        initial_target=float(before["target_btc_weight"]),
        initial_tier=0,
    )
    fresh = simulate_mvrv_allocation(
        slice_daily,
        (1.0, 1.0, 0.0),
        initial_capital=0.0,
        monthly_contribution=100.0,
        initial_btc_units=float(before["btc_units"]),
        initial_cash=float(before["cash"]),
    )

    expected = full.loc[full["date"].ge(split)].reset_index(drop=True)
    assert carried["btc_units"].tolist() == expected["btc_units"].tolist()
    # Restarting from flat state instead of carrying it changes the answer: the
    # fresh run re-enters on the slice's own Z < -0.20 print and invests money
    # the real account had already invested before the boundary.
    assert fresh["btc_units"].iloc[-1] != pytest.approx(carried["btc_units"].iloc[-1])


def test_initial_target_must_be_a_weight() -> None:
    daily = _wavy_history(30)
    with pytest.raises(ValueError, match="initial_target"):
        simulate_mvrv_allocation(
            daily, (1.0, 1.0, 0.0), initial_capital=100.0, initial_target=1.5
        )


def test_initial_spent_tiers_must_name_real_tiers() -> None:
    daily = _wavy_history(30)
    with pytest.raises(ValueError, match="initial_spent_tiers"):
        simulate_accumulation_dca(
            daily, initial_capital=100.0, initial_spent_tiers=(4,), **FROZEN_RULES
        )
