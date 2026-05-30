from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go

from executor.trend_following_model import SimulatedTrade
from utils.snapshot_context import SnapshotContext


def _rgba(hex_color: str, opacity: float) -> str:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        raise ValueError(f"Invalid hex color: {hex_color}")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r}, {g}, {b}, {opacity})"


def add_previous_session_volume_profile_overlay(
    fig: go.Figure,
    profile: dict[str, Any],
    overlay_start: pd.Timestamp,
    overlay_end: pd.Timestamp,
    session_end: pd.Timestamp,
    clamp_low: float | None = None,
    clamp_high: float | None = None,
) -> dict[str, Any]:
    profile_bins = profile.get("volume_profile", [])
    if not profile_bins:
        return {"bins_before_clamp": 0, "bins_after_clamp": 0, "max_total_volume": 0.0, "max_abs_delta": 0.0}

    if not isinstance(profile_bins, list):
        raise TypeError(
            "Invalid profile format: expected profile['volume_profile'] to be a list, "
            f"got {type(profile_bins).__name__}"
        )

    overlay_start = pd.Timestamp(overlay_start)
    overlay_end = pd.Timestamp(overlay_end)

    val_low = float(profile["val"]) if profile.get("val") is not None else None
    val_high = float(profile["vah"]) if profile.get("vah") is not None else None
    poc = float(profile["poc_price"]) if profile.get("poc_price") is not None else None

    plotted_bins: list[dict[str, Any]] = []
    for b in profile_bins:
        original_low = float(b["bin_low"])
        original_high = float(b["bin_high"])
        low = original_low
        high = original_high

        if clamp_low is not None and clamp_high is not None:
            if high < clamp_low or low > clamp_high:
                continue
            low = max(low, clamp_low)
            high = min(high, clamp_high)

        if high <= low:
            continue

        in_value_area = False
        if val_low is not None and val_high is not None:
            in_value_area = (original_low > val_low) and (original_high < val_high)

        plotted_bins.append(
            {
                "bin_index": b.get("bin_index"),
                "low": low,
                "high": high,
                "total_volume": float(b.get("total_volume", 0.0)),
                "delta": float(b.get("delta", 0.0)),
                "in_value_area": in_value_area,
            }
        )

    if not plotted_bins:
        return {
            "bins_before_clamp": len(profile_bins),
            "bins_after_clamp": 0,
            "max_total_volume": 0.0,
            "max_abs_delta": 0.0,
        }

    total_volume_candidates = [b["total_volume"] for b in plotted_bins if b["total_volume"] > 0]
    delta_candidates = [abs(b["delta"]) for b in plotted_bins if abs(b["delta"]) > 0]
    max_total_volume = max(total_volume_candidates) if total_volume_candidates else 0.0
    max_abs_delta = max(delta_candidates) if delta_candidates else 0.0

    allowed_total_width = overlay_end - overlay_start
    allowed_delta_width = allowed_total_width * 0.35
    allowed_volume_width = allowed_total_width * 0.65
    center_x = overlay_start + allowed_delta_width

    for b in plotted_bins:
        low = float(b["low"])
        high = float(b["high"])
        in_va = bool(b["in_value_area"])
        total_volume = float(b["total_volume"])
        delta = float(b["delta"])

        if max_total_volume > 0 and total_volume > 0:
            volume_frac = total_volume / max_total_volume
            volume_width = allowed_volume_width * volume_frac
            volume_x0 = center_x
            volume_x1 = center_x + volume_width
            volume_opacity = 0.50 if in_va else 0.20
            fig.add_shape(
                type="rect",
                x0=volume_x0,
                x1=volume_x1,
                y0=low,
                y1=high,
                xref="x",
                yref="y",
                line={"width": 0},
                fillcolor=f"rgba(30, 144, 255, {volume_opacity})",
                layer="below",
            )

        if max_abs_delta > 0 and abs(delta) > 0:
            delta_frac = abs(delta) / max_abs_delta
            delta_width = allowed_delta_width * delta_frac
            delta_x0 = center_x - delta_width
            delta_x1 = center_x
            delta_opacity = 0.65 if in_va else 0.25
            delta_color = f"rgba(0, 190, 110, {delta_opacity})" if delta > 0 else f"rgba(230, 60, 60, {delta_opacity})"
            fig.add_shape(
                type="rect",
                x0=delta_x0,
                x1=delta_x1,
                y0=low,
                y1=high,
                xref="x",
                yref="y",
                line={"width": 0},
                fillcolor=delta_color,
                layer="below",
            )

    if val_low is not None and val_high is not None:
        fig.add_shape(type="line", x0=overlay_start, x1=session_end, y0=val_low, y1=val_low, xref="x", yref="y", line={"color": "rgba(69, 163, 255, 0.20)", "dash": "dash", "width": 1}, layer="above")
        fig.add_shape(type="line", x0=overlay_start, x1=session_end, y0=val_high, y1=val_high, xref="x", yref="y", line={"color": "rgba(69, 163, 255, 0.20)", "dash": "dash", "width": 1}, layer="above")

    if poc is not None:
        fig.add_shape(type="line", x0=overlay_start, x1=session_end, y0=poc, y1=poc, xref="x", yref="y", line={"color": "rgba(255, 59, 59, 0.90)", "dash": "solid", "width": 2}, layer="above")

    return {
        "bins_before_clamp": len(profile_bins),
        "bins_after_clamp": len(plotted_bins),
        "max_total_volume": float(max_total_volume),
        "max_abs_delta": float(max_abs_delta),
    }


