from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


ENTRY_BELOW = -0.2
WEIGHT_LEVELS = (1.0, 0.75, 0.5, 0.25, 0.0)

# Accumulation-only defaults. Thresholds are MVRV Z-scores; fractions are the
# share of the remaining dry powder bought when the score crosses down through
# that threshold. These are pre-declared research parameters, not live rules.
ACCUMULATION_TIERS: tuple[tuple[float, float], ...] = (
    (2.0, 0.25),
    (0.5, 0.50),
    (-0.2, 0.75),
)
NO_BUY_ABOVE = 5.0

# Drawdown-ladder defaults. Each rung is (drawdown from the running all-time
# high, share of the reserve bought when that rung fires), ordered from the
# shallowest to the deepest. The floor is the share of all cash ever received
# that is never deployed, so the plan always keeps ammunition for a deeper
# crash, and re-arms are rate limited.
LADDER_RUNGS: tuple[tuple[float, float], ...] = (
    (-0.20, 0.10),
    (-0.30, 0.15),
    (-0.40, 0.20),
    (-0.55, 0.25),
    (-0.70, 0.35),
    (-0.85, 0.50),
)
LADDER_RESERVE_FLOOR = 0.10
LADDER_REARM_DAYS = 30
LADDER_REARM_DRAWDOWN = -0.10
# Rungs are published at round percentages, so a boundary comparison needs a
# tolerance: price/100 - 1 lands at -0.19999999999999996, not exactly -0.20.
LADDER_TOLERANCE = 1e-9


def allocation_variants() -> dict[str, tuple[float, float, float]]:
    """Return the small monotone MVRV 5/6/7 target-weight grid."""
    variants: dict[str, tuple[float, float, float]] = {}
    for weight_5 in WEIGHT_LEVELS:
        for weight_6 in WEIGHT_LEVELS:
            for weight_7 in (0.25, 0.0):
                if weight_5 >= weight_6 >= weight_7:
                    weights = (weight_5, weight_6, weight_7)
                    variants[_variant_name(weights)] = weights
    return variants


def simulate_mvrv_allocation(
    daily: pd.DataFrame,
    weights: tuple[float, float, float],
    *,
    initial_capital: float,
    monthly_contribution: float = 0.0,
    initial_btc_units: float = 0.0,
    initial_cash: float = 0.0,
    initial_target: float | None = None,
    initial_tier: int | None = None,
) -> pd.DataFrame:
    """Simulate a compounded BTC/cash portfolio using MVRV target weights.

    ``initial_target``/``initial_tier`` carry the strategy's own state into the
    window. Without them the rule restarts flat, which for an out-of-sample
    slice would wrongly show a strategy that is long at the boundary sitting in
    cash.
    """
    data = _validated_daily(daily)
    if len(weights) != 3 or not 1.0 >= weights[0] >= weights[1] >= weights[2] >= 0.0:
        raise ValueError("weights must satisfy 1 >= weight_5 >= weight_6 >= weight_7 >= 0")
    if initial_target is not None and not 0.0 <= initial_target <= 1.0:
        raise ValueError("initial_target must be between 0 and 1")
    targets, events = _mvrv_targets(
        data["mvrv_zscore"], weights, initial_target, initial_tier
    )
    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    return _simulate_targets(
        data,
        targets,
        events,
        flows,
        initial_btc_units=initial_btc_units,
        initial_cash=initial_cash,
    )


