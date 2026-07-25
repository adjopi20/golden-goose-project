from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


ENTRY_BELOW = -0.2
WEIGHT_LEVELS = (1.0, 0.75, 0.5, 0.25, 0.0)


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
) -> pd.DataFrame:
    """Simulate a compounded BTC/cash portfolio using MVRV target weights."""
    data = _validated_daily(daily)
    if len(weights) != 3 or not 1.0 >= weights[0] >= weights[1] >= weights[2] >= 0.0:
        raise ValueError("weights must satisfy 1 >= weight_5 >= weight_6 >= weight_7 >= 0")
    targets, events = _mvrv_targets(data["mvrv_zscore"], weights)
    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    return _simulate_targets(data, targets, events, flows)


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
) -> pd.DataFrame:
    """Invest initial capital and every later contribution fully in BTC."""
    data = _validated_daily(daily)
    targets = pd.Series(1.0, index=data.index)
    events = pd.Series(False, index=data.index)
    events.iloc[0] = True
    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    return _simulate_targets(data, targets, events, flows)


def simulate_static_dca(
    daily: pd.DataFrame,
    *,
    initial_capital: float,
    monthly_contribution: float,
    deployment_months: int = 12,
) -> pd.DataFrame:
    """Deploy initial capital over fixed monthly tranches; invest contributions on arrival."""
    data = _validated_daily(daily)
    if deployment_months <= 0:
        raise ValueError("deployment_months must be positive")
    flows = _external_flows(data["date"], initial_capital, monthly_contribution)
    month_start = _month_start_mask(data["date"])
    tranche = initial_capital / deployment_months
    tranches_left = deployment_months
    cash = 0.0
    btc_units = 0.0
    rows: list[dict[str, float | int | pd.Timestamp | str]] = []

    for index, row in data.iterrows():
        price = float(row["close"])
        flow = float(flows.iat[index])
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


def _mvrv_targets(
    zscore: pd.Series,
    weights: tuple[float, float, float],
) -> tuple[pd.Series, pd.Series]:
    target = 0.0
    tier = -1
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
) -> pd.DataFrame:
    cash = 0.0
    btc_units = 0.0
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
    if initial_capital <= 0 or monthly_contribution < 0:
        raise ValueError("initial_capital must be positive and monthly_contribution non-negative")
    flows = pd.Series(0.0, index=dates.index)
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
