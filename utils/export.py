from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def make_excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64tz_dtype(df[col]):
            df[col] = df[col].dt.tz_convert("UTC").dt.tz_localize(None)
        elif df[col].dtype == "object":
            df[col] = df[col].apply(
                lambda x: x.tz_convert("UTC").tz_localize(None)
                if isinstance(x, pd.Timestamp) and x.tz is not None
                else x
            )
    return df


def export_trade_report(trades_df: pd.DataFrame, metrics: dict[str, Any], output_path: str) -> None:
    """Export trade report to .xlsx with required sheets."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    closed = trades_df[(trades_df["is_closed"] == True) & (trades_df["realized_r"].notna())].copy() if not trades_df.empty else pd.DataFrame()

    if closed.empty:
        equity_curve_df = pd.DataFrame(columns=["trade_index", "realized_r", "equity_r"])
        drawdown_df = pd.DataFrame(columns=["trade_index", "equity_r", "peak_r", "drawdown_r"])
    else:
        realized = pd.to_numeric(closed["realized_r"], errors="coerce").fillna(0.0)
        equity_r = realized.cumsum()
        peak_r = equity_r.cummax()
        drawdown_r = equity_r - peak_r

        equity_curve_df = pd.DataFrame(
            {
                "trade_index": range(1, len(closed) + 1),
                "realized_r": realized.values,
                "equity_r": equity_r.values,
            }
        )
        drawdown_df = pd.DataFrame(
            {
                "trade_index": range(1, len(closed) + 1),
                "equity_r": equity_r.values,
                "peak_r": peak_r.values,
                "drawdown_r": drawdown_r.values,
            }
        )

    summary_df = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in metrics.items()]
    )

    config_df = pd.DataFrame(columns=["key", "value"])

    trades_df = make_excel_safe(trades_df)
    summary_df = make_excel_safe(summary_df)
    equity_curve_df = make_excel_safe(equity_curve_df)
    drawdown_df = make_excel_safe(drawdown_df)
    config_df = make_excel_safe(config_df)

    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        trades_df.to_excel(writer, sheet_name="trades", index=False)
        summary_df.to_excel(writer, sheet_name="summary", index=False)
        equity_curve_df.to_excel(writer, sheet_name="equity_curve", index=False)
        drawdown_df.to_excel(writer, sheet_name="drawdown", index=False)
        config_df.to_excel(writer, sheet_name="config", index=False)
