from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import plotly.graph_objects as go

from analytics.performance_metrics import calculate_performance_metrics
from analytics.trade_log import trades_to_dataframe
from loader.trade_loader import load_trades, load_trades_window
from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.deep_trade import build_order_bubbles
from indicator.volume_profile import build_volume_profile
from utils.export import export_trade_report
from evaluator.interpreter import (
    evaluate_candle_against_previous_value,
    load_all_session_profiles,
)


def write_jsonl(rows: list[dict[str, Any]], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_utc_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tz is None:
        raise ValueError(f"Timestamp is timezone-naive (UTC offset required): {value}")
    return ts.tz_convert("UTC")

def parse_iso8601_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, format="ISO8601")

def parse_session_date(value: str) -> dt.date:
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            return dt.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid --session-date '{value}'. Expected YYYY-MM-DD.") from exc

    if len(value) == 8 and value.isdigit():
        try:
            return dt.datetime.strptime(value, "%d%m%Y").date()
        except ValueError as exc:
            raise ValueError(f"Invalid --session-date '{value}'. Expected DDMMYYYY.") from exc

    raise ValueError(f"Invalid --session-date '{value}'. Supported formats: YYYY-MM-DD or DDMMYYYY.")


def previous_session_date(session_date: dt.date) -> dt.date:
    return session_date - dt.timedelta(days=1)