def prepare_marker_sizes(bubbles_df: pd.DataFrame) -> pd.Series:
    if bubbles_df.empty:
        return pd.Series(dtype=float)
    if "bubble_size_score" in bubbles_df.columns:
        score_num = pd.to_numeric(bubbles_df["bubble_size_score"], errors="coerce")
        has_score = score_num.notna()
        sizes = pd.Series(index=bubbles_df.index, dtype=float)
        if has_score.any():
            sizes.loc[has_score] = (8 + 8 * score_num.loc[has_score]).clip(upper=40)
        if (~has_score).any():
            qty_num = pd.to_numeric(bubbles_df.loc[~has_score, "qty"], errors="coerce")
            max_qty = qty_num.max()
            sizes.loc[~has_score] = 8.0 if pd.isna(max_qty) or max_qty <= 0 else (8 + 18 * (qty_num / max_qty)).clip(upper=40)
        return sizes.fillna(8.0)
    qty_num = pd.to_numeric(bubbles_df["qty"], errors="coerce")
    max_qty = qty_num.max()
    if pd.isna(max_qty) or max_qty <= 0:
        return pd.Series(8.0, index=bubbles_df.index)
    return (8 + 18 * (qty_num / max_qty)).clip(upper=40).fillna(8.0)


def build_bubble_hover_text(bubbles_df: pd.DataFrame) -> list[str]:
    hover_texts: list[str] = []
    optional_fields = ["notional", "bubble_tier", "bubble_size_score", "agg_trade_id", "threshold_mode", "threshold_value"]
    for _, row in bubbles_df.iterrows():
        lines = [
            f"timestamp: {row['timestamp']}",
            f"aggressive_side: {row['aggressive_side']}",
            f"price: {row['price']}",
            f"qty: {row['qty']}",
        ]
        for field in optional_fields:
            if field in bubbles_df.columns:
                val = row[field]
                if pd.notna(val):
                    lines.append(f"{field}: {val}")
        hover_texts.append("<br>".join(lines))
    return hover_texts


def add_trade_box(fig: go.Figure, trade: SimulatedTrade, end_time: pd.Timestamp) -> None:
    x0 = trade.entry_timestamp
    x1 = end_time
    if trade.direction == "long":
        risk_y0, risk_y1 = trade.sl_price, trade.entry_price
        reward_y0, reward_y1 = trade.entry_price, trade.tp1_price
    else:
        risk_y0, risk_y1 = trade.entry_price, trade.sl_price
        reward_y0, reward_y1 = trade.tp1_price, trade.entry_price

    fig.add_shape(type="rect", x0=x0, x1=x1, y0=risk_y0, y1=risk_y1, xref="x", yref="y", fillcolor="rgba(139, 16, 24, 0.45)", line=dict(color="rgba(255, 60, 60, 0.8)", width=1), layer="below")
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=reward_y0, y1=reward_y1, xref="x", yref="y", fillcolor="rgba(47, 139, 56, 0.35)", line=dict(color="rgba(103, 211, 74, 0.8)", width=1, dash="dash"), layer="below")
    fig.add_shape(type="line", x0=x0, x1=x1, y0=trade.entry_price, y1=trade.entry_price, xref="x", yref="y", line=dict(color="rgba(255,255,255,0.55)", width=1, dash="dash"), layer="above")
    fig.add_shape(type="line", x0=x1, x1=x1, y0=min(risk_y0, reward_y0), y1=max(risk_y1, reward_y1), xref="x", yref="y", line=dict(color="rgba(220,220,220,0.65)", width=1, dash="dash"), layer="above")


