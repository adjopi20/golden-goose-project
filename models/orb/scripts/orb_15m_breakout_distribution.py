from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.volume_profile import build_basic_volume_profile


NY = ZoneInfo("America/New_York")


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _dt(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=NY)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(NY).isoformat()


def _slice_idx(ts: np.ndarray, start_ms: int, end_ms: int) -> slice:
    return slice(int(np.searchsorted(ts, start_ms, side="left")), int(np.searchsorted(ts, end_ms, side="left")))


def _side(is_buyer_maker: bool) -> str:
    return "sell" if bool(is_buyer_maker) else "buy"


def _profile(df: pd.DataFrame, bins: int) -> dict[str, float] | None:
    if len(df) < 2:
        return None
    basic = build_basic_volume_profile(df[["price", "qty"]], n_bins=bins)
    buy_volume = float(df.loc[~df["is_buyer_maker"].astype(bool), "qty"].sum())
    sell_volume = float(df.loc[df["is_buyer_maker"].astype(bool), "qty"].sum())
    return {
        "profile_low": float(df["price"].min()),
        "profile_high": float(df["price"].max()),
        "poc_price": float(basic["poc_price"]),
        "val": float(basic["val"]),
        "vah": float(basic["vah"]),
        "profile_width": float(df["price"].max() - df["price"].min()),
        "total_volume": float(df["qty"].sum()),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "delta": buy_volume - sell_volume,
        "value_area_width": float(basic["value_area_width"]),
    }


def _window_features(df: pd.DataFrame, prefix: str) -> dict[str, float]:
    if df.empty:
        return {f"{prefix}_{k}": math.nan for k in ("net_delta", "volume", "buy_volume_pct", "range")}
    buy = float(df.loc[~df["is_buyer_maker"].astype(bool), "qty"].sum())
    sell = float(df.loc[df["is_buyer_maker"].astype(bool), "qty"].sum())
    volume = buy + sell
    return {
        f"{prefix}_net_delta": buy - sell,
        f"{prefix}_volume": volume,
        f"{prefix}_buy_volume_pct": buy / volume if volume > 0 else math.nan,
        f"{prefix}_range": float(df["price"].max() - df["price"].min()),
    }


def _bubble_features(df: pd.DataFrame, p95_qty: float, breakout_level: float, prefix: str) -> dict[str, Any]:
    bubbles = df.loc[df["qty"] >= p95_qty].copy()
    out: dict[str, Any] = {
        f"{prefix}_p95_bubble_count": int(len(bubbles)),
        f"{prefix}_buy_bubble_count": int((~bubbles["is_buyer_maker"].astype(bool)).sum()) if not bubbles.empty else 0,
        f"{prefix}_sell_bubble_count": int((bubbles["is_buyer_maker"].astype(bool)).sum()) if not bubbles.empty else 0,
        f"{prefix}_largest_bubble_qty": math.nan,
        f"{prefix}_largest_bubble_side": None,
        f"{prefix}_largest_bubble_distance_to_breakout_level": math.nan,
    }
    if bubbles.empty:
        return out
    row = bubbles.loc[bubbles["qty"].idxmax()]
    out[f"{prefix}_largest_bubble_qty"] = float(row["qty"])
    out[f"{prefix}_largest_bubble_side"] = _side(bool(row["is_buyer_maker"]))
    out[f"{prefix}_largest_bubble_distance_to_breakout_level"] = float(row["price"] - breakout_level)
    return out


def _context_distances(prefix: str, price: float, data: dict[str, float] | None) -> dict[str, float]:
    if not data:
        return {f"distance_to_{prefix}_{k}": math.nan for k in ("high", "low")}
    return {
        f"distance_to_{prefix}_high": float(price - data["high"]),
        f"distance_to_{prefix}_low": float(price - data["low"]),
    }


def _extreme(df: pd.DataFrame) -> dict[str, float] | None:
    if df.empty:
        return None
    return {"high": float(df["price"].max()), "low": float(df["price"].min())}


