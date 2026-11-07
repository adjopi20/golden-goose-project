from __future__ import annotations

import pandas as pd
import pytest

from model_development.models.bitcoin_onchain_cycles.frozen_accumulation import (
    FROZEN_BASE_SHARE,
    FROZEN_CASH_YIELD_ANNUAL,
    FROZEN_INITIAL_CAPITAL,
    FROZEN_MONTHLY_CONTRIBUTION,
    FROZEN_NO_BUY_ABOVE,
    FROZEN_POWDER_EXPIRY_DAYS,
    FROZEN_TIER_MODE,
    FROZEN_TIERS,
    frozen_accumulation_ledger,
    frozen_accumulation_state,
    frozen_parameters,
)
from model_development.models.bitcoin_onchain_cycles.run_daily_monitor import (
    build_signal_report,
    describe_tier_plan,
)


def _synthetic(zscores: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=len(zscores), freq="MS"),
            "close": [100.0] * len(zscores),
            "mvrv_zscore": zscores,
            "nupl": [0.3] * len(zscores),
        }
    )


# A cycle spends all three tiers below Z 0.50, rises back to Z 0.00 without ever
# reaching the re-arm level, then crosses down through -0.20 again. The crossing
# read names tier_3; the frozen rule is locked and can do nothing about it. This
# is the 2025-2026 out-of-sample shape in miniature.
LOCKED_THEN_RECROSSING = _synthetic([1.0, 0.4, -0.3, 0.0, -0.3])


def _state(daily: pd.DataFrame) -> dict:
    """Replay a synthetic history, which does not start at the paper date."""
    return frozen_accumulation_state(daily, expected_start=None)


def test_frozen_parameters_match_the_contract() -> None:
    assert FROZEN_BASE_SHARE == 0.25
    assert FROZEN_TIERS == ((0.50, 0.50), (0.00, 0.75), (-0.20, 1.00))
    assert FROZEN_TIER_MODE == "once_per_cycle"
    assert FROZEN_NO_BUY_ABOVE == 5.00
    assert FROZEN_POWDER_EXPIRY_DAYS is None
    assert FROZEN_CASH_YIELD_ANNUAL == 0.0
    assert FROZEN_INITIAL_CAPITAL == 10_000.0
    assert FROZEN_MONTHLY_CONTRIBUTION == 100.0
    assert frozen_parameters()["sells"] is False


def test_replay_never_sells() -> None:
    daily = _synthetic([1.0, 0.4, -0.3, -0.5, -0.9, 2.0, -0.4])
    ledger = frozen_accumulation_ledger(daily)
    assert not (ledger["action"] == "sell").any()
    assert _state(daily)["replay_totals"]["sells"] == 0


def test_tiers_spend_once_per_cycle_then_the_cycle_locks() -> None:
    state = _state(LOCKED_THEN_RECROSSING)

    # Day 2 spends tier_1; day 3 spends tier_2 and tier_3 on the same close, and
    # the ledger's plain ``signal`` column only names the last of those two.
    assert state["tiers_spent_this_cycle"] == [1, 2, 3]
    assert state["tiers_pending_this_cycle"] == []
    assert state["pending_tier_detail"] == []
    assert state["cycle"] == "locked"
    assert state["reserve_deployable"] is False
    assert state["last_tier_buy"]["tier"] == "tier_3"
    assert state["last_tier_buy"]["tiers_fired_at_that_close"] == [2, 3]
    assert state["replay_totals"]["tier_fires"] == 3
    assert state["replay_totals"]["tier_buy_days"] == 2
    assert state["reserve_last_deployed_date"] == state["last_tier_buy"]["date"]


def test_a_rearm_clears_the_spent_tiers() -> None:
    state = _state(_synthetic([1.0, 0.4, -0.3, 1.0, 5.5, 1.0]))

    assert state["cycle"] == "armed"
    assert state["reserve_deployable"] is True
    assert state["tiers_spent_this_cycle"] == []
    assert state["tiers_pending_this_cycle"] == [1, 2, 3]
    assert state["last_rearm"]["date"] == "2020-05-01"
    assert state["mvrv_zscore_distance_to_rearm"] == pytest.approx(4.0)
    assert describe_tier_plan(state).startswith("tier_1 fires below Z 0.50")