def session_window_from_date(
    session_date: dt.date,
    session_start_hour: int,
    session_start_minute: int,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(
        dt.datetime(
            year=session_date.year,
            month=session_date.month,
            day=session_date.day,
            hour=session_start_hour,
            minute=session_start_minute,
            tzinfo=dt.timezone.utc,
        )
    )
    end = start + pd.Timedelta(days=1)
    return start, end


def load_session_profile_from_jsonl(profile_path: Path, session_id: str) -> dict[str, Any]:
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile JSONL file not found: {profile_path}")

    with profile_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if str(row.get("session_id")) == session_id:
                return row

    raise ValueError(f"No profile row found for session_id='{session_id}' in {profile_path}")


def compute_profile_overlay_window(
    profile_start: pd.Timestamp,
    profile_end: pd.Timestamp,
    width_ratio: float,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    if not (0 < width_ratio <= 1):
        raise ValueError(f"--profile-width-ratio must be in (0, 1], got {width_ratio}")
    duration = profile_end - profile_start
    overlay_end = profile_start + (duration * width_ratio)
    return profile_start, overlay_end


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
        return {
            "bins_before_clamp": 0,
            "bins_after_clamp": 0,
            "max_total_volume": 0.0,
            "max_abs_delta": 0.0,
        }

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

            if delta > 0:
                delta_opacity = 0.65 if in_va else 0.25
                delta_color = f"rgba(0, 190, 110, {delta_opacity})"
            else:
                delta_opacity = 0.65 if in_va else 0.25
                delta_color = f"rgba(230, 60, 60, {delta_opacity})"

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
        fig.add_shape(
            type="line",
            x0=overlay_start,
            x1=session_end,
            y0=val_low,
            y1=val_low,
            xref="x",
            yref="y",
            line={"color": "rgba(69, 163, 255, 0.20)", "dash": "dash", "width": 1},
            layer="above",
        )
        fig.add_shape(
            type="line",
            x0=overlay_start,
            x1=session_end,
            y0=val_high,
            y1=val_high,
            xref="x",
            yref="y",
            line={"color": "rgba(69, 163, 255, 0.20)", "dash": "dash", "width": 1},
            layer="above",
        )

    if poc is not None:
        fig.add_shape(
            type="line",
            x0=overlay_start,
            x1=session_end,
            y0=poc,
            y1=poc,
            xref="x",
            yref="y",
            line={"color": "rgba(255, 59, 59, 0.90)", "dash": "solid", "width": 2},
            layer="above",
        )

    return {
        "bins_before_clamp": len(profile_bins),
        "bins_after_clamp": len(plotted_bins),
        "max_total_volume": float(max_total_volume),
        "max_abs_delta": float(max_abs_delta),
    }

def resolve_snapshot_time_window(args: argparse.Namespace) -> tuple[pd.Timestamp, pd.Timestamp]:
    has_session_date = args.session_date is not None
    has_start = args.start is not None
    has_end = args.end is not None

    if has_session_date and (has_start or has_end):
        raise ValueError("Provide either --session-date OR both --start and --end, not both.")

    if has_session_date:
        d = parse_session_date(args.session_date)
        start, end = session_window_from_date(d, args.session_start_hour, args.session_start_minute)
        return start, end

    if has_start ^ has_end:
        raise ValueError("For manual window, provide both --start and --end.")

    if has_start and has_end:
        start = parse_utc_timestamp(args.start)
        end = parse_utc_timestamp(args.end)
        if end <= start:
            raise ValueError(f"Invalid time window: end ({end}) must be greater than start ({start}).")
        return start, end

    raise ValueError("snapshot mode requires --session-date OR both --start and --end.")


def filter_raw_trades_by_time_window(
    trades_df: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    start_ms = int(pd.Timestamp(start).tz_convert("UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end).tz_convert("UTC").timestamp() * 1000)
    return trades_df[(trades_df["timestamp"] >= start_ms) & (trades_df["timestamp"] < end_ms)].copy()


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
            if pd.isna(max_qty) or max_qty <= 0:
                sizes.loc[~has_score] = 8.0
            else:
                sizes.loc[~has_score] = (8 + 18 * (qty_num / max_qty)).clip(upper=40)

        return sizes.fillna(8.0)

    qty_num = pd.to_numeric(bubbles_df["qty"], errors="coerce")
    max_qty = qty_num.max()
    if pd.isna(max_qty) or max_qty <= 0:
        return pd.Series(8.0, index=bubbles_df.index)
    return (8 + 18 * (qty_num / max_qty)).clip(upper=40).fillna(8.0)


def build_bubble_hover_text(bubbles_df: pd.DataFrame) -> list[str]:
    hover_texts: list[str] = []
    optional_fields = [
        "notional",
        "bubble_tier",
        "bubble_size_score",
        "agg_trade_id",
        "threshold_mode",
        "threshold_value",
    ]

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


from executor.trend_following_model import SimulatedTrade

def add_trade_overlays(fig: go.Figure, trades: list[SimulatedTrade], ohlcv_df: pd.DataFrame) -> None:
    """Add visualization elements for executed trades."""
    for trade in trades:
        if trade.result == "open":
            end_time = ohlcv_df["timestamp"].iloc[-1]
        else:
            end_time = trade.exit_timestamp

        # Entry marker with hover info
        marker_symbol = 'triangle-up' if trade.direction == 'long' else 'triangle-down'
        entry_hover = (f"Trade {trade.trade_id}<br>"
                      f"Direction: {trade.direction}<br>"
                      f"Entry: {trade.entry_price:.2f}@{trade.entry_timestamp}<br>"
                      f"SL: {trade.sl_price:.2f}<br>"
                      f"TP1: {trade.tp1_price:.2f}<br>"
                      f"Exit: {trade.exit_price:.2f}@{trade.exit_timestamp if trade.result != 'open' else 'N/A'}")

        fig.add_trace(go.Scatter(
            x=[trade.entry_timestamp],
            y=[trade.entry_price],
            mode='markers',
            marker=dict(
                symbol=marker_symbol,
                size=14,
                color='#1976ff',
                line=dict(width=1, color='#ffffff')
            ),
            name=f"Entry ({trade.direction})",
            hovertemplate=entry_hover,
            showlegend=False
        ))
        
        # Add arrow base
        fig.add_shape(type="rect",
            x0=trade.entry_timestamp - pd.Timedelta(minutes=10),
            x1=trade.entry_timestamp + pd.Timedelta(minutes=10),
            y0=trade.entry_price - 0.1,
            y1=trade.entry_price + 0.1,
            fillcolor='#1976ff',
            line=dict(width=0),
            layer='below'
        )

        # Trade zones with SVG colors
        add_trade_box(fig, trade, end_time)
        
        # Square X markers
        if trade.tp1_hit:
            fig.add_trace(go.Scatter(
                x=[trade.tp1_timestamp],
                y=[trade.tp1_price],
                mode='markers',
                marker=dict(
                    symbol='x',
                    size=12,
                    color='green',
                    line=dict(width=2, color='white')
                ),
                showlegend=False
            ))
        
        if trade.result != "open":
            color = "green" if trade.tp1_hit else "red"
            fig.add_trace(go.Scatter(
                x=[trade.exit_timestamp],
                y=[trade.exit_price],
                mode='markers',
                marker=dict(
                    symbol='x',
                    size=12,
                    color=color,
                    line=dict(width=2, color='white')
                ),
                showlegend=False
            ))
            
            # Vertical dashed line at exit
            fig.add_shape(type="line",
                x0=trade.exit_timestamp,
                x1=trade.exit_timestamp,
                y0=ohlcv_df["low"].min(),
                y1=ohlcv_df["high"].max(),
                line=dict(color="gray", dash="dash", width=1),
                layer="below"
            )

        # Breakeven line (dashed)
        fig.add_shape(type="line",
            x0=trade.entry_timestamp,
            x1=end_time,
            y0=trade.entry_price,
            y1=trade.entry_price,
            line=dict(color="rgba(100,100,255,0.5)", width=1, dash="dash"),
            layer='below'
        )

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
    fig.add_trace(
        go.Candlestick(
            x=ohlcv_df["timestamp"],
            open=ohlcv_df["open"],
            high=ohlcv_df["high"],
            low=ohlcv_df["low"],
            close=ohlcv_df["close"],
            name=f"{timeframe} OHLCV",
            increasing_line_color="#1fc7a5",
            increasing_fillcolor="#1fc7a5",
            decreasing_line_color="#e74c3c",
            decreasing_fillcolor="#e74c3c",
        )
    )

    if not bubbles_df.empty:
        bubbles_df = bubbles_df.copy()
        bubbles_df["marker_size"] = prepare_marker_sizes(bubbles_df)
        bubbles_df["hover_text"] = build_bubble_hover_text(bubbles_df)
        buy_df = bubbles_df[bubbles_df["aggressive_side"] == "buy"]
        sell_df = bubbles_df[bubbles_df["aggressive_side"] == "sell"]

        if not buy_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=buy_df["timestamp"],
                    y=buy_df["price"],
                    mode="markers",
                    name="Buy Bubbles",
                    marker={"size": buy_df["marker_size"], "color": "#27d17f", "opacity": 0.45, "line": {"width": 0.5, "color": "#1f1f1f"}},
                    hovertext=buy_df["hover_text"],
                    hoverinfo="text",
                )
            )

        if not sell_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=sell_df["timestamp"],
                    y=sell_df["price"],
                    mode="markers",
                    name="Sell Bubbles",
                    marker={"size": sell_df["marker_size"], "color": "#f05454", "opacity": 0.45, "line": {"width": 0.5, "color": "#1f1f1f"}},
                    hovertext=sell_df["hover_text"],
                    hoverinfo="text",
                )
            )

    if previous_session_start is not None and previous_session_end is not None:
        fig.add_shape(
            type="rect",
            x0=previous_session_start,
            x1=previous_session_end,
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            fillcolor="rgba(18, 66, 120, 0.18)",
            line={"width": 0},
            layer="below",
        )

    if current_session_start is not None and current_session_end is not None:
        fig.add_shape(
            type="rect",
            x0=current_session_start,
            x1=current_session_end,
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            fillcolor="rgba(60, 60, 60, 0.10)",
            line={"width": 0},
            layer="below",
        )

    if previous_profile is not None and profile_overlay_start is not None and profile_overlay_end is not None and previous_session_end is not None:
        add_previous_session_volume_profile_overlay(
            fig=fig,
            profile=previous_profile,
            overlay_start=profile_overlay_start,
            overlay_end=profile_overlay_end,
            session_end=previous_session_end,
            clamp_low=profile_clamp_low,
            clamp_high=profile_clamp_high,
        )

    if current_session_start is not None:
        current_session_start = current_session_start.tz_convert("UTC")

        fig.add_shape(
            type="line",
            x0=current_session_start,
            x1=current_session_start,
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            line={
                "color": "#7f7f7f",
                "width": 1,
                "dash": "dot",
            },
            layer="above",
        )

        fig.add_annotation(
            x=current_session_start,
            y=1,
            xref="x",
            yref="paper",
            text="Current Session Start",
            showarrow=False,
            xanchor="left",
            yanchor="top",
            font={"size": 10, "color": "#7f7f7f"},
        )

    if previous_session_start is not None and previous_session_end is not None and current_session_start is not None and current_session_end is not None:
        prev_label = previous_session_start.strftime("%Y-%m-%d")
        curr_label = current_session_start.strftime("%Y-%m-%d")
        fig.add_annotation(
            x=previous_session_start + ((previous_session_end - previous_session_start) / 2),
            y=1.04,
            xref="x",
            yref="paper",
            text=f"<b>Previous Session ({prev_label})</b>",
            showarrow=False,
            font={"size": 14, "color": "#2ea3ff"},
        )
        fig.add_annotation(
            x=current_session_start + ((current_session_end - current_session_start) / 2),
            y=1.04,
            xref="x",
            yref="paper",
            text=f"<b>Current Session ({curr_label})</b>",
            showarrow=False,
            font={"size": 14, "color": "#ffc642"},
        )

    if trades:
        add_trade_overlays(fig, trades, ohlcv_df)

    fig.update_layout(
        title=None,
        autosize=True,
        height=900,
        xaxis_rangeslider_visible=False,
        hovermode="closest",
        xaxis_title="Timestamp (UTC)",
        yaxis_title="Price",
        paper_bgcolor="#050b10",
        plot_bgcolor="#0b1117",
        font={"color": "#d6dde6"},
        showlegend=False,
        legend={"bgcolor": "rgba(0,0,0,0.25)", "bordercolor": "rgba(255,255,255,0.20)", "borderwidth": 1},
        margin={"l": 70, "r": 70, "t": 70, "b": 50},
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.06)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.06)", tickformat=",.1f", side="right")
    return fig