def _make_sample(
    *,
    day: date,
    direction: str,
    candle: dict[str, Any],
    trade_df: pd.DataFrame,
    prior24_df: pd.DataFrame,
    setup_from_orb_df: pd.DataFrame,
    breakout_candle_df: pd.DataFrame,
    orb: dict[str, float],
    prior24_profile: dict[str, float] | None,
    p95_qty: float,
    pre_ny: dict[str, float] | None,
    overnight: dict[str, float] | None,
    previous_ny: dict[str, float] | None,
    orb_end_ms: int,
    horizon_end_ms: int,
) -> dict[str, Any]:
    breakout_ms = int(candle["timestamp_ms"])
    breakout_level = orb["profile_high"] if direction == "long" else orb["profile_low"]
    invalidation_level = orb["profile_low"] if direction == "long" else orb["profile_high"]
    horizon = trade_df.loc[(trade_df["timestamp"] > breakout_ms) & (trade_df["timestamp"] <= horizon_end_ms)]
    if direction == "long":
        invalidation_hits = horizon.loc[horizon["price"] <= invalidation_level]
        same_side_reclaim_hits = horizon.loc[horizon["price"] <= breakout_level]
        end_ms = int(invalidation_hits["timestamp"].iloc[0]) if not invalidation_hits.empty else horizon_end_ms
        outcome_df = horizon.loc[horizon["timestamp"] <= end_ms]
        max_row = outcome_df.loc[outcome_df["price"].idxmax()] if not outcome_df.empty else None
        max_price = float(max_row["price"]) if max_row is not None else float(candle["close"])
        max_ms = int(max_row["timestamp"]) if max_row is not None else breakout_ms
        expansion_abs = max_price - breakout_level
    else:
        invalidation_hits = horizon.loc[horizon["price"] >= invalidation_level]
        same_side_reclaim_hits = horizon.loc[horizon["price"] >= breakout_level]
        end_ms = int(invalidation_hits["timestamp"].iloc[0]) if not invalidation_hits.empty else horizon_end_ms
        outcome_df = horizon.loc[horizon["timestamp"] <= end_ms]
        max_row = outcome_df.loc[outcome_df["price"].idxmin()] if not outcome_df.empty else None
        max_price = float(max_row["price"]) if max_row is not None else float(candle["close"])
        max_ms = int(max_row["timestamp"]) if max_row is not None else breakout_ms
        expansion_abs = breakout_level - max_price

    orb_width = float(orb["profile_width"])
    pre5 = setup_from_orb_df.loc[setup_from_orb_df["timestamp"] >= breakout_ms - 5 * 60_000]
    pre15 = setup_from_orb_df.loc[setup_from_orb_df["timestamp"] >= breakout_ms - 15 * 60_000]
    volume = float(candle["volume"])
    buy_volume = float(candle["buy_volume"])
    price = float(candle["close"])
    row: dict[str, Any] = {
        "session_day": day.isoformat(),
        "direction": direction,
        "breakout_time": _iso(breakout_ms),
        "breakout_timestamp_ms": breakout_ms,
        "breakout_level": breakout_level,
        "invalidation_level": invalidation_level,
        "breakout_open": float(candle["open"]),
        "breakout_high": float(candle["high"]),
        "breakout_low": float(candle["low"]),
        "breakout_close": price,
        "breakout_delta": float(candle["delta"]),
        "breakout_volume": volume,
        "breakout_buy_volume_pct": buy_volume / volume if volume > 0 else math.nan,
        "breakout_body_to_range": float(candle["body"]) / float(candle["range"]) if float(candle["range"]) > 0 else math.nan,
        "breakout_close_position_in_range": (price - float(candle["low"])) / float(candle["range"]) if float(candle["range"]) > 0 else math.nan,
        "breakout_trade_count": int(candle["trade_count"]),
        "breakout_largest_trade_qty": float(candle["largest_trade_qty"]),
        "breakout_largest_trade_side": candle["largest_trade_side"],
        "end_time": _iso(end_ms),
        "end_timestamp_ms": end_ms,
        "end_reason": "opposite_orb_invalidation" if not invalidation_hits.empty else "time_invalidation_0430",
        "max_expansion_time": _iso(max_ms),
        "max_expansion_timestamp_ms": max_ms,
        "max_expansion_price": max_price,
        "max_expansion_abs": expansion_abs,
        "max_expansion_pct": expansion_abs / breakout_level if breakout_level else math.nan,
        "max_expansion_orb_width_multiple": expansion_abs / orb_width if orb_width > 0 else math.nan,
        "time_to_max_expansion_seconds": (max_ms - breakout_ms) / 1000.0,
        "same_side_reclaim_time": _iso(int(same_side_reclaim_hits["timestamp"].iloc[0])) if not same_side_reclaim_hits.empty else None,
        "time_to_same_side_reclaim_seconds": (
            (int(same_side_reclaim_hits["timestamp"].iloc[0]) - breakout_ms) / 1000.0
            if not same_side_reclaim_hits.empty
            else math.nan
        ),
        "time_to_opposite_orb_invalidation_seconds": (
            (end_ms - breakout_ms) / 1000.0 if not invalidation_hits.empty else math.nan
        ),
        "p95_qty_threshold": p95_qty,
        "orb_profile_low": orb["profile_low"],
        "orb_profile_high": orb["profile_high"],
        "orb_poc_price": orb["poc_price"],
        "orb_val": orb["val"],
        "orb_vah": orb["vah"],
        "orb_profile_width": orb_width,
        "orb_total_volume": orb["total_volume"],
        "orb_delta": orb["delta"],
        "orb_buy_volume": orb["buy_volume"],
        "orb_sell_volume": orb["sell_volume"],
        "orb_poc_position_inside_profile": (orb["poc_price"] - orb["profile_low"]) / orb_width if orb_width > 0 else math.nan,
        "orb_value_area_width": orb["value_area_width"],
        "orb_breakout_level_distance_from_poc": breakout_level - orb["poc_price"],
        "time_from_orb_end_to_breakout_seconds": (breakout_ms - orb_end_ms) / 1000.0,
    }
    row.update(_window_features(setup_from_orb_df, "from_orb_end_to_breakout"))
    row.update(_window_features(pre5, "last_5m"))
    row.update(_window_features(pre15, "last_15m"))
    row.update(_bubble_features(setup_from_orb_df, p95_qty, breakout_level, "pre_breakout"))
    row.update(_bubble_features(breakout_candle_df, p95_qty, breakout_level, "breakout_candle"))
    if prior24_profile:
        for key in ("poc_price", "val", "vah"):
            row[f"distance_to_previous_24h_{key}"] = price - prior24_profile[key]
        row["breakout_against_previous_24h_poc"] = bool(price > prior24_profile["poc_price"]) if direction == "long" else bool(price < prior24_profile["poc_price"])
    else:
        for key in ("poc_price", "val", "vah"):
            row[f"distance_to_previous_24h_{key}"] = math.nan
        row["breakout_against_previous_24h_poc"] = None
    row.update(_context_distances("pre_ny", price, pre_ny))
    row.update(_context_distances("overnight", price, overnight))
    row.update(_context_distances("previous_ny", price, previous_ny))
    return row