def simulate_rollover_allocation(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    rollover_mode: str,
    rollover_target: float = 0.5,
    arm_mvrv: float = 2.0,
    arm_nupl: float = 0.5,
    mvrv_drop: float = 1.0,
    nupl_drop: float = 0.1,
) -> pd.DataFrame:
    """Reduce BTC once per cycle after an armed MVRV/NUPL peak rolls over."""
    data = _validated_daily(daily, require_nupl=True)
    if rollover_mode not in {"mvrv", "nupl", "combined"}:
        raise ValueError("rollover_mode must be mvrv, nupl, or combined")
    if not 0.0 <= rollover_target <= 1.0:
        raise ValueError("rollover_target must be between 0 and 1")
    if mvrv_drop <= 0 or nupl_drop <= 0:
        raise ValueError("rollover drops must be positive")

    target = 0.0
    cycle_active = False
    armed = False
    rolled_over = False
    peak_mvrv = -np.inf
    peak_nupl = -np.inf
    targets: list[float] = []
    events: list[bool] = []
    signals: list[str] = []

    for row in data.itertuples():
        previous = target
        signal = "hold"
        if row.mvrv_zscore < ENTRY_BELOW:
            target = 1.0
            if not cycle_active or rolled_over:
                signal = "cycle_reset"
            cycle_active = True
            armed = False
            rolled_over = False
            peak_mvrv = -np.inf
            peak_nupl = -np.inf
        elif cycle_active and row.mvrv_zscore >= 7.0:
            target = 0.0
            signal = "hard_exit_mvrv_7"
            cycle_active = False
            armed = False
        elif cycle_active and not rolled_over:
            arm_now = (
                row.mvrv_zscore >= arm_mvrv
                if rollover_mode == "mvrv"
                else row.nupl >= arm_nupl
                if rollover_mode == "nupl"
                else row.mvrv_zscore >= arm_mvrv and row.nupl >= arm_nupl
            )
            if not armed and arm_now:
                armed = True
                peak_mvrv = row.mvrv_zscore
                peak_nupl = row.nupl
                signal = "rollover_armed"
            elif armed:
                peak_mvrv = max(peak_mvrv, row.mvrv_zscore)
                peak_nupl = max(peak_nupl, row.nupl)
                mvrv_triggered = peak_mvrv - row.mvrv_zscore >= mvrv_drop
                nupl_triggered = peak_nupl - row.nupl >= nupl_drop
                triggered = (
                    mvrv_triggered
                    if rollover_mode == "mvrv"
                    else nupl_triggered
                    if rollover_mode == "nupl"
                    else mvrv_triggered and nupl_triggered
                )
                if triggered:
                    target = rollover_target
                    rolled_over = True
                    signal = f"{rollover_mode}_rollover"

        targets.append(target)
        events.append(target != previous)
        signals.append(signal)

    flows = _external_flows(data["date"], initial_capital, 0.0)
    ledger = _simulate_targets(
        data,
        pd.Series(targets, index=data.index),
        pd.Series(events, index=data.index),
        flows,
    )
    ledger["signal"] = signals
    return ledger


def simulate_cvdd_confirmed_allocation(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    cvdd_band: float,
    exit_mode: str,
) -> pd.DataFrame:
    """Enter only when MVRV is below -0.2 and price is near calculated CVDD."""
    data = _validated_daily(daily, require_nupl=True, require_cvdd=True)
    if cvdd_band <= 0:
        raise ValueError("cvdd_band must be positive")
    if exit_mode not in {"paper", "mvrv_rollover_50", "combined_rollover_75"}:
        raise ValueError(
            "exit_mode must be paper, mvrv_rollover_50, or combined_rollover_75"
        )

    target = 0.0
    cycle_active = False
    armed = False
    rolled_over = False
    peak_mvrv = -np.inf
    peak_nupl = -np.inf
    targets: list[float] = []
    events: list[bool] = []
    signals: list[str] = []

    for row in data.itertuples():
        previous = target
        signal = "hold"
        cvdd_entry = (
            row.mvrv_zscore < ENTRY_BELOW
            and row.close <= row.cvdd * (1.0 + cvdd_band)
        )
        if cvdd_entry:
            target = 1.0
            if not cycle_active or rolled_over:
                signal = "cvdd_entry"
            cycle_active = True
            armed = False
            rolled_over = False
            peak_mvrv = -np.inf
            peak_nupl = -np.inf
        elif cycle_active and row.mvrv_zscore >= 7.0:
            target = 0.0
            signal = "hard_exit_mvrv_7"
            cycle_active = False
            armed = False
        elif cycle_active and exit_mode != "paper" and not rolled_over:
            combined = exit_mode == "combined_rollover_75"
            arm_now = (
                row.mvrv_zscore >= 2.0 and row.nupl >= 0.5
                if combined
                else row.mvrv_zscore >= 2.0
            )
            if not armed and arm_now:
                armed = True
                peak_mvrv = row.mvrv_zscore
                peak_nupl = row.nupl
                signal = "rollover_armed"
            elif armed:
                peak_mvrv = max(peak_mvrv, row.mvrv_zscore)
                peak_nupl = max(peak_nupl, row.nupl)
                mvrv_triggered = peak_mvrv - row.mvrv_zscore >= 1.0
                nupl_triggered = peak_nupl - row.nupl >= 0.1
                if mvrv_triggered and (nupl_triggered or not combined):
                    target = 0.75 if combined else 0.5
                    rolled_over = True
                    signal = (
                        "combined_rollover" if combined else "mvrv_rollover"
                    )

        targets.append(target)
        events.append(target != previous)
        signals.append(signal)

    flows = _external_flows(data["date"], initial_capital, 0.0)
    ledger = _simulate_targets(
        data,
        pd.Series(targets, index=data.index),
        pd.Series(events, index=data.index),
        flows,
    )
    ledger["signal"] = signals
    ledger["price_to_cvdd"] = data["close"].div(data["cvdd"])
    return ledger