def _default_title(symbol: str, session_label: str, start: pd.Timestamp, end: pd.Timestamp) -> str:
    start_utc = start.tz_convert("UTC")
    end_utc = end.tz_convert("UTC")
    start_wib = start_utc.tz_convert("Asia/Jakarta")
    end_wib = end_utc.tz_convert("Asia/Jakarta")
    return (
        f"{symbol} Replay Snapshot — Session {session_label}<br>"
        f"UTC Window: {start_utc.strftime('%Y-%m-%d %H:%M')} → {end_utc.strftime('%Y-%m-%d %H:%M')}<br>"
        f"Equivalent WIB: {start_wib.strftime('%Y-%m-%d %H:%M')} → {end_wib.strftime('%Y-%m-%d %H:%M')}"
    )


def _default_two_session_title(
    symbol: str,
    session_label: str,
    previous_start: pd.Timestamp,
    previous_end: pd.Timestamp,
    current_start: pd.Timestamp,
    current_end: pd.Timestamp,
) -> str:
    ps_utc = previous_start.tz_convert("UTC")
    pe_utc = previous_end.tz_convert("UTC")
    cs_utc = current_start.tz_convert("UTC")
    ce_utc = current_end.tz_convert("UTC")
    return (
        f"{symbol} Replay Snapshot — Session {session_label}<br>"
        f"Prev UTC: {ps_utc.strftime('%Y-%m-%d %H:%M')} → {pe_utc.strftime('%Y-%m-%d %H:%M')}<br>"
        f"Curr UTC: {cs_utc.strftime('%Y-%m-%d %H:%M')} → {ce_utc.strftime('%Y-%m-%d %H:%M')}"
    )