def run(input_path: Path, output_dir: Path, bins: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(input_path, columns=["timestamp", "price", "qty", "is_buyer_maker"])
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    ts = df["timestamp"].to_numpy(dtype=np.int64)

    min_day = datetime.fromtimestamp(int(ts.min()) / 1000.0, tz=timezone.utc).astimezone(NY).date()
    max_day = datetime.fromtimestamp(int(ts.max()) / 1000.0, tz=timezone.utc).astimezone(NY).date()
    samples: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    for offset in range((max_day - min_day).days + 1):
        day = min_day + timedelta(days=offset)
        prior_start = _ms(_dt(day - timedelta(days=1), time(9, 30)))
        ny_open = _ms(_dt(day, time(9, 30)))
        orb_end = _ms(_dt(day, time(9, 45)))
        setup_end = _ms(_dt(day, time(10, 15)))
        horizon_end = _ms(_dt(day + timedelta(days=1), time(4, 30)))

        if prior_start < int(ts.min()) or horizon_end > int(ts.max()):
            skipped["edge_coverage"] = skipped.get("edge_coverage", 0) + 1
            continue

        prior24 = df.iloc[_slice_idx(ts, prior_start, ny_open)]
        orb_df = df.iloc[_slice_idx(ts, ny_open, orb_end)]
        setup_df = df.iloc[_slice_idx(ts, orb_end, setup_end)]
        if prior24.empty or len(orb_df) < 2 or setup_df.empty:
            skipped["missing_required_window"] = skipped.get("missing_required_window", 0) + 1
            continue

        orb = _profile(orb_df, bins)
        if not orb or orb["profile_width"] <= 0:
            skipped["invalid_orb_profile"] = skipped.get("invalid_orb_profile", 0) + 1
            continue

        prior24_profile = _profile(prior24, bins)
        p95_qty = float(np.quantile(prior24["qty"].to_numpy(dtype=np.float64), 0.95))
        session_df = df.iloc[_slice_idx(ts, orb_end, horizon_end)]
        candles = aggregate_trades_to_ohlcv(setup_df, "AVAXUSDC", "1m")
        pre_ny = _extreme(df.iloc[_slice_idx(ts, _ms(_dt(day, time(1, 30))), ny_open)])
        overnight = _extreme(df.iloc[_slice_idx(ts, _ms(_dt(day - timedelta(days=1), time(17, 30))), _ms(_dt(day, time(1, 30))))])
        previous_ny = _extreme(df.iloc[_slice_idx(ts, _ms(_dt(day - timedelta(days=1), time(9, 30))), _ms(_dt(day - timedelta(days=1), time(17, 30))))])

        touched_low = False
        touched_high = False
        long_done = False
        short_done = False
        for candle in candles:
            c_start = int(candle["timestamp_ms"])
            c_end = c_start + 60_000
            candle_trades = df.iloc[_slice_idx(ts, c_start, c_end)]
            if candle_trades.empty:
                continue
            current_touched_low = bool((candle_trades["price"] <= orb["profile_low"]).any())
            current_touched_high = bool((candle_trades["price"] >= orb["profile_high"]).any())
            if not long_done and not touched_low and not current_touched_low and float(candle["close"]) > orb["profile_high"]:
                samples.append(_make_sample(
                    day=day, direction="long", candle=candle, trade_df=session_df,
                    prior24_df=prior24, setup_from_orb_df=df.iloc[_slice_idx(ts, orb_end, c_end)],
                    breakout_candle_df=candle_trades, orb=orb, prior24_profile=prior24_profile,
                    p95_qty=p95_qty, pre_ny=pre_ny, overnight=overnight, previous_ny=previous_ny,
                    orb_end_ms=orb_end, horizon_end_ms=horizon_end,
                ))
                long_done = True
            if not short_done and not touched_high and not current_touched_high and float(candle["close"]) < orb["profile_low"]:
                samples.append(_make_sample(
                    day=day, direction="short", candle=candle, trade_df=session_df,
                    prior24_df=prior24, setup_from_orb_df=df.iloc[_slice_idx(ts, orb_end, c_end)],
                    breakout_candle_df=candle_trades, orb=orb, prior24_profile=prior24_profile,
                    p95_qty=p95_qty, pre_ny=pre_ny, overnight=overnight, previous_ny=previous_ny,
                    orb_end_ms=orb_end, horizon_end_ms=horizon_end,
                ))
                short_done = True
            touched_low = touched_low or current_touched_low
            touched_high = touched_high or current_touched_high
            if long_done and short_done:
                break

    sample_df = pd.DataFrame(samples)
    sample_df.to_parquet(output_dir / "sample_table.parquet", index=False)
    sample_df.to_parquet(output_dir / "feature_table.parquet", index=False)
    write_reports(output_dir, input_path, sample_df, skipped)
    return {"samples": len(sample_df), "skipped": skipped, "output_dir": str(output_dir)}


def _bucket(v: float) -> str:
    if not np.isfinite(v):
        return "unknown"
    if v < 0.5:
        return "failed"
    if v < 1.0:
        return "weak"
    if v < 2.0:
        return "decent"
    if v < 4.0:
        return "strong"
    return "exceptional"


def _with_directional_features(sample_df: pd.DataFrame) -> pd.DataFrame:
    df = sample_df.copy()
    direction_mult = np.where(df["direction"].eq("long"), 1.0, -1.0)
    df["breakout_delta_in_direction"] = direction_mult * df["breakout_delta"]
    df["orb_delta_in_direction"] = direction_mult * df["orb_delta"]
    df["post_orb_delta_in_direction"] = direction_mult * df["from_orb_end_to_breakout_net_delta"]
    df["last_5m_delta_in_direction"] = direction_mult * df["last_5m_net_delta"]
    df["last_15m_delta_in_direction"] = direction_mult * df["last_15m_net_delta"]
    df["breakout_close_follow_through"] = np.where(
        df["direction"].eq("long"),
        df["breakout_close_position_in_range"],
        1.0 - df["breakout_close_position_in_range"],
    )
    df["pre_breakout_abs_bubble_distance"] = df["pre_breakout_largest_bubble_distance_to_breakout_level"].abs()
    df["breakout_abs_bubble_distance"] = df["breakout_candle_largest_bubble_distance_to_breakout_level"].abs()
    df["abs_distance_previous_24h_poc"] = df["distance_to_previous_24h_poc_price"].abs()
    return df


def _feature_separation(
    sample_df: pd.DataFrame,
    positive_threshold: float,
    label: str,
    min_positive_rows: int = 5,
    min_failed_rows: int = 50,
) -> pd.DataFrame:
    df = _with_directional_features(sample_df)
    positive = df[df["max_expansion_orb_width_multiple"] >= positive_threshold]
    failed = df[df["max_expansion_orb_width_multiple"] < 0.5]
    if len(positive) < min_positive_rows or len(failed) < min_failed_rows:
        return pd.DataFrame()

    feature_cols = [
        "time_from_orb_end_to_breakout_seconds",
        "breakout_delta_in_direction",
        "breakout_volume",
        "breakout_buy_volume_pct",
        "breakout_body_to_range",
        "breakout_close_follow_through",
        "breakout_trade_count",
        "breakout_largest_trade_qty",
        "orb_profile_width",
        "orb_total_volume",
        "orb_delta_in_direction",
        "orb_poc_position_inside_profile",
        "orb_value_area_width",
        "orb_breakout_level_distance_from_poc",
        "post_orb_delta_in_direction",
        "from_orb_end_to_breakout_volume",
        "from_orb_end_to_breakout_buy_volume_pct",
        "from_orb_end_to_breakout_range",
        "last_5m_delta_in_direction",
        "last_5m_volume",
        "last_5m_buy_volume_pct",
        "last_5m_range",
        "last_15m_delta_in_direction",
        "last_15m_volume",
        "last_15m_buy_volume_pct",
        "last_15m_range",
        "pre_breakout_p95_bubble_count",
        "pre_breakout_buy_bubble_count",
        "pre_breakout_sell_bubble_count",
        "pre_breakout_largest_bubble_qty",
        "pre_breakout_abs_bubble_distance",
        "breakout_candle_p95_bubble_count",
        "breakout_candle_buy_bubble_count",
        "breakout_candle_sell_bubble_count",
        "breakout_candle_largest_bubble_qty",
        "breakout_abs_bubble_distance",
        "abs_distance_previous_24h_poc",
    ]
    rows = []
    for col in feature_cols:
        if col not in df.columns:
            continue
        positive_values = positive[col].dropna()
        failed_values = failed[col].dropna()
        if len(positive_values) < min_positive_rows or len(failed_values) < min_failed_rows:
            continue
        positive_median = float(positive_values.median())
        failed_median = float(failed_values.median())
        delta = positive_median - failed_median
        relative_delta = abs(delta) / (abs(failed_median) + 1e-9)
        rows.append(
            {
                "comparison": label,
                "feature": col,
                "positive_median": positive_median,
                "failed_median": failed_median,
                "median_delta": delta,
                "relative_delta": relative_delta,
                "positive_n": int(len(positive)),
                "failed_n": int(len(failed)),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("relative_delta", ascending=False).head(18)


def _pattern_report(sample_df: pd.DataFrame) -> str:
    lines = [
        "# Pattern Candidates",
        "",
        "These are exploratory distribution clues, not a validated trading edge.",
        "The breakout sample is deliberately broad: close beyond the frozen 09:30-09:45 NY ORB profile without touching the opposite side first.",
        "",
    ]
    if sample_df.empty:
        lines.append("_No samples._")
        return "\n".join(lines)

    df = sample_df.copy()
    df["bucket"] = df["max_expansion_orb_width_multiple"].map(_bucket)
    tail_counts = pd.DataFrame(
        [
            {
                "threshold_orb_width_multiple": threshold,
                "samples": int((df["max_expansion_orb_width_multiple"] >= threshold).sum()),
                "sample_rate": float((df["max_expansion_orb_width_multiple"] >= threshold).mean()),
            }
            for threshold in (0.5, 1.0, 2.0, 4.0)
        ]
    )
    lines.extend(["## Tail Size", "", _md_table(tail_counts), ""])
    lines.extend(
        [
            "## Feature Separation",
            "",
            "Direction-normalized delta columns mean positive values support the breakout direction.",
            "The comparison baseline is failed samples with max expansion below 0.5 ORB width.",
            "",
        ]
    )
    for threshold, label in ((0.5, "expanding_ge_0p5_vs_failed"), (1.0, "expanding_ge_1p0_vs_failed")):
        table = _feature_separation(df, threshold, label)
        lines.extend([f"### {label}", "", _md_table(table), ""])

    top_cols = [
        "session_day",
        "direction",
        "breakout_time",
        "end_reason",
        "max_expansion_orb_width_multiple",
        "time_from_orb_end_to_breakout_seconds",
        "breakout_candle_p95_bubble_count",
        "pre_breakout_p95_bubble_count",
        "orb_total_volume",
        "breakout_volume",
    ]
    top = df.sort_values("max_expansion_orb_width_multiple", ascending=False).head(15)
    lines.extend(["## Top Expansion Feature Rows", "", _md_table(top[top_cols]), ""])
    return "\n".join(lines)


def _quality_report(sample_df: pd.DataFrame, skipped: dict[str, int]) -> str:
    lines = [
        "# Data Quality Checks",
        "",
        "Grain: one accepted ORB breakout sample per NY session day.",
        "",
    ]
    if sample_df.empty:
        lines.append("_No samples._")
        return "\n".join(lines)

    critical_cols = [
        "session_day",
        "direction",
        "breakout_time",
        "breakout_level",
        "orb_profile_low",
        "orb_profile_high",
        "p95_qty_threshold",
        "max_expansion_orb_width_multiple",
        "end_reason",
    ]
    checks = pd.DataFrame(
        [
            {"check": "rows", "value": int(len(sample_df))},
            {"check": "unique_session_days", "value": int(sample_df["session_day"].nunique())},
            {"check": "duplicate_session_day_direction", "value": int(sample_df.duplicated(["session_day", "direction"]).sum())},
            {"check": "nonpositive_orb_width", "value": int((sample_df["orb_profile_width"] <= 0).sum())},
            {
                "check": "bad_breakout_window",
                "value": int(
                    (
                        (sample_df["time_from_orb_end_to_breakout_seconds"] < 0)
                        | (sample_df["time_from_orb_end_to_breakout_seconds"] > 1800)
                    ).sum()
                ),
            },
            {"check": "bad_end_before_breakout", "value": int((sample_df["end_timestamp_ms"] < sample_df["breakout_timestamp_ms"]).sum())},
            {"check": "skipped_edge_coverage", "value": int(skipped.get("edge_coverage", 0))},
        ]
    )
    null_rows = pd.DataFrame(
        [{"column": col, "nulls": int(sample_df[col].isna().sum())} for col in critical_cols]
    )
    end_reasons = sample_df["end_reason"].value_counts().rename_axis("end_reason").reset_index(name="samples")

    lines.extend(["## Core Checks", "", _md_table(checks), ""])
    lines.extend(["## Critical Nulls", "", _md_table(null_rows), ""])
    lines.extend(["## End Reasons", "", _md_table(end_reasons), ""])
    return "\n".join(lines)


def write_reports(output_dir: Path, input_path: Path, sample_df: pd.DataFrame, skipped: dict[str, int]) -> None:
    lines = [
        "# ORB 15m Breakout Distribution",
        "",
        f"Input: `{input_path}`",
        f"Samples: `{len(sample_df)}`",
        f"Skipped: `{json.dumps(skipped, sort_keys=True)}`",
        "",
    ]
    if not sample_df.empty:
        sample_df = sample_df.copy()
        sample_df["bucket"] = sample_df["max_expansion_orb_width_multiple"].map(_bucket)
        by_bucket = sample_df.groupby(["direction", "bucket"], dropna=False).size().reset_index(name="samples")
        lines.extend(["## Bucket Counts", "", _md_table(by_bucket), ""])
        thresholds = []
        for direction, group in sample_df.groupby("direction"):
            for threshold in (0.5, 1.0, 2.0, 4.0):
                thresholds.append({
                    "direction": direction,
                    "threshold_orb_width_multiple": threshold,
                    "hit_rate": float((group["max_expansion_orb_width_multiple"] >= threshold).mean()),
                    "samples": int(len(group)),
                })
        lines.extend(["## Expansion Hit Rates", "", _md_table(pd.DataFrame(thresholds)), ""])
        top = sample_df.sort_values("max_expansion_orb_width_multiple", ascending=False).head(20)
        cols = ["session_day", "direction", "breakout_time", "end_reason", "max_expansion_orb_width_multiple", "time_to_max_expansion_seconds"]
        lines.extend(["## Top Expansions", "", _md_table(top[cols]), ""])

    methodology = output_dir / "methodology.md"
    methodology.write_text((Path("models/orb/model/research_design_orb_15m_breakout_distribution.md").read_text(encoding="utf-8")), encoding="utf-8")
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")
    (output_dir / "distribution_summary.md").write_text("\n".join(lines), encoding="utf-8")
    (output_dir / "pattern_candidates.md").write_text(_pattern_report(sample_df), encoding="utf-8")
    (output_dir / "data_quality.md").write_text(_quality_report(sample_df, skipped), encoding="utf-8")


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows._"
    cols = list(df.columns)
    rows = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in df.iterrows():
        rows.append("| " + " | ".join(_fmt(row[col]) for col in cols) + " |")
    return "\n".join(rows)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.6g}"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--bins", type=int, default=50)
    args = parser.parse_args()
    summary = run(Path(args.input), Path(args.output_dir), args.bins)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