def simulate_buy_and_hold(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    monthly_contribution: float = 0.0,
    initial_btc_units: float = 0.0,
    initial_cash: float = 0.0,
) -> pd.DataFrame:
    """Invest initial capital and every later contribution fully in BTC."""
    data = _validated_daily(daily)
    targets = pd.Series(1.0, index=data.index)
    events = pd.Series(False, index=data.index)
    events.iloc[0] = True
    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    return _simulate_targets(
        data,
        targets,
        events,
        flows,
        initial_btc_units=initial_btc_units,
        initial_cash=initial_cash,
    )


def simulate_static_dca(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    monthly_contribution: float,
    deployment_months: int = 12,
    cash_yield_annual: float | pd.Series = 0.0,
    initial_btc_units: float = 0.0,
    initial_cash: float = 0.0,
) -> pd.DataFrame:
    """Deploy initial capital over fixed monthly tranches; invest contributions on arrival.

    When ``initial_btc_units`` or ``initial_cash`` is given, the account is being
    resumed: whatever deployment was going to happen already happened, so the
    tranche schedule does not restart.
    """
    data = _validated_daily(daily)
    if deployment_months <= 0:
        raise ValueError("deployment_months must be positive")
    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    growth = _cash_growth_factors(cash_yield_annual, data)
    month_start = _month_start_mask(data["date"])
    tranche = initial_capital / deployment_months
    resumed = initial_btc_units > 0.0 or initial_cash > 0.0
    tranches_left = 0 if resumed else deployment_months
    cash = float(initial_cash)
    btc_units = float(initial_btc_units)
    rows: list[dict[str, float | int | pd.Timestamp | str]] = []

    for index, row in data.iterrows():
        price = float(row["close"])
        flow = float(flows.iat[index])
        cash *= float(growth[index])
        cash += flow
        purchase = 0.0
        if month_start.iat[index]:
            purchase = monthly_contribution if index else 0.0
            if tranches_left:
                purchase += tranche
                tranches_left -= 1
            purchase = min(purchase, cash)
            btc_units += purchase / price
            cash -= purchase
        equity = cash + btc_units * price
        rows.append(
            _ledger_row(
                row["date"],
                price,
                equity,
                cash,
                btc_units,
                flow,
                purchase,
                purchase,
                int(purchase > 0),
                "buy" if purchase > 0 else "hold",
                np.nan,
            )
        )
    return pd.DataFrame(rows)


