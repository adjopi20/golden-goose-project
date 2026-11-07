"""Live state of the frozen accumulation model.

Section 1 of ``ACCUMULATION_CONTRACT.md`` freezes exactly one parameter set.
Every tool that reports what that rule would do must read the frozen values
from here rather than re-deriving them, otherwise the report silently drifts
from the contract. The live monitor used to infer tier crossings from the last
two MVRV Z-scores alone, which cannot tell an armed cycle from a locked one:
during the 2025-2026 decline it kept printing tier thresholds that the frozen
rule was not allowed to act on.

This module does not reimplement the state machine. It replays the reference
simulator named by the contract, ``allocation.simulate_accumulation_dca``, with
the frozen parameters, and then reads the resulting ledger. The ledger is what
the recorded in-sample and out-of-sample results were produced with, so a
disagreement between a report and the ledger is a bug in the report.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from model_development.models.bitcoin_onchain_cycles.allocation import simulate_accumulation_dca
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_START


FROZEN_MODEL = "frozen_acc_once_base25_deep"
FROZEN_ON = "2026-09-26"
FROZEN_CONTRACT = "model_development/models/bitcoin_onchain_cycles/ACCUMULATION_CONTRACT.md"

# Transcribed from ACCUMULATION_CONTRACT.md, section 1. Do not edit these to
# explore an idea: changing one turns the recorded OOS read
# (research/oos_validation/accumulation/) into development history and a later
# unseen window is needed for a genuine test.
FROZEN_BASE_SHARE = 0.25
FROZEN_TIERS: tuple[tuple[float, float], ...] = (
    (0.50, 0.50),
    (0.00, 0.75),
    (-0.20, 1.00),
)
FROZEN_TIER_MODE = "once_per_cycle"
FROZEN_NO_BUY_ABOVE = 5.00
FROZEN_POWDER_EXPIRY_DAYS: int | None = None
FROZEN_CASH_YIELD_ANNUAL = 0.0
FROZEN_INITIAL_CAPITAL = 10_000.0
FROZEN_MONTHLY_CONTRIBUTION = 100.0


def frozen_parameters() -> dict[str, Any]:
    """Return the frozen rule as a JSON-friendly description."""
    return {
        "model": FROZEN_MODEL,
        "frozen_on": FROZEN_ON,
        "contract": FROZEN_CONTRACT,
        "initial_capital_usd": FROZEN_INITIAL_CAPITAL,
        "monthly_contribution_usd": FROZEN_MONTHLY_CONTRIBUTION,
        "base_share": FROZEN_BASE_SHARE,
        "tiers": {
            f"tier_{number}": {
                "buy_below_mvrv_zscore": threshold,
                "reserve_fraction": fraction,
            }
            for number, (threshold, fraction) in enumerate(FROZEN_TIERS, start=1)
        },
        "tier_mode": FROZEN_TIER_MODE,
        "base_pause_at_or_above_mvrv_zscore": FROZEN_NO_BUY_ABOVE,
        "cycle_rearm_at_or_above_mvrv_zscore": FROZEN_NO_BUY_ABOVE,
        "powder_expiry_days": FROZEN_POWDER_EXPIRY_DAYS,
        "cash_yield_annual": FROZEN_CASH_YIELD_ANNUAL,
        "sells": False,
    }


def frozen_accumulation_ledger(
    daily: pd.DataFrame,
    *,
    initial_capital: float = FROZEN_INITIAL_CAPITAL,
    monthly_contribution: float = FROZEN_MONTHLY_CONTRIBUTION,
) -> pd.DataFrame:
    """Replay the frozen rule over ``daily`` with the reference simulator."""
    ledger = simulate_accumulation_dca(
        daily,
        initial_capital=initial_capital,
        monthly_contribution=monthly_contribution,
        base_share=FROZEN_BASE_SHARE,
        tiers=FROZEN_TIERS,
        no_buy_above=FROZEN_NO_BUY_ABOVE,
        powder_expiry_days=FROZEN_POWDER_EXPIRY_DAYS,
        tier_mode=FROZEN_TIER_MODE,
        cash_yield_annual=FROZEN_CASH_YIELD_ANNUAL,
    )
    sells = int((ledger["action"] == "sell").sum())
    if sells:
        raise RuntimeError(f"the frozen accumulation rule must never sell, saw {sells}")
    return ledger


def frozen_accumulation_state(
    daily: pd.DataFrame,
    *,
    expected_start: str | pd.Timestamp | None = PAPER_START,
    initial_capital: float = FROZEN_INITIAL_CAPITAL,
    monthly_contribution: float = FROZEN_MONTHLY_CONTRIBUTION,
) -> dict[str, Any]:
    """Return today's true state of the frozen rule, replayed from history.

    The replay must start at the paper's own start date: tier and cycle state
    carry across every later date, so a short window reports a rule that never
    existed — a frame beginning in 2025 says the cycle is ``armed`` with reserve
    left, when the real cycle has been locked since 2022. ``expected_start``
    enforces that by default; pass ``None`` only for synthetic histories in
    tests, where the caller owns the interpretation.
    """
    if expected_start is not None:
        first_date = pd.Timestamp(daily["date"].iloc[0])
        if first_date != pd.Timestamp(expected_start):
            raise ValueError(
                "frozen accumulation state must be replayed from "
                f"{pd.Timestamp(expected_start).date()}, got {first_date.date()}; "
                "a truncated history silently reports the wrong cycle state"
            )
    ledger = frozen_accumulation_ledger(
        daily,
        initial_capital=initial_capital,
        monthly_contribution=monthly_contribution,
    )
    if ledger.empty:
        raise ValueError("frozen accumulation state needs at least one day of history")

    dates = pd.to_datetime(ledger["date"])
    signals = ledger["signal"].astype(str)
    fired = ledger["tiers_fired"].astype(str)
    zscore = pd.to_numeric(ledger["mvrv_zscore"]).to_numpy(dtype=float)
    today_zscore = float(zscore[-1])

    # The reference simulator clears the spent-tier set on every day whose
    # Z-score is at or above the no-buy level, and a tier can only fire when the
    # Z-score is below its threshold, so the re-arm day itself never spends.
    # The current cycle therefore begins the day after the last re-arm.
    rearm_positions = np.flatnonzero(zscore >= FROZEN_NO_BUY_ABOVE)
    last_rearm_position = int(rearm_positions[-1]) if rearm_positions.size else None
    cycle_start = 0 if last_rearm_position is None else last_rearm_position + 1

    spent_this_cycle = sorted(
        {
            int(number)
            for entry in fired.iloc[cycle_start:]
            for number in entry.split(",")
            if number
        }
    )
    tier_numbers = list(range(1, len(FROZEN_TIERS) + 1))
    pending_this_cycle = [number for number in tier_numbers if number not in spent_this_cycle]

    pending_detail = [
        {
            "tier": f"tier_{number}",
            "buy_below_mvrv_zscore": FROZEN_TIERS[number - 1][0],
            "reserve_fraction": FROZEN_TIERS[number - 1][1],
        }
        for number in pending_this_cycle
    ]

    tier_buy_rows = ledger[fired.ne("").to_numpy()]
    last_tier_buy: dict[str, Any] | None = None
    if len(tier_buy_rows):
        last_row = tier_buy_rows.iloc[-1]
        last_fired = [int(number) for number in str(last_row["tiers_fired"]).split(",") if number]
        last_tier_buy = {
            "date": pd.Timestamp(last_row["date"]).date().isoformat(),
            "tier": f"tier_{last_fired[-1]}",
            "tiers_fired_at_that_close": last_fired,
            "day_notional_usd": float(last_row["purchase_notional"]),
            "mvrv_zscore": float(last_row["mvrv_zscore"]),
        }

    last_rearm: dict[str, Any] | None = None
    if last_rearm_position is not None:
        last_rearm = {
            "date": pd.Timestamp(dates.iloc[last_rearm_position]).date().isoformat(),
            "mvrv_zscore": float(zscore[last_rearm_position]),
            "days_since": int((dates.iloc[-1] - dates.iloc[last_rearm_position]).days),
        }

    days_since_last_tier_buy = (
        int((dates.iloc[-1] - pd.Timestamp(last_tier_buy["date"])).days)
        if last_tier_buy is not None
        else None
    )

    return {
        # Whether the always-on base tranche is allowed to buy, not whether it
        # buys today: it only has a budget on an inflow day, so this stays
        # "enabled" on every other day. ``contract_action_at_last_close`` is the
        # field that says what actually happened at the last close.
        "base_tranche_enabled": today_zscore < FROZEN_NO_BUY_ABOVE,
        "cycle": "armed" if pending_this_cycle else "locked",
        # The only lever that can spend the reserve is an unspent tier, so an
        # armed cycle is exactly the condition under which the reserve is
        # deployable at some future close without a re-arm first.
        "reserve_deployable": bool(pending_this_cycle),
        "rearm_requires_mvrv_zscore": FROZEN_NO_BUY_ABOVE,
        "mvrv_zscore_distance_to_rearm": FROZEN_NO_BUY_ABOVE - today_zscore,
        "tiers_spent_this_cycle": spent_this_cycle,
        "tiers_pending_this_cycle": pending_this_cycle,
        "pending_tier_detail": pending_detail,
        # All unspent cash is the reserve pool. On a day at or above the no-buy
        # level the whole inflow waits here too (contract section 1.2), so this
        # is the reserve plus any paused base tranche, not the reserve alone.
        "reserve_cash_usd": float(ledger["cash"].iloc[-1]),
        "reserve_last_deployed_date": (
            last_tier_buy["date"] if last_tier_buy is not None else None
        ),
        "last_rearm": last_rearm,
        "last_tier_buy": last_tier_buy,
        "days_since_last_tier_buy": days_since_last_tier_buy,
        "contract_action_at_last_close": str(signals.iloc[-1]),
        "last_close": {
            "date": pd.Timestamp(dates.iloc[-1]).date().isoformat(),
            "btc_price": float(ledger["asset_price"].iloc[-1]),
            "mvrv_zscore": today_zscore,
            "inflow_usd": float(ledger["net_flow"].iloc[-1]),
            "traded_notional_usd": float(ledger["traded_notional"].iloc[-1]),
            "equity_usd": float(ledger["equity"].iloc[-1]),
            "btc_units": float(ledger["btc_units"].iloc[-1]),
            "btc_weight": float(ledger["actual_btc_weight"].iloc[-1]),
        },
        "replay_totals": {
            "days": int(len(ledger)),
            "first_date": pd.Timestamp(dates.iloc[0]).date().isoformat(),
            "tier_fires": int(sum(len(entry.split(",")) for entry in fired if entry)),
            "tier_buy_days": int(len(tier_buy_rows)),
            "base_buys": int((signals == "base").sum()),
            "paused_base_days": int((signals == "no_buy").sum()),
            "sells": 0,
        },
    }
