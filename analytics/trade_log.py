from __future__ import annotations

from typing import Any

import pandas as pd


def trades_to_dataframe(trades: list[Any]) -> pd.DataFrame:
    """Convert simulated trades into a normalized DataFrame for analytics/reporting."""
    if not trades:
        return pd.DataFrame(
            columns=[
                "trade_id",
                "direction",
                "entry_timestamp",
                "entry_price",
                "sl_price",
                "tp1_price",
                "risk",
                "tp1_hit",
                "tp1_timestamp",
                "exit_timestamp",
                "exit_price",
                "result",
                "is_closed",
                "runner_r",
                "realized_r",
            ]
        )

    rows: list[dict[str, Any]] = []

    for t in trades:
        is_closed = getattr(t, "result", "open") != "open"
        tp1_hit = bool(getattr(t, "tp1_hit", False)) if is_closed else False
        tp1_r = float(getattr(t, "tp1_r", 4.0))
        risk = abs(float(getattr(t, "entry_price", 0.0)) - float(getattr(t, "sl_price", 0.0)))

        runner_r: float | None = None
        realized_r: float | None = None

        if is_closed:
            if not tp1_hit:
                realized_r = -1.0
            else:
                entry_price = float(getattr(t, "entry_price", 0.0))
                exit_price = float(getattr(t, "exit_price", 0.0))
                direction = str(getattr(t, "direction", "")).lower()

                if risk > 0:
                    if direction == "long":
                        runner_r = (exit_price - entry_price) / risk
                    else:
                        runner_r = (entry_price - exit_price) / risk
                    realized_r = 0.5 * tp1_r + 0.5 * runner_r
                else:
                    runner_r = None
                    realized_r = None

        rows.append(
            {
                "trade_id": getattr(t, "trade_id", None),
                "direction": getattr(t, "direction", None),
                "entry_timestamp": getattr(t, "entry_timestamp", None),
                "entry_price": getattr(t, "entry_price", None),
                "sl_price": getattr(t, "sl_price", None),
                "tp1_price": getattr(t, "tp1_price", None),
                "risk": risk,
                "tp1_hit": tp1_hit,
                "tp1_timestamp": getattr(t, "tp1_timestamp", None),
                "exit_timestamp": getattr(t, "exit_timestamp", None),
                "exit_price": getattr(t, "exit_price", None),
                "result": getattr(t, "result", None),
                "is_closed": is_closed,
                "runner_r": runner_r,
                "realized_r": realized_r,
            }
        )

    return pd.DataFrame(rows)