def simulate_accumulation_dca(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    monthly_contribution: float = 0.0,
    base_share: float = 0.25,
    tiers: tuple[tuple[float, float], ...] = ACCUMULATION_TIERS,
    no_buy_above: float = NO_BUY_ABOVE,
    powder_expiry_days: int | None = None,
    tier_mode: str = "crossing",
    cash_yield_annual: float | pd.Series = 0.0,
    initial_btc_units: float = 0.0,
    initial_cash: float = 0.0,
    initial_spent_tiers: tuple[int, ...] = (),
) -> pd.DataFrame:
    """Accumulate BTC without ever selling, using MVRV Z-score conditions.

    Every external inflow is split into an always-on ``base_share`` tranche and
    a dry-powder reserve. Powder leaves the reserve only through a tier
    condition; base buying pauses at or above ``no_buy_above`` so the model
    stops adding at euphoria. It never sells, so drawdown can only be shaped by
    what is bought and when.

    ``tier_mode`` controls how often a tier may spend:

    - ``"crossing"`` spends whenever the score crosses *down* through a
      threshold. Repeated shallow crossings drain the reserve geometrically, so
      the reserve is typically gone before the real capitulation.
    - ``"once_per_cycle"`` allows each tier to spend at most once between
      re-arms at or above ``no_buy_above``, which preserves powder across a
      multi-year bear market.
    """
    data = _validated_daily(daily)
    if not 0.0 <= base_share <= 1.0:
        raise ValueError("base_share must be between 0 and 1")
    _validate_accumulation_tiers(tiers, no_buy_above)
    if powder_expiry_days is not None and powder_expiry_days <= 0:
        raise ValueError("powder_expiry_days must be positive when provided")
    if tier_mode not in {"crossing", "once_per_cycle"}:
        raise ValueError("tier_mode must be crossing or once_per_cycle")
    if powder_expiry_days is not None and (initial_cash or initial_btc_units):
        raise ValueError("resuming an expiry strategy requires dated reserve lots")
    tier_numbers = {number for number, _ in enumerate(tiers, start=1)}
    if not set(initial_spent_tiers) <= tier_numbers:
        raise ValueError("initial_spent_tiers must name tiers of this tier set")

    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    growth = _cash_growth_factors(cash_yield_annual, data)
    # Resuming a window: the caller carries the holdings and the tier state in,
    # so the cycle does not restart. ``powder`` arrival times are not carried,
    # which only matters when ``powder_expiry_days`` is set.
    cash = float(initial_cash)
    btc_units = float(initial_btc_units)
    previous_zscore = np.inf
    spent_tiers: set[int] = set(initial_spent_tiers)
    powder: list[tuple[int, float]] = []

    def consume_powder(amount: float, *, newest: bool = False) -> None:
        # Base purchases consume their own inflow; reserve purchases use FIFO.
        while amount > 1e-10 and powder:
            index = -1 if newest else 0
            arrival, balance = powder[index]
            used = min(amount, balance)
            amount -= used
            if balance - used <= 1e-10:
                powder.pop(index)
            else:
                powder[index] = (arrival, balance - used)
    rows: list[dict[str, float | int | pd.Timestamp | str]] = []
    signals: list[str] = []
    fired_tiers_log: list[list[int]] = []
    powder_expired_log: list[float] = []

    for ordinal, (_, row) in enumerate(data.iterrows()):
        price = float(row["close"])
        zscore = float(row["mvrv_zscore"])
        flow = float(flows.iat[ordinal])
        cash *= float(growth[ordinal])
        powder = [(arrival, amount * float(growth[ordinal])) for arrival, amount in powder]
        cash += flow
        if flow > 0:
            powder.append((ordinal, flow))

        spent = 0.0
        signal = "hold"
        fired_tiers: list[int] = []

        expired = 0.0
        if powder_expiry_days is not None and zscore < no_buy_above:
            expired = sum(amount for arrival, amount in powder
                          if ordinal - arrival >= powder_expiry_days)
        if expired > 0:
            amount = min(expired, cash)
            cash -= amount
            spent += amount
            consume_powder(amount)
            signal = "powder_expiry"
        powder_expired_log.append(expired)

        base_budget = flow * base_share
        if base_budget > 0:
            if zscore < no_buy_above:
                amount = min(base_budget, cash)
                cash -= amount
                spent += amount
                consume_powder(amount, newest=True)
                if signal == "hold":
                    signal = "base"
            elif signal == "hold":
                signal = "no_buy"

        if zscore >= no_buy_above:
            spent_tiers.clear()

        for tier_number, (threshold, fraction) in enumerate(tiers, start=1):
            if tier_mode == "crossing":
                triggered = zscore < threshold <= previous_zscore
            else:
                triggered = zscore < threshold and tier_number not in spent_tiers
            if triggered:
                amount = min(fraction * cash, cash)
                cash -= amount
                spent += amount
                consume_powder(amount)
                spent_tiers.add(tier_number)
                fired_tiers.append(tier_number)
                signal = f"tier_{tier_number}"

        btc_units += spent / price
        equity = cash + btc_units * price
        rows.append(
            _ledger_row(
                row["date"],
                price,
                equity,
                cash,
                btc_units,
                flow,
                spent,
                spent,
                int(spent > 0),
                "buy" if spent > 0 else "hold",
                np.nan,
            )
        )
        signals.append(signal)
        fired_tiers_log.append(fired_tiers)
        previous_zscore = zscore

    ledger = pd.DataFrame(rows)
    ledger["signal"] = signals
    # Diagnostic only: ``signal`` records the last tier that fired on a day, so
    # a close that spends two tiers at once would hide one of them. The powder
    # log is likewise exact where the signal label can be overwritten.
    ledger["tiers_fired"] = [",".join(str(n) for n in fired) for fired in fired_tiers_log]
    ledger["powder_expired_notional"] = powder_expired_log
    ledger["mvrv_zscore"] = data["mvrv_zscore"].to_numpy(dtype=float)
    return ledger