def test_base_tranche_pauses_at_or_above_the_no_buy_level() -> None:
    at_level = _state(_synthetic([1.0, 5.0]))
    assert at_level["base_tranche_enabled"] is False
    assert at_level["contract_action_at_last_close"] == "no_buy"

    below = _state(_synthetic([1.0, 4.99]))
    assert below["base_tranche_enabled"] is True
    assert below["contract_action_at_last_close"] == "base"


def test_a_truncated_history_is_refused_by_default() -> None:
    """A short window invents an armed cycle where the real one is locked."""
    truncated = LOCKED_THEN_RECROSSING.loc[
        LOCKED_THEN_RECROSSING["date"].eq(pd.Timestamp("2020-04-01"))
    ]
    assert _state(truncated)["cycle"] == "armed"  # what a short window claims
    assert _state(LOCKED_THEN_RECROSSING)["cycle"] == "locked"  # the truth
    with pytest.raises(ValueError, match="must be replayed from"):
        frozen_accumulation_state(truncated)


def test_locked_reserve_still_refills_from_contributions() -> None:
    state = _state(LOCKED_THEN_RECROSSING)

    # Tier_3 took the whole reserve on 2020-03-01. The two later contributions
    # still leave 75% each in cash, which is exactly how the real reserve grew
    # to $3,825 while the cycle stayed locked.
    assert state["reserve_cash_usd"] == pytest.approx(150.0)
    assert state["reserve_last_deployed_date"] == "2020-03-01"
    assert state["days_since_last_tier_buy"] == 61


def test_every_copy_of_the_frozen_rule_agrees() -> None:
    """Two other modules restate the frozen parameters; drift must fail loudly."""
    from model_development.models.bitcoin_onchain_cycles.run_ladder_experiment import (
        FROZEN_BASELINE_KWARGS,
    )
    from model_development.models.bitcoin_onchain_cycles.run_oos_accumulation import FROZEN_RULES

    shared = {
        "base_share": FROZEN_BASE_SHARE,
        "tiers": FROZEN_TIERS,
        "tier_mode": FROZEN_TIER_MODE,
    }
    for name, copy in (
        ("run_ladder_experiment.FROZEN_BASELINE_KWARGS", FROZEN_BASELINE_KWARGS),
        ("run_oos_accumulation.FROZEN_RULES", FROZEN_RULES),
    ):
        for key, value in shared.items():
            assert copy[key] == value, f"{name}[{key}] drifted from the contract"

    assert FROZEN_RULES["no_buy_above"] == FROZEN_NO_BUY_ABOVE
    assert FROZEN_RULES["powder_expiry_days"] is FROZEN_POWDER_EXPIRY_DAYS
    assert FROZEN_RULES["cash_yield_annual"] == FROZEN_CASH_YIELD_ANNUAL


def test_monitor_report_defers_to_the_replay_not_the_crossing_read() -> None:
    report = build_signal_report(
        LOCKED_THEN_RECROSSING,
        position="cash",
        coinmetrics_through=pd.Timestamp("2020-05-01"),
        expected_start=None,
    )

    # The old monitor reported exactly this, and it was wrong: the rule was not
    # allowed to act, because the cycle had not re-armed.
    assert report["informative_crossing_read"]["signal"] == "TIER_3"
    assert report["informative_crossing_read"]["tier_unlocked_by_a_fresh_crossing"] is True

    state = report["state"]
    assert state["cycle"] == "locked"
    assert state["reserve_deployable"] is False
    assert state["contract_action_at_last_close"] == "base"
    assert "locked" in report["next_close"]["tiers"]

    # The report describes a never-sell rule and must not carry exit fields.
    assert report["rule"]["sells"] is False
    assert "exit_watch_mvrv_7" not in report
    assert report["coinmetrics_through"] == "2020-05-01"
    assert report["cdd_through"] is None
