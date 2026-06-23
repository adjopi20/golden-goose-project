#!/usr/bin/env python
"""
simulate_dynamic_exit.py

Golden Goose Project
Command-line only exit-model simulator.

Inputs:
1. impulse-bubble master parquet
2. raw aggTrades parquet(s)

Best current research filter:
    top quantile net_migration_R
    + top quantile price_range_R
    + top quantile total_trade_count

Default:
    top 10% for each metric.

Exit model:
    - Initial SL = -1R
    - If SL hits before +1R: full position exits at -1R
    - If +1R is reached first:
        * 50% position exits at +1R
        * remaining 50% activates trailing stop
        * trailing stop starts at entry / 0R
        * trailing distance = 1R
        * trailing moves 1:1 with favorable price
        * trailing never moves backward
    - No fee, spread, slippage
    - Independent-trade arithmetic summation, not geometric compounding
    - Open trades at result-horizon end are closed at final available price by default

Example:
python scripts/simulate_dynamic_exit.py ^
  --master research/130626/avaxusdc_0022/0022-master.parquet ^
  --raw storage/avaxusdc/parquet/AVAXUSDC-aggTrades-2024-06_to_2026-05.parquet ^
  --evidence-seconds 900 ^
  --result-seconds 3600 ^
  --filter-quantile 0.90
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd


# =========================
# Utility helpers
# =========================

def _read_parquet_many(paths: list[Path], columns: Optional[list[str]] = None) -> pd.DataFrame:
    frames = []
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(f"Raw parquet not found: {p}")
        frames.append(pd.read_parquet(p, columns=columns))

    if not frames:
        raise ValueError("No parquet paths supplied.")

    return pd.concat(frames, ignore_index=True)


def normalize_timestamp_series(s: pd.Series) -> pd.Series:
    """
    Normalize timestamps to timezone-naive pandas datetime64[ns].

    Supports:
    - datetime64
    - timezone-aware datetime
    - integer epoch in seconds/ms/us/ns
    - parseable string timestamps
    """
    if pd.api.types.is_datetime64_any_dtype(s):
        out = pd.to_datetime(s, errors="coerce")
    elif pd.api.types.is_numeric_dtype(s):
        non_null = s.dropna()

        if non_null.empty:
            out = pd.to_datetime(s, errors="coerce")
        else:
            mx = float(non_null.abs().max())

            if mx > 1e17:
                unit = "ns"
            elif mx > 1e14:
                unit = "us"
            elif mx > 1e11:
                unit = "ms"
            else:
                unit = "s"

            out = pd.to_datetime(s, unit=unit, errors="coerce", utc=True)
    else:
        out = pd.to_datetime(s, errors="coerce", utc=True)

    try:
        if getattr(out.dt, "tz", None) is not None:
            out = out.dt.tz_convert("UTC").dt.tz_localize(None)
    except AttributeError:
        pass

    return out


def ensure_columns(df: pd.DataFrame, required: Iterable[str], name: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"{name} missing required columns: {missing}")


def first_existing_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def safe_float(x, default=np.nan) -> float:
    try:
        if pd.isna(x):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def favorable_r_from_anchor(price: float, anchor_price: float, risk: float, bias: str) -> float:
    if not np.isfinite(price) or not np.isfinite(anchor_price) or risk <= 0:
        return np.nan

    bias = str(bias).lower()

    if bias == "long":
        return (price - anchor_price) / risk

    if bias == "short":
        return (anchor_price - price) / risk

    return np.nan


def get_last_price_at_or_before(ts_ns: np.ndarray, prices: np.ndarray, t_ns: int) -> tuple[float, int]:
    """
    Return last price at or before timestamp and its array index.
    If none exists, fallback to first price after timestamp.
    """
    idx = int(np.searchsorted(ts_ns, t_ns, side="right") - 1)

    if idx >= 0:
        return float(prices[idx]), idx

    idx2 = int(np.searchsorted(ts_ns, t_ns, side="left"))

    if idx2 < len(prices):
        return float(prices[idx2]), idx2

    return np.nan, -1


def window_indices(ts_ns: np.ndarray, start_ns: int, end_ns: int) -> tuple[int, int]:
    left = int(np.searchsorted(ts_ns, start_ns, side="left"))
    right = int(np.searchsorted(ts_ns, end_ns, side="left"))
    return left, right


# =========================
# Anchor construction
# =========================

def build_anchor_table(
    master: pd.DataFrame,
    evidence_seconds: int,
    result_seconds: int,
    default_stop_buffer_pct: float,
) -> pd.DataFrame:
    """
    Build rank-1 confirmed bubble anchors from master parquet.
    """
    ensure_columns(
        master,
        ["confirmed_setup_rank", "directional_bias", "setup_confirmation_timestamp"],
        "master",
    )

    m = master.copy()

    if "bubble_role" in m.columns:
        m = m[m["bubble_role"].astype(str).str.lower().eq("confirmed_setup")].copy()

    m = m[pd.to_numeric(m["confirmed_setup_rank"], errors="coerce").eq(1)].copy()

    if m.empty:
        raise ValueError("No rank-1 confirmed setup anchors found.")

    m["anchor_timestamp"] = normalize_timestamp_series(m["setup_confirmation_timestamp"])

    price_candidates = ["confirmation_price", "anchor_price", "bubble_price", "price"]
    price_col = first_existing_column(m, price_candidates)

    if price_col is None:
        raise KeyError(f"Master must contain one anchor price column among: {price_candidates}")

    m["anchor_price"] = pd.to_numeric(m[price_col], errors="coerce")

    if "stop_buffer_pct" in m.columns:
        m["stop_buffer_pct"] = (
            pd.to_numeric(m["stop_buffer_pct"], errors="coerce")
            .fillna(default_stop_buffer_pct)
        )
    else:
        m["stop_buffer_pct"] = default_stop_buffer_pct

    if "impulse_group_id" not in m.columns:
        m["impulse_group_id"] = np.arange(len(m), dtype=np.int64)

    m = m.sort_values("anchor_timestamp")
    m = m.drop_duplicates(subset=["impulse_group_id"], keep="first").copy()

    m["evidence_start_timestamp"] = m["anchor_timestamp"]
    m["evidence_end_timestamp"] = m["anchor_timestamp"] + pd.to_timedelta(evidence_seconds, unit="s")
    m["entry_timestamp"] = m["evidence_end_timestamp"]
    m["result_start_timestamp"] = m["entry_timestamp"]
    m["result_end_timestamp"] = m["entry_timestamp"] + pd.to_timedelta(result_seconds, unit="s")

    required_clean = ["anchor_timestamp", "anchor_price", "directional_bias", "stop_buffer_pct"]

    for c in required_clean:
        if m[c].isna().any():
            before = len(m)
            m = m.dropna(subset=[c]).copy()
            print(f"[WARN] Dropped {before - len(m)} anchors with null {c}")

    m = m[m["directional_bias"].astype(str).str.lower().isin(["long", "short"])].copy()
    m["directional_bias"] = m["directional_bias"].astype(str).str.lower()

    return m.reset_index(drop=True)


# =========================
# Feature reconstruction
# =========================

def compute_anchor_features(
    anchors: pd.DataFrame,
    ts_ns: np.ndarray,
    prices: np.ndarray,
    qty: np.ndarray,
    keep_incomplete: bool,
    progress_every: int = 1000,
) -> pd.DataFrame:
    """
    Compute filter features:
    - total_trade_count
    - price_range_R
    - net_migration_R

    Also computes:
    - entry_price
    - risk_per_unit
    - result-window indices
    """
    raw_start = int(ts_ns[0])
    raw_end = int(ts_ns[-1])

    rows = []
    skipped_incomplete = 0
    skipped_no_entry = 0

    anchor_ts_ns = anchors["anchor_timestamp"].astype("int64").to_numpy()
    entry_ts_ns = anchors["entry_timestamp"].astype("int64").to_numpy()
    result_end_ns = anchors["result_end_timestamp"].astype("int64").to_numpy()

    for i, row in anchors.iterrows():
        if progress_every and i and i % progress_every == 0:
            print(f"[features] processed {i:,}/{len(anchors):,}")

        a_ns = int(anchor_ts_ns[i])
        e_ns = int(entry_ts_ns[i])
        r_end_ns = int(result_end_ns[i])

        if not keep_incomplete:
            if a_ns < raw_start or r_end_ns > raw_end:
                skipped_incomplete += 1
                continue

        entry_price, _ = get_last_price_at_or_before(ts_ns, prices, e_ns)

        if not np.isfinite(entry_price):
            skipped_no_entry += 1
            continue

        bias = str(row["directional_bias"]).lower()
        stop_buffer_pct = safe_float(row["stop_buffer_pct"], 0.001)
        risk = entry_price * stop_buffer_pct

        if not np.isfinite(risk) or risk <= 0:
            skipped_no_entry += 1
            continue

        ev_l, ev_r = window_indices(ts_ns, a_ns, e_ns)
        ev_prices = prices[ev_l:ev_r]
        ev_qty = qty[ev_l:ev_r]

        if len(ev_prices) == 0:
            total_trade_count = 0
            total_qty = 0.0
            price_range_R = 0.0
            last_ev_price = entry_price
        else:
            total_trade_count = int(len(ev_prices))
            total_qty = float(np.nansum(ev_qty))
            price_range_R = float((np.nanmax(ev_prices) - np.nanmin(ev_prices)) / risk)
            last_ev_price = float(ev_prices[-1])

        anchor_price = safe_float(row["anchor_price"])
        net_migration_R = favorable_r_from_anchor(last_ev_price, anchor_price, risk, bias)
        entry_position_R = favorable_r_from_anchor(entry_price, anchor_price, risk, bias)

        res_l, res_r = window_indices(ts_ns, e_ns, r_end_ns)

        if not keep_incomplete and res_r <= res_l:
            skipped_incomplete += 1
            continue

        rows.append(
            {
                "observation_id": i,
                "impulse_group_id": row.get("impulse_group_id", i),
                "directional_bias": bias,
                "anchor_timestamp": row["anchor_timestamp"],
                "entry_timestamp": row["entry_timestamp"],
                "result_end_timestamp": row["result_end_timestamp"],
                "anchor_price": anchor_price,
                "entry_price": entry_price,
                "stop_buffer_pct": stop_buffer_pct,
                "risk_per_unit": risk,
                "total_trade_count": total_trade_count,
                "total_qty": total_qty,
                "price_range_R": price_range_R,
                "net_migration_R": net_migration_R,
                "entry_position_R": entry_position_R,
                "result_left_idx": res_l,
                "result_right_idx": res_r,
            }
        )

    print(f"[features] skipped incomplete windows: {skipped_incomplete:,}")
    print(f"[features] skipped missing entry/risk: {skipped_no_entry:,}")

    return pd.DataFrame(rows)


def add_best_filter_flags(features: pd.DataFrame, filter_quantile: float) -> tuple[pd.DataFrame, dict]:
    """
    Best current research filter:
    top quantile net_migration_R
    + top quantile price_range_R
    + top quantile total_trade_count
    """
    f = features.copy()

    q_net = float(f["net_migration_R"].quantile(filter_quantile))
    q_range = float(f["price_range_R"].quantile(filter_quantile))
    q_count = float(f["total_trade_count"].quantile(filter_quantile))

    mask = (
        f["net_migration_R"].ge(q_net)
        & f["price_range_R"].ge(q_range)
        & f["total_trade_count"].ge(q_count)
    )

    f["filter_top_directional_activity"] = mask
    f["filter_top_directional_activity_long"] = mask & f["directional_bias"].eq("long")
    f["filter_top_directional_activity_short"] = mask & f["directional_bias"].eq("short")

    thresholds = {
        "filter_quantile": filter_quantile,
        "net_migration_R_threshold": q_net,
        "price_range_R_threshold": q_range,
        "total_trade_count_threshold": q_count,
        "filter_logic": (
            f"net_migration_R >= q{filter_quantile:.2f} "
            f"AND price_range_R >= q{filter_quantile:.2f} "
            f"AND total_trade_count >= q{filter_quantile:.2f}"
        ),
    }

    return f, thresholds


# =========================
# Exit simulation
# =========================

def simulate_one_trade(
    result_prices: np.ndarray,
    entry_price: float,
    risk: float,
    bias: str,
    close_open_at_horizon: bool = True,
) -> dict:
    """
    Simulate:
    - 50% TP at +1R
    - 50% trailing after +1R
    - trailing distance = 1R
    - trailing starts at entry / 0R
    """
    bias = str(bias).lower()

    if result_prices.size == 0 or not np.isfinite(entry_price) or risk <= 0:
        return {
            "exit_R": np.nan,
            "exit_reason": "no_result_prices",
            "tp1_hit": False,
            "initial_sl_hit": False,
            "trailing_stop_hit": False,
            "horizon_close": False,
            "max_favorable_R_seen": np.nan,
            "max_adverse_R_seen": np.nan,
        }

    max_fav_seen = -np.inf
    max_adv_seen = 0.0

    tp1_hit = False

    if bias == "long":
        initial_sl = entry_price - risk
        tp1 = entry_price + risk

        active_trail = False
        high = entry_price
        trail_stop = entry_price

        for p in result_prices:
            p = float(p)
            r = (p - entry_price) / risk
            max_fav_seen = max(max_fav_seen, r)
            max_adv_seen = min(max_adv_seen, r)

            if not active_trail:
                if p <= initial_sl:
                    return {
                        "exit_R": -1.0,
                        "exit_reason": "initial_sl",
                        "tp1_hit": False,
                        "initial_sl_hit": True,
                        "trailing_stop_hit": False,
                        "horizon_close": False,
                        "max_favorable_R_seen": max(0.0, max_fav_seen),
                        "max_adverse_R_seen": max_adv_seen,
                    }

                if p >= tp1:
                    tp1_hit = True
                    active_trail = True
                    high = max(high, p)
                    trail_stop = max(entry_price, high - risk)
                    continue

            else:
                high = max(high, p)
                trail_stop = max(trail_stop, high - risk)

                if p <= trail_stop:
                    trail_R = (trail_stop - entry_price) / risk
                    exit_R = 0.5 * 1.0 + 0.5 * trail_R

                    return {
                        "exit_R": float(exit_R),
                        "exit_reason": "trailing_stop",
                        "tp1_hit": True,
                        "initial_sl_hit": False,
                        "trailing_stop_hit": True,
                        "horizon_close": False,
                        "max_favorable_R_seen": max(0.0, max_fav_seen),
                        "max_adverse_R_seen": max_adv_seen,
                    }

        final_p = float(result_prices[-1])
        final_R = (final_p - entry_price) / risk

        if close_open_at_horizon:
            if active_trail:
                exit_R = 0.5 * 1.0 + 0.5 * final_R
            else:
                exit_R = final_R

            return {
                "exit_R": float(exit_R),
                "exit_reason": "horizon_close",
                "tp1_hit": bool(tp1_hit),
                "initial_sl_hit": False,
                "trailing_stop_hit": False,
                "horizon_close": True,
                "max_favorable_R_seen": max(0.0, max_fav_seen),
                "max_adverse_R_seen": max_adv_seen,
            }

    elif bias == "short":
        initial_sl = entry_price + risk
        tp1 = entry_price - risk

        active_trail = False
        low = entry_price
        trail_stop = entry_price

        for p in result_prices:
            p = float(p)
            r = (entry_price - p) / risk
            max_fav_seen = max(max_fav_seen, r)
            max_adv_seen = min(max_adv_seen, r)

            if not active_trail:
                if p >= initial_sl:
                    return {
                        "exit_R": -1.0,
                        "exit_reason": "initial_sl",
                        "tp1_hit": False,
                        "initial_sl_hit": True,
                        "trailing_stop_hit": False,
                        "horizon_close": False,
                        "max_favorable_R_seen": max(0.0, max_fav_seen),
                        "max_adverse_R_seen": max_adv_seen,
                    }

                if p <= tp1:
                    tp1_hit = True
                    active_trail = True
                    low = min(low, p)
                    trail_stop = min(entry_price, low + risk)
                    continue

            else:
                low = min(low, p)
                trail_stop = min(trail_stop, low + risk)

                if p >= trail_stop:
                    trail_R = (entry_price - trail_stop) / risk
                    exit_R = 0.5 * 1.0 + 0.5 * trail_R

                    return {
                        "exit_R": float(exit_R),
                        "exit_reason": "trailing_stop",
                        "tp1_hit": True,
                        "initial_sl_hit": False,
                        "trailing_stop_hit": True,
                        "horizon_close": False,
                        "max_favorable_R_seen": max(0.0, max_fav_seen),
                        "max_adverse_R_seen": max_adv_seen,
                    }

        final_p = float(result_prices[-1])
        final_R = (entry_price - final_p) / risk

        if close_open_at_horizon:
            if active_trail:
                exit_R = 0.5 * 1.0 + 0.5 * final_R
            else:
                exit_R = final_R

            return {
                "exit_R": float(exit_R),
                "exit_reason": "horizon_close",
                "tp1_hit": bool(tp1_hit),
                "initial_sl_hit": False,
                "trailing_stop_hit": False,
                "horizon_close": True,
                "max_favorable_R_seen": max(0.0, max_fav_seen),
                "max_adverse_R_seen": max_adv_seen,
            }

    return {
        "exit_R": np.nan,
        "exit_reason": "open_not_closed",
        "tp1_hit": bool(tp1_hit),
        "initial_sl_hit": False,
        "trailing_stop_hit": False,
        "horizon_close": False,
        "max_favorable_R_seen": max(0.0, max_fav_seen) if np.isfinite(max_fav_seen) else np.nan,
        "max_adverse_R_seen": max_adv_seen,
    }


def simulate_trades(
    features: pd.DataFrame,
    prices: np.ndarray,
    close_open_at_horizon: bool,
    progress_every: int = 1000,
) -> pd.DataFrame:
    rows = []

    for i, row in features.iterrows():
        if progress_every and i and i % progress_every == 0:
            print(f"[simulate] processed {i:,}/{len(features):,}")

        l = int(row["result_left_idx"])
        r = int(row["result_right_idx"])
        result_prices = prices[l:r]

        sim = simulate_one_trade(
            result_prices=result_prices,
            entry_price=float(row["entry_price"]),
            risk=float(row["risk_per_unit"]),
            bias=str(row["directional_bias"]),
            close_open_at_horizon=close_open_at_horizon,
        )

        out = {
            "observation_id": row["observation_id"],
            "impulse_group_id": row["impulse_group_id"],
            "directional_bias": row["directional_bias"],
            "filter_top_directional_activity": bool(row["filter_top_directional_activity"]),
            "filter_top_directional_activity_long": bool(row["filter_top_directional_activity_long"]),
            "filter_top_directional_activity_short": bool(row["filter_top_directional_activity_short"]),
        }

        out.update(sim)
        rows.append(out)

    return pd.DataFrame(rows)


# =========================
# Summary
# =========================

def summarize_sample(name: str, trades: pd.DataFrame, mask: pd.Series) -> dict:
    x = trades.loc[mask].copy()
    n = len(x)

    if n == 0:
        return {
            "sample": name,
            "sample_n": 0,
            "sample_pct": 0.0,
            "total_R_sum": np.nan,
            "mean_R_per_trade": np.nan,
            "median_R_per_trade": np.nan,
            "profit_factor": np.nan,
        }

    r = pd.to_numeric(x["exit_R"], errors="coerce").dropna()
    n_valid = len(r)

    gross_profit = float(r[r > 0].sum())
    gross_loss = float(-r[r < 0].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    return {
        "sample": name,
        "sample_n": n,
        "valid_n": n_valid,
        "sample_pct": n / len(trades) if len(trades) else np.nan,
        "total_R_sum": float(r.sum()),
        "mean_R_per_trade": float(r.mean()) if n_valid else np.nan,
        "median_R_per_trade": float(r.median()) if n_valid else np.nan,
        "win_rate": float((r > 0).mean()) if n_valid else np.nan,
        "loss_rate": float((r < 0).mean()) if n_valid else np.nan,
        "breakeven_rate": float((r == 0).mean()) if n_valid else np.nan,
        "hit_1R_exit_rate": float((r >= 1.0).mean()) if n_valid else np.nan,
        "hit_2R_exit_rate": float((r >= 2.0).mean()) if n_valid else np.nan,
        "hit_3R_exit_rate": float((r >= 3.0).mean()) if n_valid else np.nan,
        "hit_5R_exit_rate": float((r >= 5.0).mean()) if n_valid else np.nan,
        "max_win_R": float(r.max()) if n_valid else np.nan,
        "max_loss_R": float(r.min()) if n_valid else np.nan,
        "gross_profit_R": gross_profit,
        "gross_loss_R": gross_loss,
        "profit_factor": float(profit_factor),
        "tp1_hit_rate": float(x["tp1_hit"].mean()) if "tp1_hit" in x.columns else np.nan,
        "initial_sl_hit_rate": float(x["initial_sl_hit"].mean()) if "initial_sl_hit" in x.columns else np.nan,
        "trailing_stop_hit_rate": float(x["trailing_stop_hit"].mean()) if "trailing_stop_hit" in x.columns else np.nan,
        "horizon_close_rate": float(x["horizon_close"].mean()) if "horizon_close" in x.columns else np.nan,
    }


def build_summary(trades: pd.DataFrame) -> pd.DataFrame:
    all_mask = pd.Series(True, index=trades.index)
    filt = trades["filter_top_directional_activity"].astype(bool)
    long = trades["directional_bias"].eq("long")
    short = trades["directional_bias"].eq("short")

    summaries = [
        summarize_sample("baseline_all", trades, all_mask),
        summarize_sample("baseline_long", trades, long),
        summarize_sample("baseline_short", trades, short),
        summarize_sample("top_directional_activity_all", trades, filt),
        summarize_sample("top_directional_activity_long", trades, filt & long),
        summarize_sample("top_directional_activity_short", trades, filt & short),
    ]

    summary = pd.DataFrame(summaries)

    ordered_cols = [
        "sample",
        "sample_n",
        "valid_n",
        "sample_pct",
        "total_R_sum",
        "mean_R_per_trade",
        "median_R_per_trade",
        "profit_factor",
        "win_rate",
        "loss_rate",
        "breakeven_rate",
        "hit_1R_exit_rate",
        "hit_2R_exit_rate",
        "hit_3R_exit_rate",
        "hit_5R_exit_rate",
        "max_win_R",
        "max_loss_R",
        "gross_profit_R",
        "gross_loss_R",
        "tp1_hit_rate",
        "initial_sl_hit_rate",
        "trailing_stop_hit_rate",
        "horizon_close_rate",
    ]

    return summary[[c for c in ordered_cols if c in summary.columns]]


# =========================
# Main
# =========================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate 50% TP at 1R + 50% 1R trailing stop on dynamic directional-activity filter."
    )

    parser.add_argument("--master", required=True, type=Path, help="Path to impulse bubble master parquet.")
    parser.add_argument("--raw", required=True, nargs="+", type=Path, help="One or more raw aggTrades parquet paths.")

    parser.add_argument("--evidence-seconds", type=int, default=900)
    parser.add_argument("--result-seconds", type=int, default=3600)
    parser.add_argument("--filter-quantile", type=float, default=0.90)
    parser.add_argument("--default-stop-buffer-pct", type=float, default=0.001)

    parser.add_argument("--keep-incomplete-result-windows", action="store_true")

    parser.add_argument(
        "--no-close-open-at-horizon",
        action="store_true",
        help="If set, trades that do not hit SL/TP/trail are left open as NaN instead of closed at final horizon price.",
    )

    parser.add_argument("--progress-every", type=int, default=1000)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.master.exists():
        raise FileNotFoundError(f"Master parquet not found: {args.master}")

    print("\n==============================")
    print("Dynamic Exit Simulation")
    print("==============================")
    print(f"master: {args.master}")
    print("raw:")
    for p in args.raw:
        print(f"  - {p}")
    print(f"evidence_seconds: {args.evidence_seconds}")
    print(f"result_seconds: {args.result_seconds}")
    print(f"filter_quantile: {args.filter_quantile}")
    print(f"default_stop_buffer_pct: {args.default_stop_buffer_pct}")
    print(f"close_open_at_horizon: {not args.no_close_open_at_horizon}")

    print("\n[load] master")
    master = pd.read_parquet(args.master)

    raw_cols = ["timestamp", "price", "qty"]

    print("[load] raw aggTrades")
    raw = _read_parquet_many(args.raw, columns=raw_cols)
    ensure_columns(raw, raw_cols, "raw aggTrades")

    print(f"[load] raw rows: {len(raw):,}")

    raw["timestamp"] = normalize_timestamp_series(raw["timestamp"])
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce")
    raw["qty"] = pd.to_numeric(raw["qty"], errors="coerce")

    raw = raw.dropna(subset=["timestamp", "price", "qty"]).sort_values("timestamp").reset_index(drop=True)

    print(f"[load] raw rows after clean/sort: {len(raw):,}")

    ts_ns = raw["timestamp"].astype("int64").to_numpy()
    prices = raw["price"].astype("float64").to_numpy()
    qty = raw["qty"].astype("float64").to_numpy()

    if not np.all(ts_ns[:-1] <= ts_ns[1:]):
        raise ValueError("Raw timestamps are not sorted ascending.")

    print("\n[anchors] building rank-1 anchors")
    anchors = build_anchor_table(
        master=master,
        evidence_seconds=args.evidence_seconds,
        result_seconds=args.result_seconds,
        default_stop_buffer_pct=args.default_stop_buffer_pct,
    )

    print(f"[anchors] anchors: {len(anchors):,}")

    print("\n[features] reconstructing evidence features")
    features = compute_anchor_features(
        anchors=anchors,
        ts_ns=ts_ns,
        prices=prices,
        qty=qty,
        keep_incomplete=args.keep_incomplete_result_windows,
        progress_every=args.progress_every,
    )

    print(f"[features] usable observations: {len(features):,}")

    features, thresholds = add_best_filter_flags(features, filter_quantile=args.filter_quantile)

    print("\n[filter] thresholds")
    for k, v in thresholds.items():
        print(f"{k}: {v}")

    print("\n[simulate] running exit model")
    trades = simulate_trades(
        features=features,
        prices=prices,
        close_open_at_horizon=not args.no_close_open_at_horizon,
        progress_every=args.progress_every,
    )

    summary = build_summary(trades)

    print("\n==============================")
    print("Exit Simulation Summary")
    print("==============================")

    with pd.option_context(
        "display.max_columns",
        100,
        "display.width",
        260,
        "display.float_format",
        "{:.6f}".format,
    ):
        print(summary.to_string(index=False))

    print("\nNotes:")
    print("- total_R_sum is simple arithmetic sum of independent trade R results.")
    print("- No fee, spread, slippage, or compounding is included.")
    print("- top_directional_activity = top quantile net_migration_R + price_range_R + total_trade_count.")
    print("- Profitability check: total_R_sum > 0 and mean_R_per_trade > 0.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        raise