def simulate_ladder_accumulation(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    monthly_contribution: float = 0.0,
    base_share: float = 0.35,
    rungs: tuple[tuple[float, float], ...] = LADDER_RUNGS,
    sizing: str = "share_of_reserve",
    unit_notional: float | None = None,
    reserve_floor: float = LADDER_RESERVE_FLOOR,
    rearm_days: int = LADDER_REARM_DAYS,
    rearm_drawdown: float = LADDER_REARM_DRAWDOWN,
    no_buy_above: float = NO_BUY_ABOVE,
    cash_yield_annual: float | pd.Series = 0.0,
) -> pd.DataFrame:
    """Accumulate BTC without selling, using a drawdown ladder and a cash floor.

    Inflows split into an always-on base tranche and a reserve. The reserve is
    spent only when price falls a given distance below its running all-time
    high, one rung per re-arm window, and each rung fires at most once between
    re-arms. ``reserve_floor`` caps cumulative deployment at
    ``1 - reserve_floor`` of every dollar ever received, so the plan never
    reaches 100% invested and always keeps ammunition for a deeper crash.

    ``sizing`` is either ``"share_of_reserve"`` (the rung buys that share of the
    reserve left at that moment) or ``"fixed_unit"`` (the rung buys
    ``unit_notional`` dollars, which keeps shallow rungs from draining the
    reserve).
    """
    data = _validated_daily(daily)
    if not 0.0 <= base_share <= 1.0:
        raise ValueError("base_share must be between 0 and 1")
    if not 0.0 <= reserve_floor < 1.0:
        raise ValueError("reserve_floor must be within [0, 1)")
    if rearm_days < 1:
        raise ValueError("rearm_days must be at least 1")
    if not -1.0 < rearm_drawdown < 0.0:
        raise ValueError("rearm_drawdown must be between -1 (exclusive) and 0")
    _validate_ladder_rungs(rungs, sizing, unit_notional)

    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    growth = _cash_growth_factors(cash_yield_annual, data)
    cash = 0.0
    btc_units = 0.0
    inflows = 0.0
    deployed = 0.0
    price_high = 0.0
    spent_rungs: set[int] = set()
    last_ladder_ordinal: int | None = None
    rows: list[dict[str, float | int | pd.Timestamp | str]] = []
    signals: list[str] = []

    for ordinal, (_, row) in enumerate(data.iterrows()):
        price = float(row["close"])
        zscore = float(row["mvrv_zscore"])
        flow = float(flows.iat[ordinal])
        cash *= float(growth[ordinal])
        cash += flow
        inflows += flow
        price_high = max(price_high, price)
        drawdown = price / price_high - 1.0 if price_high > 0 else 0.0
        if drawdown >= rearm_drawdown - LADDER_TOLERANCE:
            spent_rungs.clear()

        headroom = max((1.0 - reserve_floor) * inflows - deployed, 0.0)
        spent = 0.0
        signal = "hold"

        base_budget = flow * base_share
        if base_budget > 0:
            if zscore < no_buy_above:
                amount = min(base_budget, cash, headroom)
                cash -= amount
                spent += amount
                deployed += amount
                signal = "base" if amount > 0 else "cap"
            else:
                signal = "no_buy"

        ladder_ready = last_ladder_ordinal is None or (
            ordinal - last_ladder_ordinal
        ) >= rearm_days
        headroom = max((1.0 - reserve_floor) * inflows - deployed, 0.0)
        triggered = next(
            (
                (rung_number, threshold, share)
                for rung_number, (threshold, share) in enumerate(rungs, start=1)
                if drawdown <= threshold + LADDER_TOLERANCE
                and rung_number not in spent_rungs
            ),
            None,
        )
        if triggered is not None:
            rung_number, _, share = triggered
            if not ladder_ready:
                if signal in {"hold", "base"}:
                    signal = "cooldown"
            else:
                desired = (
                    unit_notional if sizing == "fixed_unit" else share * cash
                )
                amount = min(float(desired), cash, headroom)
                if amount > 0:
                    cash -= amount
                    spent += amount
                    deployed += amount
                    spent_rungs.add(rung_number)
                    last_ladder_ordinal = ordinal
                    signal = f"ladder_{rung_number}"
                elif signal == "hold":
                    signal = "cap"

        btc_units += spent / price
        equity = cash + btc_units * price
        rows.append(
            _ledger_row(
                row["date"],
                price,
                equity,
                cash,
                btc_units,
                flow,
                spent,
                spent,
                int(spent > 0),
                "buy" if spent > 0 else "hold",
                np.nan,
            )
        )
        signals.append(signal)

    ledger = pd.DataFrame(rows)
    ledger["signal"] = signals
    ledger["mvrv_zscore"] = data["mvrv_zscore"].to_numpy(dtype=float)
    ledger["drawdown_from_high"] = (
        ledger["asset_price"].div(ledger["asset_price"].cummax()).sub(1.0)
    )
    return ledger