def add_trade_overlays(fig: go.Figure, trades: list[SimulatedTrade], ohlcv_df: pd.DataFrame) -> None:
    for trade in trades:
        end_time = ohlcv_df["timestamp"].iloc[-1] if trade.result == "open" else trade.exit_timestamp
        marker_symbol = "triangle-up" if trade.direction == "long" else "triangle-down"
        entry_hover = (
            f"Trade {trade.trade_id}<br>"
            f"Direction: {trade.direction}<br>"
            f"Entry: {trade.entry_price:.2f}@{trade.entry_timestamp}<br>"
            f"SL: {trade.sl_price:.2f}<br>"
            f"TP1: {trade.tp1_price:.2f}<br>"
            f"Exit: {trade.exit_price:.2f}@{trade.exit_timestamp if trade.result != 'open' else 'N/A'}"
        )
        fig.add_trace(go.Scatter(x=[trade.entry_timestamp], y=[trade.entry_price], mode="markers", marker=dict(symbol=marker_symbol, size=14, color="#1976ff", line=dict(width=1, color="#ffffff")), name=f"Entry ({trade.direction})", hovertemplate=entry_hover, showlegend=False))
        add_trade_box(fig, trade, end_time)
        if trade.tp1_hit:
            fig.add_trace(go.Scatter(x=[trade.tp1_timestamp], y=[trade.tp1_price], mode="markers", marker=dict(symbol="x", size=12, color="green", line=dict(width=2, color="white")), showlegend=False))
        if trade.result != "open":
            color = "green" if trade.tp1_hit else "red"
            fig.add_trace(go.Scatter(x=[trade.exit_timestamp], y=[trade.exit_price], mode="markers", marker=dict(symbol="x", size=12, color=color, line=dict(width=2, color="white")), showlegend=False))
        fig.add_shape(type="line", x0=trade.entry_timestamp, x1=end_time, y0=trade.entry_price, y1=trade.entry_price, line=dict(color="rgba(100,100,255,0.5)", width=1, dash="dash"), layer="below")