def _validate_common(args: argparse.Namespace) -> None:
    if args.mode not in {"export-ohlcv", "export-bubbles", "snapshot"}:
        raise ValueError("--mode must be one of: export-ohlcv, export-bubbles, snapshot")
    if not (0 <= args.session_start_hour <= 23):
        raise ValueError("--session-start-hour must be in [0, 23]")
    if not (0 <= args.session_start_minute <= 59):
        raise ValueError("--session-start-minute must be in [0, 59]")
    if not (0 < args.profile_width_ratio <= 1):
        raise ValueError("--profile-width-ratio must be in (0, 1]")


def _print_export_ohlcv_summary(
    input_file: Path,
    output_file: Path,
    raw_trade_count: int,
    candles: list[dict[str, Any]],
) -> None:
    print("mode: export-ohlcv")
    print(f"input file: {input_file}")
    print(f"output file: {output_file}")
    print(f"raw trade count: {raw_trade_count}")
    print(f"candle count: {len(candles)}")
    print(f"first candle timestamp: {candles[0]['timestamp'] if candles else None}")
    print(f"last candle timestamp: {candles[-1]['timestamp'] if candles else None}")
    print(f"total volume: {sum(float(c['volume']) for c in candles)}")
    print(f"total buy volume: {sum(float(c['buy_volume']) for c in candles)}")
    print(f"total sell volume: {sum(float(c['sell_volume']) for c in candles)}")
    print(f"total delta: {sum(float(c['delta']) for c in candles)}")


def _print_export_bubbles_summary(
    input_file: Path,
    output_file: Path,
    raw_trade_count: int,
    bubbles: list[dict[str, Any]],
    min_qty: float | None,
    min_notional: float | None,
) -> None:
    buy_count = sum(1 for b in bubbles if b["aggressive_side"] == "buy")
    sell_count = sum(1 for b in bubbles if b["aggressive_side"] == "sell")

    print("mode: export-bubbles")
    print(f"input file: {input_file}")
    print(f"output file: {output_file}")
    print(f"raw trade count: {raw_trade_count}")
    print(f"bubble count: {len(bubbles)}")
    print(f"buy bubble count: {buy_count}")
    print(f"sell bubble count: {sell_count}")
    print(f"first bubble timestamp: {bubbles[0]['timestamp'] if bubbles else None}")
    print(f"last bubble timestamp: {bubbles[-1]['timestamp'] if bubbles else None}")
    print(f"total bubble qty: {sum(float(b['qty']) for b in bubbles)}")
    print(f"total bubble notional: {sum(float(b['notional']) for b in bubbles)}")
    print(f"threshold settings: min_qty={min_qty}, min_notional={min_notional}")