def _validate_ladder_rungs(
    rungs: tuple[tuple[float, float], ...],
    sizing: str,
    unit_notional: float | None,
) -> None:
    if sizing not in {"share_of_reserve", "fixed_unit"}:
        raise ValueError("sizing must be share_of_reserve or fixed_unit")
    if sizing == "fixed_unit" and (unit_notional is None or unit_notional <= 0):
        raise ValueError("fixed_unit sizing requires a positive unit_notional")
    thresholds = [threshold for threshold, _ in rungs]
    if thresholds != sorted(thresholds, reverse=True) or len(set(thresholds)) != len(
        thresholds
    ):
        raise ValueError("ladder thresholds must run from shallow to deep")
    if any(not -1.0 < threshold < 0.0 for threshold in thresholds):
        raise ValueError("ladder thresholds must lie between -1 and 0")
    for _, share in rungs:
        if not 0.0 < share <= 1.0:
            raise ValueError("ladder shares must be within (0, 1]")


def _validate_accumulation_tiers(
    tiers: tuple[tuple[float, float], ...],
    no_buy_above: float,
) -> None:
    thresholds = [threshold for threshold, _ in tiers]
    if thresholds != sorted(thresholds, reverse=True) or len(set(thresholds)) != len(
        thresholds
    ):
        raise ValueError("tier thresholds must be strictly descending")
    for _, fraction in tiers:
        if not 0.0 < fraction <= 1.0:
            raise ValueError("tier fractions must be within (0, 1]")
    if tiers and no_buy_above <= thresholds[0]:
        raise ValueError("no_buy_above must be above every tier threshold")


