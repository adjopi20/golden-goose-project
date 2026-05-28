from __future__ import annotations

from typing import Any

import pandas as pd


def calculate_performance_metrics(trades_df: pd.DataFrame) -> dict[str, Any]:
    """Calculate performance metrics from closed trades with non-null realized_r."""
    default = {
        "total_trades": 0,
        "win_rate": 0.0,
        "tp1_hit_rate": 0.0,
        "total_r": 0.0,
        "avg_r": 0.0,
        "median_r": 0.0,
        "max_drawdown_r": 0.0,
        "profit_factor": None,
        "expectancy_r": 0.0,
        "longest_losing_streak": 0,
    }

    if trades_df is None or trades_df.empty:
        return default

    closed = trades_df[(trades_df["is_closed"] == True) & (trades_df["realized_r"].notna())].copy()
    if closed.empty:
        return default

    realized = pd.to_numeric(closed["realized_r"], errors="coerce").dropna()
    if realized.empty:
        return default

    total_trades = int(len(realized))
    wins = realized[realized > 0]
    losses = realized[realized < 0]

    win_rate = float((realized > 0).mean()) if total_trades > 0 else 0.0
    loss_rate = 1.0 - win_rate
    tp1_hit_rate = float(pd.to_numeric(closed["tp1_hit"], errors="coerce").fillna(0).astype(float).mean())

    total_r = float(realized.sum())
    avg_r = float(realized.mean())
    median_r = float(realized.median())

    equity_r = realized.cumsum()
    peak_r = equity_r.cummax()
    drawdown_r = equity_r - peak_r
    max_drawdown_r = float(drawdown_r.min()) if not drawdown_r.empty else 0.0

    gross_profit_r = float(wins.sum()) if not wins.empty else 0.0
    gross_loss_r = float(abs(losses.sum())) if not losses.empty else 0.0

    if gross_loss_r == 0:
        if gross_profit_r > 0:
            profit_factor: float | None = float("inf")
        else:
            profit_factor = None
    else:
        profit_factor = gross_profit_r / gross_loss_r

    avg_win_r = float(wins.mean()) if not wins.empty else 0.0
    avg_loss_r_abs = float(abs(losses.mean())) if not losses.empty else 0.0
    expectancy_r = (win_rate * avg_win_r) - (loss_rate * avg_loss_r_abs)

    longest_losing_streak = 0
    current_streak = 0
    for r in realized:
        if r < 0:
            current_streak += 1
            if current_streak > longest_losing_streak:
                longest_losing_streak = current_streak
        else:
            current_streak = 0

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "tp1_hit_rate": tp1_hit_rate,
        "total_r": total_r,
        "avg_r": avg_r,
        "median_r": median_r,
        "max_drawdown_r": max_drawdown_r,
        "profit_factor": profit_factor,
        "expectancy_r": float(expectancy_r),
        "longest_losing_streak": int(longest_losing_streak),
    }