def add_trade_box(fig, trade, end_time):
    x0 = trade.entry_timestamp
    x1 = end_time

    if trade.direction == "long":
        risk_y0 = trade.sl_price
        risk_y1 = trade.entry_price
        reward_y0 = trade.entry_price
        reward_y1 = trade.tp1_price
    else:
        risk_y0 = trade.entry_price
        risk_y1 = trade.sl_price
        reward_y0 = trade.tp1_price
        reward_y1 = trade.entry_price

    # Risk box
    fig.add_shape(
        type="rect",
        x0=x0,
        x1=x1,
        y0=risk_y0,
        y1=risk_y1,
        xref="x",
        yref="y",
        fillcolor="rgba(139, 16, 24, 0.45)",
        line=dict(color="rgba(255, 60, 60, 0.8)", width=1),
        layer="below",
    )

    # Reward box
    fig.add_shape(
        type="rect",
        x0=x0,
        x1=x1,
        y0=reward_y0,
        y1=reward_y1,
        xref="x",
        yref="y",
        fillcolor="rgba(47, 139, 56, 0.35)",
        line=dict(color="rgba(103, 211, 74, 0.8)", width=1, dash="dash"),
        layer="below",
    )

    # Entry line
    fig.add_shape(
        type="line",
        x0=x0,
        x1=x1,
        y0=trade.entry_price,
        y1=trade.entry_price,
        xref="x",
        yref="y",
        line=dict(color="rgba(255,255,255,0.55)", width=1, dash="dash"),
        layer="above",
    )

    # Exit vertical line
    fig.add_shape(
        type="line",
        x0=x1,
        x1=x1,
        y0=min(risk_y0, reward_y0),
        y1=max(risk_y1, reward_y1),
        xref="x",
        yref="y",
        line=dict(color="rgba(220,220,220,0.65)", width=1, dash="dash"),
        layer="above",
    )