def build_chart(
    ohlcv_df: pd.DataFrame,
    bubbles_df: pd.DataFrame,
    timeframe: str,
    previous_profile: dict[str, Any] | None = None,
    profile_overlay_start: pd.Timestamp | None = None,
    profile_overlay_end: pd.Timestamp | None = None,
    previous_session_start: pd.Timestamp | None = None,
    previous_session_end: pd.Timestamp | None = None,
    current_session_start: pd.Timestamp | None = None,
    current_session_end: pd.Timestamp | None = None,
    session_label: str | None = None,
    profile_clamp_low: float | None = None,
    profile_clamp_high: float | None = None,
    trades: list[SimulatedTrade] | None = None,
) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=ohlcv_df["timestamp"], open=ohlcv_df["open"], high=ohlcv_df["high"], low=ohlcv_df["low"], close=ohlcv_df["close"], name=f"{timeframe} OHLCV", increasing_line_color="#1fc7a5", increasing_fillcolor="#1fc7a5", decreasing_line_color="#e74c3c", decreasing_fillcolor="#e74c3c"))
    if not bubbles_df.empty:
        bubbles_df = bubbles_df.copy()
        bubbles_df["marker_size"] = prepare_marker_sizes(bubbles_df)
        bubbles_df["hover_text"] = build_bubble_hover_text(bubbles_df)
        buy_df = bubbles_df[bubbles_df["aggressive_side"] == "buy"]
        sell_df = bubbles_df[bubbles_df["aggressive_side"] == "sell"]
        if not buy_df.empty:
            fig.add_trace(go.Scatter(x=buy_df["timestamp"], y=buy_df["price"], mode="markers", name="Buy Bubbles", marker={"size": buy_df["marker_size"], "color": "#27d17f", "opacity": 0.45, "line": {"width": 0.5, "color": "#1f1f1f"}}, hovertext=buy_df["hover_text"], hoverinfo="text"))
        if not sell_df.empty:
            fig.add_trace(go.Scatter(x=sell_df["timestamp"], y=sell_df["price"], mode="markers", name="Sell Bubbles", marker={"size": sell_df["marker_size"], "color": "#f05454", "opacity": 0.45, "line": {"width": 0.5, "color": "#1f1f1f"}}, hovertext=sell_df["hover_text"], hoverinfo="text"))
    if previous_session_start is not None and previous_session_end is not None:
        fig.add_shape(type="rect", x0=previous_session_start, x1=previous_session_end, y0=0, y1=1, xref="x", yref="paper", fillcolor="rgba(18, 66, 120, 0.18)", line={"width": 0}, layer="below")
    if current_session_start is not None and current_session_end is not None:
        fig.add_shape(type="rect", x0=current_session_start, x1=current_session_end, y0=0, y1=1, xref="x", yref="paper", fillcolor="rgba(60, 60, 60, 0.10)", line={"width": 0}, layer="below")
    if previous_profile is not None and profile_overlay_start is not None and profile_overlay_end is not None and previous_session_end is not None:
        add_previous_session_volume_profile_overlay(fig=fig, profile=previous_profile, overlay_start=profile_overlay_start, overlay_end=profile_overlay_end, session_end=previous_session_end, clamp_low=profile_clamp_low, clamp_high=profile_clamp_high)
    if current_session_start is not None:
        current_session_start = current_session_start.tz_convert("UTC")
        fig.add_shape(type="line", x0=current_session_start, x1=current_session_start, y0=0, y1=1, xref="x", yref="paper", line={"color": "#7f7f7f", "width": 1, "dash": "dot"}, layer="above")
        fig.add_annotation(x=current_session_start, y=1, xref="x", yref="paper", text="Current Session Start", showarrow=False, xanchor="left", yanchor="top", font={"size": 10, "color": "#7f7f7f"})
    if previous_session_start is not None and previous_session_end is not None and current_session_start is not None and current_session_end is not None:
        prev_label = previous_session_start.strftime("%Y-%m-%d")
        curr_label = current_session_start.strftime("%Y-%m-%d")
        fig.add_annotation(x=previous_session_start + ((previous_session_end - previous_session_start) / 2), y=1.04, xref="x", yref="paper", text=f"<b>Previous Session ({prev_label})</b>", showarrow=False, font={"size": 14, "color": "#2ea3ff"})
        fig.add_annotation(x=current_session_start + ((current_session_end - current_session_start) / 2), y=1.04, xref="x", yref="paper", text=f"<b>Current Session ({curr_label})</b>", showarrow=False, font={"size": 14, "color": "#ffc642"})
    if trades:
        add_trade_overlays(fig, trades, ohlcv_df)
    fig.update_layout(title=None, autosize=True, height=900, xaxis_rangeslider_visible=False, hovermode="closest", xaxis_title="Timestamp (UTC)", yaxis_title="Price", paper_bgcolor="#050b10", plot_bgcolor="#0b1117", font={"color": "#d6dde6"}, showlegend=False, legend={"bgcolor": "rgba(0,0,0,0.25)", "bordercolor": "rgba(255,255,255,0.20)", "borderwidth": 1}, margin={"l": 70, "r": 70, "t": 70, "b": 50})
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.06)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.06)", tickformat=",.1f", side="right")
    return fig


def render_snapshot(context: SnapshotContext) -> go.Figure:
    combined_ohlcv_df = pd.concat([context.previous_session_candles, context.current_session_candles], ignore_index=True)
    return build_chart(
        combined_ohlcv_df,
        context.current_session_bubbles,
        timeframe=context.timeframe,
        previous_profile=context.previous_session_profile,
        profile_overlay_start=context.profile_overlay_start,
        profile_overlay_end=context.profile_overlay_end,
        previous_session_start=context.previous_session_start,
        previous_session_end=context.previous_session_end,
        current_session_start=context.current_session_start,
        current_session_end=context.current_session_end,
        session_label=context.session_date,
        profile_clamp_low=context.profile_clamp_low,
        profile_clamp_high=context.profile_clamp_high,
        trades=context.executed_trades,
    )
