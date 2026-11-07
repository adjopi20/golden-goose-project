from __future__ import annotations

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {"date", "equity", "asset_price", "net_flow"}


def cost_basis_ledger(ledger: pd.DataFrame) -> pd.DataFrame:
    """Add cumulative contributions and cost-basis drawdown columns.

    Two different "underwater" questions are measured:

    - ``equity_vs_contributions``: portfolio equity against every dollar put in,
      which is the loss an investor actually feels. Contributions raise the
      denominator, so it can only be compared between strategies that receive
      identical cash on identical dates.
    - ``price_vs_average_cost``: market price against the average price actually
      paid for the stack, which is how deep the holding trades below its own
      cost basis.
    """
    missing = REQUIRED_COLUMNS - set(ledger.columns)
    if missing:
        raise ValueError(f"ledger missing columns: {', '.join(sorted(missing))}")
    data = ledger.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    if data["date"].isna().any() or data["date"].duplicated().any():
        raise ValueError("ledger dates must be valid and unique")
    data = data.sort_values("date").reset_index(drop=True)

    for column in ("equity", "asset_price", "net_flow"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
        if data[column].isna().any():
            raise ValueError(f"ledger {column} must be numeric")
    for column in ("btc_bought", "purchase_notional", "opening_btc_units", "opening_cost_basis_usd"):
        if column not in data:
            data[column] = 0.0
        data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0.0)

    contributions = data["net_flow"].clip(lower=0.0).cumsum()
    withdrawals = (-data["net_flow"].clip(upper=0.0)).cumsum()
    net_invested = contributions - withdrawals
    if (net_invested <= 0).any():
        raise ValueError("cumulative net contributions must stay positive")

    data["cumulative_contributions"] = contributions
    data["cumulative_net_invested"] = net_invested
    data["cumulative_purchase_notional"] = data["purchase_notional"].cumsum()
    total_basis = data["cumulative_purchase_notional"] + data["opening_cost_basis_usd"].cumsum()
    btc_bought = data["btc_bought"].cumsum() + data["opening_btc_units"].cumsum()
    data["average_cost_price"] = (
        total_basis.div(btc_bought).where(btc_bought > 0)
    )
    data["equity_vs_contributions"] = data["equity"].div(net_invested).sub(1.0)
    data["price_vs_average_cost"] = (
        data["asset_price"].div(data["average_cost_price"]).sub(1.0)
    )
    return data


def cost_basis_metrics(ledger: pd.DataFrame) -> dict[str, float | int | str | None]:
    """Summarize how far a portfolio ever fell below its own cost basis."""
    frame = cost_basis_ledger(ledger)
    equity_gap = frame["equity_vs_contributions"]
    price_gap = frame["price_vs_average_cost"].dropna()

    equity_trough = int(equity_gap.idxmin())
    recovery = equity_gap.iloc[equity_trough + 1 :]
    recovered = recovery.loc[recovery.ge(0.0)]
    recovery_days = (
        int((frame["date"].iloc[recovered.index[0]] - frame["date"].iloc[equity_trough]).days)
        if len(recovered)
        else None
    )

    equity_underwater = equity_gap.lt(0.0)
    price_underwater = frame["price_vs_average_cost"].lt(0.0)
    return {
        "start_date": frame["date"].iloc[0].date().isoformat(),
        "end_date": frame["date"].iloc[-1].date().isoformat(),
        "total_contributions": float(frame["net_flow"].clip(lower=0.0).sum()),
        "total_withdrawals": float(-frame["net_flow"].clip(upper=0.0).sum()),
        "ending_equity": float(frame["equity"].iloc[-1]),
        "ending_profit_vs_contributions": float(equity_gap.iloc[-1]),
        "final_average_cost_price": float(frame["average_cost_price"].iloc[-1])
        if np.isfinite(frame["average_cost_price"].iloc[-1])
        else np.nan,
        "worst_equity_vs_contributions": float(equity_gap.min()),
        "equity_trough_date": frame["date"].iloc[equity_trough].date().isoformat(),
        "days_to_recover_equity": recovery_days,
        "equity_underwater_days": int(equity_underwater.sum()),
        "equity_underwater_share": float(equity_underwater.mean()),
        "equity_longest_underwater_days": _longest_streak(equity_underwater),
        "worst_price_vs_average_cost": float(price_gap.min())
        if len(price_gap)
        else np.nan,
        "price_underwater_days": int(price_underwater.sum()),
        "price_underwater_share": float(price_underwater.mean()),
        "price_longest_underwater_days": _longest_streak(price_underwater),
    }


def _longest_streak(flags: pd.Series) -> int:
    longest = 0
    current = 0
    for flag in flags:
        current = current + 1 if bool(flag) else 0
        longest = max(longest, current)
    return int(longest)
