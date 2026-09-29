from __future__ import annotations

import pytest

from backtest_engine.exit_policy import ExitPolicy, build_exit_policy_grid


def test_grid_builds_fixed_trailing_and_partial_variants() -> None:
    policies = build_exit_policy_grid(
        modes=("fixed", "trailing_only", "partial_trailing"),
        fixed_targets_r=(1.0, 2.0),
        tp1_r_values=(2.0,),
        tp1_fractions=(0.25, 0.5),
        trail_activation_r_values=(2.0,),
        risk_trail_distances_r=(2.0, 3.0),
        atr_trail_multipliers=(5.0,),
        percentage_trail_callbacks_pct=(2.0,),
        protection_floor_r_values=(None, 0.0),
    )

    assert len(policies) == 26
    assert len({policy.label for policy in policies}) == 26
    assert {policy.mode for policy in policies} == {
        "fixed",
        "trailing_only",
        "partial_trailing",
    }


def test_policy_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError, match="cannot exceed trail activation"):
        ExitPolicy(
            label="invalid",
            mode="trailing_only",
            trail_activation_r=1.0,
            trail_unit="r",
            trail_distance=2.0,
            protection_floor_r=2.0,
        )


def test_grid_is_configuration_only() -> None:
    policy = build_exit_policy_grid(
        modes=("trailing_only",),
        trail_activation_r_values=(4.0,),
        atr_trail_multipliers=(8.0,),
    )[0]

    assert policy.mode == "trailing_only"
    assert policy.trail_activation_r == 4.0
    assert policy.trail_unit == "atr"
    assert policy.trail_distance == 8.0
    assert policy.protection_floor_r is None


def test_grid_skips_floor_that_cannot_exist_at_activation() -> None:
    policies = build_exit_policy_grid(
        modes=("trailing_only",),
        trail_activation_r_values=(1.0, 2.0),
        risk_trail_distances_r=(3.0,),
        protection_floor_r_values=(None, 0.0, 2.0),
    )
    assert len(policies) == 5
    assert all(
        policy.protection_floor_r is None
        or policy.protection_floor_r <= policy.trail_activation_r
        for policy in policies
    )


def test_percentage_trail_policy() -> None:
    policy = build_exit_policy_grid(
        modes=("trailing_only",),
        trail_activation_r_values=(1.0,),
        percentage_trail_callbacks_pct=(1.5,),
    )[0]
    assert policy.trail_unit == "percent"
    assert policy.trail_distance == 1.5
    assert "1p5pct" in policy.label


def test_percentage_above_one_hundred_rejected() -> None:
    with pytest.raises(ValueError, match="cannot exceed 100"):
        ExitPolicy(label="bad", mode="trailing_only", trail_activation_r=1,
                   trail_unit="percent", trail_distance=101)