def main() -> None:
    trades = []
    parser = argparse.ArgumentParser(description="Replay orchestrator: export OHLCV, export bubbles, or render snapshot HTML.")
    parser.add_argument("--mode", required=True, choices=["export-ohlcv", "export-bubbles", "snapshot"])
    parser.add_argument("--input", required=True, help="Input aggTrades file (.jsonl or .parquet)")
    parser.add_argument("--output", required=True, help="Output file path (JSONL for export modes, HTML for snapshot)")
    parser.add_argument("--symbol", required=True, help="Symbol label (e.g., BTCUSDT)")
    parser.add_argument("--session-date", help="Session date (YYYY-MM-DD or DDMMYYYY)")
    parser.add_argument("--start", help="Manual UTC start timestamp (timezone-aware)")
    parser.add_argument("--end", help="Manual UTC end timestamp (timezone-aware)")
    parser.add_argument("--session-start-hour", type=int, default=13)
    parser.add_argument("--session-start-minute", type=int, default=30)
    parser.add_argument("--min-qty", type=float, default=None)
    parser.add_argument("--min-notional", type=float, default=None)
    parser.add_argument("--profile-input", help="Session profile JSONL input (used in snapshot mode with --session-date)")
    parser.add_argument("--profile-width-ratio", type=float, default=0.30, help="Overlay width ratio over previous session window (0,1]")
    parser.add_argument(
        "--timeframe",
        default="1m",
        choices=["1m", "5m", "15m"],
        help="OHLCV candle timeframe for snapshot/export chart. Default: 1m.",
    )
    parser.add_argument("--trade-report-output", help="Output .xlsx path for trade performance report")
    parser.add_argument("--title", help="Chart title override (snapshot mode)")
    args = parser.parse_args()

    _validate_common(args)

    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.mode in {"export-bubbles", "snapshot"} and args.min_qty is None and args.min_notional is None:
        raise ValueError("At least one threshold must be provided: --min-qty and/or --min-notional")

    if args.mode == "export-ohlcv":
        trades = load_trades(str(input_path))
        if args.start is not None or args.end is not None:
            if args.start is None or args.end is None:
                raise ValueError("For manual window, provide both --start and --end.")
            start = parse_utc_timestamp(args.start)
            end = parse_utc_timestamp(args.end)
            if end <= start:
                raise ValueError(f"Invalid time window: end ({end}) must be greater than start ({start}).")
            trades = filter_raw_trades_by_time_window(trades, start, end)

        candles = aggregate_trades_to_ohlcv(trades_df=trades, symbol=args.symbol, timeframe=args.timeframe)
        write_jsonl(candles, str(output_path))
        _print_export_ohlcv_summary(input_path, output_path, len(trades), candles)
        print(f"timeframe: {args.timeframe}")
        return

    if args.mode == "export-bubbles":
        trades = load_trades(str(input_path))
        if args.start is not None or args.end is not None:
            if args.start is None or args.end is None:
                raise ValueError("For manual window, provide both --start and --end.")
            start = parse_utc_timestamp(args.start)
            end = parse_utc_timestamp(args.end)
            if end <= start:
                raise ValueError(f"Invalid time window: end ({end}) must be greater than start ({start}).")
            trades = filter_raw_trades_by_time_window(trades, start, end)

        bubbles = build_order_bubbles(
            trades_df=trades,
            symbol=args.symbol,
            min_qty=float(args.min_qty) if args.min_qty is not None else None,
            min_notional=float(args.min_notional) if args.min_notional is not None else None,
        )
        write_jsonl(bubbles, str(output_path))
        _print_export_bubbles_summary(
            input_path,
            output_path,
            len(trades),
            bubbles,
            float(args.min_qty) if args.min_qty is not None else None,
            float(args.min_notional) if args.min_notional is not None else None,
        )
        return

    previous_start: pd.Timestamp | None = None
    previous_end: pd.Timestamp | None = None
    current_start: pd.Timestamp | None = None
    current_end: pd.Timestamp | None = None
    previous_profile: dict[str, Any] | None = None
    profile_overlay_start: pd.Timestamp | None = None
    profile_overlay_end: pd.Timestamp | None = None
    profile_clamp_low: float | None = None
    profile_clamp_high: float | None = None
    profile_render_stats: dict[str, Any] | None = None

    if args.session_date is not None:
        session_date = parse_session_date(args.session_date)
        prev_date = previous_session_date(session_date)

        previous_start, previous_end = session_window_from_date(
            prev_date, args.session_start_hour, args.session_start_minute
        )
        current_start, current_end = session_window_from_date(
            session_date, args.session_start_hour, args.session_start_minute
        )

        start = previous_start
        end = current_end

        chart_window_trades = load_trades_window(str(input_path), start=start, end=end)
        current_session_trades = filter_raw_trades_by_time_window(chart_window_trades, current_start, current_end)
        previous_session_trades = filter_raw_trades_by_time_window(chart_window_trades, previous_start, previous_end)

        if previous_session_trades.empty:
            raise ValueError(
                f"No trades in previous-session window [start={previous_start.isoformat()}, end={previous_end.isoformat()})."
            )

        previous_profile = build_volume_profile(previous_session_trades)
        previous_profile = {"session_id": prev_date.isoformat(), **previous_profile}

        if args.profile_input is not None:
            profile_path = Path(args.profile_input)
            previous_profile = load_session_profile_from_jsonl(profile_path, prev_date.isoformat())
            profile_overlay_start, profile_overlay_end = compute_profile_overlay_window(
                previous_start,
                previous_end,
                float(args.profile_width_ratio),
            )
    else:
        start, end = resolve_snapshot_time_window(args)
        chart_window_trades = load_trades_window(str(input_path), start=start, end=end)
        current_session_trades = chart_window_trades

    if chart_window_trades.empty:
        raise ValueError(f"No trades in selected chart window [start={start.isoformat()}, end={end.isoformat()}).")

    if current_session_trades.empty:
        raise ValueError(
            f"No trades in current bubble window [start={current_start.isoformat() if current_start is not None else start.isoformat()}, "
            f"end={current_end.isoformat() if current_end is not None else end.isoformat()})."
        )

    candles = aggregate_trades_to_ohlcv(trades_df=chart_window_trades, symbol=args.symbol, timeframe=args.timeframe)
    if not candles:
        raise ValueError("No candles generated for selected window.")

    bubbles = build_order_bubbles(
        trades_df=current_session_trades,
        symbol=args.symbol,
        min_qty=float(args.min_qty) if args.min_qty is not None else None,
        min_notional=float(args.min_notional) if args.min_notional is not None else None,
    )

    ohlcv_df = pd.DataFrame(candles)
    bubbles_df = pd.DataFrame(bubbles)
    ohlcv_df["timestamp"] = parse_iso8601_series(ohlcv_df["timestamp"])
    if not bubbles_df.empty:
        bubbles_df["timestamp"] = parse_iso8601_series(bubbles_df["timestamp"])
        bubbles_df["aggressive_side"] = bubbles_df["aggressive_side"].astype(str).str.lower()

    ohlcv_df = ohlcv_df.sort_values("timestamp", ascending=True).reset_index(drop=True)
    if not bubbles_df.empty:
        bubbles_df = bubbles_df.sort_values("timestamp", ascending=True).reset_index(drop=True)

    if bubbles_df.empty:
        print("Warning: No bubbles in selected window. Generating candlestick-only chart.")

    # --- Balance / Imbalance evaluator (v1) ---
    # Live-safe constraints:
    # - Use only previous completed session profile
    # - Use current completed candle
    # - No future candles / no future session data
    ohlcv_df["location"] = "unknown"
    ohlcv_df["balance_state"] = "unknown"

    evaluator_profiles: dict[str, dict[str, Any]] | None = None
    if args.profile_input is not None:
        evaluator_profiles = load_all_session_profiles(Path(args.profile_input))

    if evaluator_profiles is not None:
        for idx, row in ohlcv_df.iterrows():
            candle_ts = pd.Timestamp(row["timestamp"])

            # In session-date mode, evaluate only current session candles.
            if current_start is not None and current_end is not None:
                if not (current_start <= candle_ts < current_end):
                    continue

            candle_payload = {
                "timestamp": candle_ts.isoformat(),
                "close": float(row["close"]),
            }

            try:
                evaluation = evaluate_candle_against_previous_value(
                    candle=candle_payload,
                    profile_by_session_id=evaluator_profiles,
                    session_start_hour=int(args.session_start_hour),
                    session_start_minute=int(args.session_start_minute),
                )
                ohlcv_df.at[idx, "location"] = str(evaluation["location"])
                ohlcv_df.at[idx, "balance_state"] = str(evaluation["balance_state"])
            except ValueError:
                # Keep deterministic fallback when profile for the required
                # previous session is unavailable.
                ohlcv_df.at[idx, "location"] = "unknown"
                ohlcv_df.at[idx, "balance_state"] = "unknown"

    if previous_start is not None and previous_end is not None and previous_profile is not None:
        prev_ohlcv_df = ohlcv_df[
            (ohlcv_df["timestamp"] >= previous_start)
            & (ohlcv_df["timestamp"] < previous_end)
        ]

        if prev_ohlcv_df.empty:
            print("Warning: previous-session OHLCV dataframe is empty; skipping profile clamping.")
            profile_clamp_low = None
            profile_clamp_high = None
        else:
            profile_clamp_low = float(prev_ohlcv_df["low"].min())
            profile_clamp_high = float(prev_ohlcv_df["high"].max())

        profile_render_stats = add_previous_session_volume_profile_overlay(
            fig=go.Figure(),
            profile=previous_profile,
            overlay_start=profile_overlay_start if profile_overlay_start is not None else previous_start,
            overlay_end=profile_overlay_end if profile_overlay_end is not None else previous_start,
            session_end=previous_end,
            clamp_low=profile_clamp_low,
            clamp_high=profile_clamp_high,
        )

    session_label = args.session_date if args.session_date else "Custom"

    # Generate simulated trades based on execution strategy
    trades = []
    if evaluator_profiles is not None:
        from executor.trend_following_model import simulate_trend_following
        trades = simulate_trend_following(
            candles_df=ohlcv_df,
            bubbles_df=bubbles_df,
            interpreter_df=ohlcv_df[["location", "balance_state"]],
            tick_size=0.01  # BTCUSDT tick size
        )

    fig = build_chart(
        ohlcv_df,
        bubbles_df,
        timeframe=args.timeframe,
        previous_profile=previous_profile,
        profile_overlay_start=profile_overlay_start,
        profile_overlay_end=profile_overlay_end,
        previous_session_start=previous_start,
        previous_session_end=previous_end,
        current_session_start=current_start,
        current_session_end=current_end,
        session_label=session_label,
        profile_clamp_low=profile_clamp_low,
        profile_clamp_high=profile_clamp_high,
        trades=trades,
    )
    fig.write_html(output_path, include_plotlyjs="cdn", full_html=True)

    buy_count = int((bubbles_df["aggressive_side"] == "buy").sum()) if not bubbles_df.empty else 0
    sell_count = int((bubbles_df["aggressive_side"] == "sell").sum()) if not bubbles_df.empty else 0

    # Trade analytics summary
    trades_df = trades_to_dataframe(trades)
    metrics = calculate_performance_metrics(trades_df)

    print("\nTrade Summary:")
    print(f"Total trades: {len(trades_df)}")
    print(f"Closed trades: {int(trades_df['is_closed'].sum()) if not trades_df.empty else 0}")
    print(f"Open trades: {int((~trades_df['is_closed']).sum()) if not trades_df.empty else 0}")
    print(f"Win rate: {metrics['win_rate']:.2%}")
    print(f"TP1 hit rate: {metrics['tp1_hit_rate']:.2%}")
    print(f"Total R: {metrics['total_r']:.4f}")
    print(f"Avg R: {metrics['avg_r']:.4f}")
    print(f"Median R: {metrics['median_r']:.4f}")
    print(f"Max drawdown (R): {metrics['max_drawdown_r']:.4f}")
    print(f"Expectancy (R): {metrics['expectancy_r']:.4f}")
    print(f"Longest losing streak: {metrics['longest_losing_streak']}")
    if metrics["profit_factor"] is None:
        print("Profit factor: None")
    elif metrics["profit_factor"] == float("inf"):
        print("Profit factor: inf")
    else:
        print(f"Profit factor: {metrics['profit_factor']:.4f}")

    if args.trade_report_output:
        export_trade_report(trades_df=trades_df, metrics=metrics, output_path=args.trade_report_output)
        print(f"trade report output: {args.trade_report_output}")

    print("\nSnapshot Details:")
    print(f"timeframe: {args.timeframe}")
    print(f"input file: {input_path}")
    print(f"output HTML: {output_path}")
    print(f"chart UTC start: {start.isoformat()}")
    print(f"chart UTC end: {end.isoformat()}")
    if current_start is not None and current_end is not None:
        print(f"current-session UTC start: {current_start.isoformat()}")
        print(f"current-session UTC end: {current_end.isoformat()}")
    if previous_start is not None and previous_end is not None:
        print(f"previous-session UTC start: {previous_start.isoformat()}")
        print(f"previous-session UTC end: {previous_end.isoformat()}")
    print(f"raw trades in chart window: {len(chart_window_trades)}")
    print(f"raw trades in bubble window: {len(current_session_trades)}")
    print(f"candle count: {len(ohlcv_df)}")
    if "balance_state" in ohlcv_df.columns and "location" in ohlcv_df.columns:
        balance_counts = ohlcv_df["balance_state"].value_counts(dropna=False).to_dict()
        location_counts = ohlcv_df["location"].value_counts(dropna=False).to_dict()
        print(f"balance_state counts: {balance_counts}")
        print(f"location counts: {location_counts}")
    print(f"bubble count: {len(bubbles_df)}")
    print(f"buy bubble count: {buy_count}")
    print(f"sell bubble count: {sell_count}")
    print(f"first candle timestamp: {ohlcv_df['timestamp'].iloc[0].isoformat()}")
    print(f"last candle timestamp: {ohlcv_df['timestamp'].iloc[-1].isoformat()}")
    if not bubbles_df.empty:
        print(f"first bubble timestamp: {bubbles_df['timestamp'].iloc[0].isoformat()}")
        print(f"last bubble timestamp: {bubbles_df['timestamp'].iloc[-1].isoformat()}")
    else:
        print("first bubble timestamp: N/A")
        print("last bubble timestamp: N/A")
    if args.profile_input is not None:
        print(f"selected timeframe: {args.timeframe}")
        print(f"profile input file: {args.profile_input}")
        print(f"profile width ratio: {args.profile_width_ratio}")
        print(f"previous profile loaded: {previous_profile is not None}")
        print(f"previous profile session_id: {previous_profile.get('session_id') if previous_profile else None}")
        if previous_profile is not None:
            print(f"profile session_low/session_high: {previous_profile.get('session_low')} / {previous_profile.get('session_high')}")
            print(f"profile val/vah/poc_price: {previous_profile.get('val')} / {previous_profile.get('vah')} / {previous_profile.get('poc_price')}")
        print(f"previous OHLCV low/high: {profile_clamp_low} / {profile_clamp_high}")
        print(f"clamp_low/clamp_high: {profile_clamp_low} / {profile_clamp_high}")

        if previous_profile is not None:
            preview_stats = add_previous_session_volume_profile_overlay(
                fig=go.Figure(),
                profile=previous_profile,
                overlay_start=profile_overlay_start if profile_overlay_start is not None else previous_start,
                overlay_end=profile_overlay_end if profile_overlay_end is not None else previous_start,
                session_end=previous_end if previous_end is not None else end,
                clamp_low=profile_clamp_low,
                clamp_high=profile_clamp_high,
            )
            print(f"number of profile bins before clamp: {preview_stats.get('bins_before_clamp')}")
            print(f"number of profile bins after clamp: {preview_stats.get('bins_after_clamp')}")
            print(f"max_total_volume: {preview_stats.get('max_total_volume')}")
            print(f"max_abs_delta: {preview_stats.get('max_abs_delta')}")


if __name__ == "__main__":
    main()