def _cash_growth_factors(
    cash_yield_annual: float | pd.Series,
    data: pd.DataFrame,
) -> np.ndarray:
    """Return the daily gross cash factor for every row of ``data``.

    ``cash_yield_annual`` is a constant annualized decimal rate or a
    date-indexed series of them, such as a Treasury bill rate. Interest accrues
    at the start of each row on cash already held, so a row's own inflow never
    earns interest on its arrival day.
    """
    if isinstance(cash_yield_annual, pd.Series):
        rates = cash_yield_annual.copy()
        rates.index = pd.to_datetime(rates.index)
        rates = rates[~rates.index.duplicated(keep="last")]
        aligned = rates.reindex(pd.DatetimeIndex(data["date"])).ffill()
        if aligned.isna().any():
            raise ValueError(
                "cash_yield_annual series does not cover the ledger dates; only "
                "forward filling is causal, back filling would import a future rate"
            )
        values = aligned.to_numpy(dtype=float)
    else:
        values = np.full(len(data), float(cash_yield_annual), dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("cash_yield_annual must be finite everywhere")
    if (values <= -1.0).any():
        raise ValueError("cash_yield_annual must stay above -1")
    return np.power(1.0 + values, 1.0 / 365.0)


def _mvrv_targets(
    zscore: pd.Series,
    weights: tuple[float, float, float],
    initial_target: float | None = None,
    initial_tier: int | None = None,
) -> tuple[pd.Series, pd.Series]:
    if initial_target is None:
        target = 0.0
        tier = -1
    else:
        # Carried state: the strategy is already in a position, and ``tier``
        # says how far up the ratchet it had climbed. Tier 0 means "entered,
        # no elevated level seen since the last reset".
        target = float(initial_target)
        tier = 0 if initial_tier is None else int(initial_tier)
    targets: list[float] = []
    events: list[bool] = []

    for value in pd.to_numeric(zscore, errors="coerce"):
        previous = target
        if value < ENTRY_BELOW:
            target, tier = 1.0, 0
        elif tier >= 0:
            if value >= 7.0 and tier < 3:
                target, tier = weights[2], 3
            elif value >= 6.0 and tier < 2:
                target, tier = weights[1], 2
            elif value >= 5.0 and tier < 1:
                target, tier = weights[0], 1
        targets.append(target)
        events.append(target != previous)

    return (
        pd.Series(targets, index=zscore.index, dtype=float),
        pd.Series(events, index=zscore.index, dtype=bool),
    )


def _simulate_targets(
    data: pd.DataFrame,
    targets: pd.Series,
    target_events: pd.Series,
    flows: pd.Series,
    *,
    initial_btc_units: float = 0.0,
    initial_cash: float = 0.0,
) -> pd.DataFrame:
    """Walk the target weights, starting from an optional carried position.

    ``initial_btc_units``/``initial_cash`` let a window resume an account that
    already holds assets, which is what an honest out-of-sample slice needs: the
    position is carried across the boundary instead of restarted.
    """
    cash = float(initial_cash)
    btc_units = float(initial_btc_units)
    rows: list[dict[str, float | int | pd.Timestamp | str]] = []

    for index, row in data.iterrows():
        price = float(row["close"])
        flow = float(flows.iat[index])
        cash += flow
        btc_value = btc_units * price
        equity = cash + btc_value
        target = float(targets.iat[index])
        desired_btc_value = equity * target

        if bool(target_events.iat[index]):
            trade_notional = desired_btc_value - btc_value
        elif flow > 0 and btc_value < desired_btc_value:
            trade_notional = min(cash, desired_btc_value - btc_value)
        else:
            trade_notional = 0.0

        trade_notional = min(trade_notional, cash) if trade_notional > 0 else max(
            trade_notional, -btc_value
        )
        btc_units += trade_notional / price
        cash -= trade_notional
        equity = cash + btc_units * price
        purchase = max(trade_notional, 0.0)
        rows.append(
            _ledger_row(
                row["date"],
                price,
                equity,
                cash,
                btc_units,
                flow,
                abs(trade_notional),
                purchase,
                int(abs(trade_notional) > 1e-12),
                "buy" if trade_notional > 0 else "sell" if trade_notional < 0 else "hold",
                target,
            )
        )
    return pd.DataFrame(rows)


def _external_flows(
    dates: pd.Series,
    initial_capital: float,
    monthly_contribution: float,
) -> pd.Series:
    """Return the external cash each day, with 0 meaning "no new capital".

    ``initial_capital=0`` is allowed so a window can resume an account that
    already holds assets, instead of restarting it with fresh capital.
    """
    if initial_capital < 0:
        raise ValueError("initial_capital must be non-negative")
    if monthly_contribution < 0:
        raise ValueError("monthly_contribution must be non-negative")
    flows = pd.Series(0.0, index=dates.index)
    if initial_capital > 0:
        flows.iloc[0] = initial_capital
    if monthly_contribution:
        month_start = _month_start_mask(dates)
        month_start.iloc[0] = False
        flows.loc[month_start] = monthly_contribution
    return flows


def _month_start_mask(dates: pd.Series) -> pd.Series:
    months = pd.to_datetime(dates).dt.to_period("M")
    mask = months.ne(months.shift())
    mask.iloc[0] = True
    return mask


def _validated_daily(
    daily: pd.DataFrame,
    *,
    require_nupl: bool = False,
    require_cvdd: bool = False,
) -> pd.DataFrame:
    if daily.empty:
        raise ValueError("daily data cannot be empty")
    missing = {"date", "close", "mvrv_zscore"} - set(daily.columns)
    if require_nupl:
        missing |= {"nupl"} - set(daily.columns)
    if require_cvdd:
        missing |= {"cvdd"} - set(daily.columns)
    if missing:
        raise ValueError(f"daily data missing columns: {', '.join(sorted(missing))}")
    columns = [
        "date",
        "close",
        "mvrv_zscore",
        *(["nupl"] if require_nupl else []),
        *(["cvdd"] if require_cvdd else []),
    ]
    data = daily[columns].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data["mvrv_zscore"] = pd.to_numeric(data["mvrv_zscore"], errors="coerce")
    if require_nupl:
        data["nupl"] = pd.to_numeric(data["nupl"], errors="coerce")
    if require_cvdd:
        data["cvdd"] = pd.to_numeric(data["cvdd"], errors="coerce")
    if data.isna().any().any() or data["date"].duplicated().any():
        raise ValueError("daily data must contain valid, unique dates and numeric values")
    positive = ["close", *(["cvdd"] if require_cvdd else [])]
    if not np.isfinite(data.drop(columns="date").to_numpy(dtype=float)).all():
        raise ValueError("daily numeric values must be finite")
    if data[positive].le(0).any().any():
        raise ValueError("close and CVDD must be positive")
    return data.sort_values("date").reset_index(drop=True)


def _ledger_row(
    date: pd.Timestamp,
    price: float,
    equity: float,
    cash: float,
    btc_units: float,
    net_flow: float,
    traded_notional: float,
    purchase_notional: float,
    transaction_count: int,
    action: str,
    target_weight: float,
) -> dict[str, float | int | pd.Timestamp | str]:
    return {
        "date": date,
        "asset_price": price,
        "equity": equity,
        "btc_units": btc_units,
        "cash": cash,
        "net_flow": net_flow,
        "traded_notional": traded_notional,
        "btc_bought": purchase_notional / price,
        "purchase_notional": purchase_notional,
        "transaction_count": transaction_count,
        "action": action,
        "target_btc_weight": target_weight,
        "actual_btc_weight": btc_units * price / equity if equity else 0.0,
    }


def _variant_name(weights: tuple[float, float, float]) -> str:
    paper_names: Mapping[tuple[float, float, float], str] = {
        (0.0, 0.0, 0.0): "mvrv_1",
        (1.0, 0.0, 0.0): "mvrv_2",
        (1.0, 1.0, 0.0): "mvrv_3",
    }
    if weights in paper_names:
        return paper_names[weights]
    levels = "_".join(str(round(weight * 100)) for weight in weights)
    return f"tier_{levels}"
