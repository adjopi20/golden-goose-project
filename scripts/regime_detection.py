from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


# =============================================================================
# Timestamp / schema handling
# =============================================================================

def pick_col(columns, candidates, required=True):
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    if required:
        raise ValueError(f"Could not find required column. Tried: {candidates}. Available: {list(columns)}")
    return None


def normalize_timestamp(series: pd.Series) -> pd.Series:
    """
    Robust timestamp parser.

    Important:
    Binance aggTrade timestamp is usually milliseconds.
    If you pass millisecond timestamps directly into pd.Timestamp(),
    you get wrong 1970 dates.

    This function detects:
    - seconds
    - milliseconds
    - microseconds
    - nanoseconds
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, utc=True)

    x = pd.to_numeric(series, errors="coerce")
    max_abs = float(np.nanmax(np.abs(x.to_numpy())))

    if max_abs > 1e17:
        unit = "ns"
    elif max_abs > 1e14:
        unit = "us"
    elif max_abs > 1e11:
        unit = "ms"
    else:
        unit = "s"

    return pd.to_datetime(x, unit=unit, utc=True)


def timestamp_to_ns(series: pd.Series) -> np.ndarray:
    s = pd.to_datetime(series, utc=True)

    # Convert timezone-aware timestamp to UTC-naive nanosecond precision.
    s = s.dt.tz_convert("UTC").dt.tz_localize(None)

    return s.astype("datetime64[ns]").astype("int64").to_numpy()


def timezone_aware_to_naive_utc(series: pd.Series) -> pd.Series:
    if not pd.api.types.is_datetime64_any_dtype(series):
        return series

    s = pd.to_datetime(series, utc=True)
    return s.dt.tz_convert("UTC").dt.tz_localize(None)


def load_raw_aggtrades(path: Path) -> pd.DataFrame:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    columns = pf.schema.names

    ts_col = pick_col(columns, ["timestamp", "event_timestamp", "T", "time", "transact_time"])
    price_col = pick_col(columns, ["price", "p"])
    qty_col = pick_col(columns, ["qty", "quantity", "q"])
    maker_col = pick_col(columns, ["is_buyer_maker", "m"], required=False)

    read_cols = [ts_col, price_col, qty_col]
    if maker_col:
        read_cols.append(maker_col)

    df = pd.read_parquet(path, columns=read_cols)

    df = df.rename(
        columns={
            ts_col: "timestamp",
            price_col: "price",
            qty_col: "qty",
        }
    )

    if maker_col:
        df = df.rename(columns={maker_col: "is_buyer_maker"})
    else:
        df["is_buyer_maker"] = np.nan

    df["timestamp"] = normalize_timestamp(df["timestamp"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float64")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("float64")
    df["notional"] = df["price"] * df["qty"]

    df = df.dropna(subset=["timestamp", "price", "qty"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    if df["is_buyer_maker"].notna().any():
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        # Binance convention:
        # is_buyer_maker = False => buyer was aggressive
        # is_buyer_maker = True  => seller was aggressive
        df["aggressor_side"] = np.where(df["is_buyer_maker"], -1, 1)
    else:
        df["aggressor_side"] = 0

    print(f"Loaded rows: {len(df):,}")
    print(f"timestamp min: {df['timestamp'].min()}")
    print(f"timestamp max: {df['timestamp'].max()}")
    print(f"price min/max: {df['price'].min()} / {df['price'].max()}")

    return df


# =============================================================================
# Session handling
# =============================================================================

def parse_hhmm(value: str) -> pd.Timedelta:
    h, m = value.split(":")
    return pd.Timedelta(hours=int(h), minutes=int(m))


def add_session_columns(df: pd.DataFrame, cutoff_utc: str) -> pd.DataFrame:
    cutoff = parse_hhmm(cutoff_utc)

    # Session day starts at cutoff UTC, e.g. 13:30 UTC.
    session_start = (df["timestamp"] - cutoff).dt.floor("D") + cutoff

    df["session_start"] = session_start
    df["session_id"] = session_start.dt.strftime("%Y-%m-%d")
    df["previous_session_id"] = (session_start - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")

    return df


# =============================================================================
# Volume profile
# =============================================================================

def value_area_from_profile(profile: pd.Series, value_area_pct: float = 0.70):
    """
    Build value area by expanding outward from POC.

    profile index = price_bin
    profile value = volume/qty
    """
    profile = profile.sort_index()
    prices = profile.index.to_numpy(dtype="float64")
    vols = profile.to_numpy(dtype="float64")

    total_vol = vols.sum()
    if len(prices) == 0 or total_vol <= 0:
        return np.nan, np.nan, np.nan

    poc_idx = int(np.argmax(vols))
    lo = poc_idx
    hi = poc_idx
    cum = vols[poc_idx]
    target = total_vol * value_area_pct

    while cum < target and (lo > 0 or hi < len(prices) - 1):
        left_vol = vols[lo - 1] if lo > 0 else -np.inf
        right_vol = vols[hi + 1] if hi < len(prices) - 1 else -np.inf

        if right_vol >= left_vol:
            hi += 1
            cum += vols[hi]
        else:
            lo -= 1
            cum += vols[lo]

    val = prices[lo]
    vah = prices[hi]
    poc = prices[poc_idx]

    return val, vah, poc


def build_session_profiles(
    df: pd.DataFrame,
    tick_size: float | None,
    profile_bin_pct: float,
    value_area_pct: float,
) -> pd.DataFrame:
    rows = []

    for session_id, g in df.groupby("session_id", sort=True):
        if len(g) < 10:
            continue

        median_price = float(g["price"].median())

        if tick_size and tick_size > 0:
            price_step = float(tick_size)
        else:
            price_step = max(median_price * profile_bin_pct, 1e-12)

        price_bin = np.round(g["price"].to_numpy() / price_step) * price_step
        profile = pd.Series(g["qty"].to_numpy(), index=price_bin).groupby(level=0).sum()

        val, vah, poc = value_area_from_profile(profile, value_area_pct=value_area_pct)

        rows.append(
            {
                "session_id": session_id,
                "session_start": g["session_start"].iloc[0],
                "session_end": g["session_start"].iloc[0] + pd.Timedelta(days=1),
                "session_trade_count": len(g),
                "session_qty": float(g["qty"].sum()),
                "session_notional": float(g["notional"].sum()),
                "session_low": float(g["price"].min()),
                "session_high": float(g["price"].max()),
                "previous_val": val,
                "previous_vah": vah,
                "previous_poc": poc,
                "profile_price_step": price_step,
                "value_width": vah - val if pd.notna(vah) and pd.notna(val) else np.nan,
            }
        )

    profiles = pd.DataFrame(rows)
    return profiles


# =============================================================================
# Feature calculation
# =============================================================================

def safe_div(a, b, default=np.nan):
    if b is None or b == 0 or pd.isna(b):
        return default
    return a / b


def location_masks(price: np.ndarray, val: float, vah: float):
    above = price > vah
    below = price < val
    inside = (price >= val) & (price <= vah)
    return above, below, inside


def duration_location_shares(
    ts_ns: np.ndarray,
    price: np.ndarray,
    window_end_ns: int,
    val: float,
    vah: float,
):
    """
    Approximate time share by assigning each trade price until the next trade.
    """
    if len(ts_ns) == 0:
        return np.nan, np.nan, np.nan

    next_ts = np.empty_like(ts_ns)
    next_ts[:-1] = ts_ns[1:]
    next_ts[-1] = window_end_ns

    dur = np.maximum(next_ts - ts_ns, 0).astype("float64")
    total = dur.sum()

    if total <= 0:
        return np.nan, np.nan, np.nan

    above, below, inside = location_masks(price, val, vah)

    return (
        float(dur[above].sum() / total),
        float(dur[below].sum() / total),
        float(dur[inside].sum() / total),
    )


def bar_extreme_counts(
    ts_ns: np.ndarray,
    price: np.ndarray,
    window_start_ns: int,
    window_end_ns: int,
    bar_seconds: int,
    min_increment: float,
):
    """
    Counts new highs/lows on compressed bars instead of raw trades.
    This avoids counting every tiny tick as a new extreme.
    """
    if len(ts_ns) == 0:
        return 0, 0

    bar_ns = int(bar_seconds * 1_000_000_000)
    n_bars = max(1, int(math.ceil((window_end_ns - window_start_ns) / bar_ns)))

    idx = ((ts_ns - window_start_ns) // bar_ns).astype("int64")
    idx = np.clip(idx, 0, n_bars - 1)

    highs = np.full(n_bars, -np.inf)
    lows = np.full(n_bars, np.inf)

    np.maximum.at(highs, idx, price)
    np.minimum.at(lows, idx, price)

    valid = np.isfinite(highs) & np.isfinite(lows)
    highs = highs[valid]
    lows = lows[valid]

    if len(highs) == 0:
        return 0, 0

    new_high_count = 0
    current_high = highs[0]

    for h in highs[1:]:
        if h > current_high + min_increment:
            new_high_count += 1
            current_high = h
        else:
            current_high = max(current_high, h)

    new_low_count = 0
    current_low = lows[0]

    for l in lows[1:]:
        if l < current_low - min_increment:
            new_low_count += 1
            current_low = l
        else:
            current_low = min(current_low, l)

    return int(new_high_count), int(new_low_count)


def compute_window_features(
    t_ns: np.ndarray,
    price: np.ndarray,
    qty: np.ndarray,
    notional: np.ndarray,
    side: np.ndarray,
    window_start_ns: int,
    window_end_ns: int,
    val: float,
    vah: float,
    poc: float,
    value_width: float,
    price_step: float,
    bar_seconds: int,
):
    start_idx = np.searchsorted(t_ns, window_start_ns, side="left")
    end_idx = np.searchsorted(t_ns, window_end_ns, side="left")

    if end_idx <= start_idx:
        return None

    tt = t_ns[start_idx:end_idx]
    pp = price[start_idx:end_idx]
    qq = qty[start_idx:end_idx]
    nn = notional[start_idx:end_idx]
    ss = side[start_idx:end_idx]

    if len(pp) < 3:
        return None

    open_price = float(pp[0])
    close_price = float(pp[-1])
    high_price = float(np.max(pp))
    low_price = float(np.min(pp))
    median_price = float(np.median(pp))

    total_qty = float(np.sum(qq))
    total_notional = float(np.sum(nn))

    above, below, inside = location_masks(pp, val, vah)

    qty_above = float(np.sum(qq[above]))
    qty_below = float(np.sum(qq[below]))
    qty_inside = float(np.sum(qq[inside]))

    notional_above = float(np.sum(nn[above]))
    notional_below = float(np.sum(nn[below]))
    notional_inside = float(np.sum(nn[inside]))

    time_above, time_below, time_inside = duration_location_shares(
        tt, pp, window_end_ns, val, vah
    )

    mid_ns = window_start_ns + (window_end_ns - window_start_ns) // 2
    first_half = tt < mid_ns
    second_half = tt >= mid_ns

    if first_half.sum() > 0:
        median_first = float(np.median(pp[first_half]))
        vwap_first = float(np.sum(pp[first_half] * qq[first_half]) / np.sum(qq[first_half]))
    else:
        median_first = np.nan
        vwap_first = np.nan

    if second_half.sum() > 0:
        median_second = float(np.median(pp[second_half]))
        vwap_second = float(np.sum(pp[second_half] * qq[second_half]) / np.sum(qq[second_half]))
    else:
        median_second = np.nan
        vwap_second = np.nan

    second_pp = pp[second_half]
    second_qq = qq[second_half]

    if len(second_pp) > 0 and np.sum(second_qq) > 0:
        second_above, second_below, second_inside = location_masks(second_pp, val, vah)
        second_qty_above_share = float(np.sum(second_qq[second_above]) / np.sum(second_qq))
        second_qty_below_share = float(np.sum(second_qq[second_below]) / np.sum(second_qq))
        second_qty_inside_share = float(np.sum(second_qq[second_inside]) / np.sum(second_qq))
    else:
        second_qty_above_share = np.nan
        second_qty_below_share = np.nan
        second_qty_inside_share = np.nan

    price_range = high_price - low_price
    signed_efficiency = safe_div(close_price - open_price, price_range, default=0.0)

    if value_width and value_width > 0:
        median_change_value_width = (median_second - median_first) / value_width
        vwap_change_value_width = (vwap_second - vwap_first) / value_width
        close_vs_poc_value_width = (close_price - poc) / value_width
        range_value_width = price_range / value_width
    else:
        median_change_value_width = np.nan
        vwap_change_value_width = np.nan
        close_vs_poc_value_width = np.nan
        range_value_width = np.nan

    new_high_count, new_low_count = bar_extreme_counts(
        tt,
        pp,
        window_start_ns,
        window_end_ns,
        bar_seconds=bar_seconds,
        min_increment=price_step,
    )

    buy_qty = float(np.sum(qq[ss == 1])) if not np.all(ss == 0) else np.nan
    sell_qty = float(np.sum(qq[ss == -1])) if not np.all(ss == 0) else np.nan
    total_aggr_qty = buy_qty + sell_qty if pd.notna(buy_qty) and pd.notna(sell_qty) else np.nan

    buy_qty_share = safe_div(buy_qty, total_aggr_qty)
    sell_qty_share = safe_div(sell_qty, total_aggr_qty)

    return {
        "trade_count": int(len(pp)),
        "open_price": open_price,
        "close_price": close_price,
        "high_price": high_price,
        "low_price": low_price,
        "median_price": median_price,
        "total_qty": total_qty,
        "total_notional": total_notional,

        "time_above_vah_share": time_above,
        "time_below_val_share": time_below,
        "time_inside_value_share": time_inside,

        "qty_above_vah_share": safe_div(qty_above, total_qty),
        "qty_below_val_share": safe_div(qty_below, total_qty),
        "qty_inside_value_share": safe_div(qty_inside, total_qty),

        "notional_above_vah_share": safe_div(notional_above, total_notional),
        "notional_below_val_share": safe_div(notional_below, total_notional),
        "notional_inside_value_share": safe_div(notional_inside, total_notional),

        "median_first_half": median_first,
        "median_second_half": median_second,
        "vwap_first_half": vwap_first,
        "vwap_second_half": vwap_second,
        "median_change_value_width": median_change_value_width,
        "vwap_change_value_width": vwap_change_value_width,
        "close_vs_poc_value_width": close_vs_poc_value_width,
        "range_value_width": range_value_width,

        "second_half_qty_above_vah_share": second_qty_above_share,
        "second_half_qty_below_val_share": second_qty_below_share,
        "second_half_qty_inside_value_share": second_qty_inside_share,

        "signed_efficiency": signed_efficiency,
        "new_high_count": new_high_count,
        "new_low_count": new_low_count,

        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "buy_qty_share": buy_qty_share,
        "sell_qty_share": sell_qty_share,
    }


# =============================================================================
# Regime classification
# =============================================================================

def classify_regime(row, args):
    accepted_above = (
        row["time_above_vah_share"] >= args.accept_share
        and row["qty_above_vah_share"] >= args.accept_share
        and row["close_price"] > row["previous_vah"]
    )

    accepted_below = (
        row["time_below_val_share"] >= args.accept_share
        and row["qty_below_val_share"] >= args.accept_share
        and row["close_price"] < row["previous_val"]
    )

    accepted_inside = (
        row["time_inside_value_share"] >= args.balance_share
        and row["qty_inside_value_share"] >= args.balance_share
    )

    upward_migration = (
        row["median_change_value_width"] >= args.min_migration_value_width
        and row["signed_efficiency"] >= args.min_efficiency
        and row["new_high_count"] >= args.min_new_extreme_count
    )

    downward_migration = (
        row["median_change_value_width"] <= -args.min_migration_value_width
        and row["signed_efficiency"] <= -args.min_efficiency
        and row["new_low_count"] >= args.min_new_extreme_count
    )

    flat_rotation = (
        abs(row["median_change_value_width"]) <= args.flat_migration_value_width
        and abs(row["signed_efficiency"]) <= args.flat_efficiency
    )

    upside_attempt = row["high_price"] > row["previous_vah"]
    downside_attempt = row["low_price"] < row["previous_val"]

    reaccepted_from_above = (
        upside_attempt
        and row["close_price"] <= row["previous_vah"]
        and (
            row["second_half_qty_inside_value_share"] >= args.failed_second_half_share
            or row["time_inside_value_share"] >= args.balance_share
        )
    )

    reaccepted_from_below = (
        downside_attempt
        and row["close_price"] >= row["previous_val"]
        and (
            row["second_half_qty_inside_value_share"] >= args.failed_second_half_share
            or row["time_inside_value_share"] >= args.balance_share
        )
    )

    if accepted_above and upward_migration and not reaccepted_from_above:
        return "UP_IMBALANCE_ACCEPTED"

    if accepted_below and downward_migration and not reaccepted_from_below:
        return "DOWN_IMBALANCE_ACCEPTED"

    if reaccepted_from_above and not accepted_above:
        return "FAILED_UPSIDE_AUCTION"

    if reaccepted_from_below and not accepted_below:
        return "FAILED_DOWNSIDE_AUCTION"

    if accepted_inside and flat_rotation:
        return "BALANCE_INSIDE_PREVIOUS_VALUE"

    if accepted_above and flat_rotation:
        return "BALANCE_ABOVE_PREVIOUS_VALUE"

    if accepted_below and flat_rotation:
        return "BALANCE_BELOW_PREVIOUS_VALUE"

    return "TRANSITION_OR_NOISE"


def regime_direction(label: str) -> int:
    if label == "UP_IMBALANCE_ACCEPTED":
        return 1
    if label == "DOWN_IMBALANCE_ACCEPTED":
        return -1
    return 0


# =============================================================================
# Future outcome
# =============================================================================

def compute_future_result(
    t_ns: np.ndarray,
    price: np.ndarray,
    entry_price: float,
    result_start_ns: int,
    result_end_ns: int,
    direction: int,
):
    start_idx = np.searchsorted(t_ns, result_start_ns, side="left")
    end_idx = np.searchsorted(t_ns, result_end_ns, side="left")

    if end_idx <= start_idx:
        return None

    pp = price[start_idx:end_idx]

    future_high = float(np.max(pp))
    future_low = float(np.min(pp))
    future_close = float(pp[-1])

    future_return_pct = (future_close - entry_price) / entry_price
    future_up_mfe_pct = (future_high - entry_price) / entry_price
    future_down_mfe_pct = (entry_price - future_low) / entry_price

    if direction == 1:
        directional_mfe_pct = future_up_mfe_pct
        directional_mae_pct = future_down_mfe_pct
        directional_return_pct = future_return_pct
    elif direction == -1:
        directional_mfe_pct = future_down_mfe_pct
        directional_mae_pct = future_up_mfe_pct
        directional_return_pct = -future_return_pct
    else:
        directional_mfe_pct = np.nan
        directional_mae_pct = np.nan
        directional_return_pct = np.nan

    return {
        "future_high": future_high,
        "future_low": future_low,
        "future_close": future_close,
        "future_return_pct": future_return_pct,
        "future_up_mfe_pct": future_up_mfe_pct,
        "future_down_mfe_pct": future_down_mfe_pct,
        "directional_mfe_pct": directional_mfe_pct,
        "directional_mae_pct": directional_mae_pct,
        "directional_return_pct": directional_return_pct,
        "future_hit_0_10pct": directional_mfe_pct >= 0.001 if direction != 0 else np.nan,
        "future_hit_0_20pct": directional_mfe_pct >= 0.002 if direction != 0 else np.nan,
        "future_hit_0_50pct": directional_mfe_pct >= 0.005 if direction != 0 else np.nan,
    }


# =============================================================================
# Main scan
# =============================================================================

def run_regime_scan(df: pd.DataFrame, profiles: pd.DataFrame, args) -> pd.DataFrame:
    profile_map = profiles.set_index("session_id").to_dict("index")

    t_ns = timestamp_to_ns(df["timestamp"])
    price = df["price"].to_numpy(dtype="float64")
    qty = df["qty"].to_numpy(dtype="float64")
    notional = df["notional"].to_numpy(dtype="float64")
    side = df["aggressor_side"].to_numpy(dtype="int8")

    dataset_end_ns = int(t_ns[-1])

    rows = []

    sessions = profiles.sort_values("session_start").to_dict("records")

    if args.max_sessions:
        sessions = sessions[-args.max_sessions:]

    for s in sessions:
        session_id = s["session_id"]
        prev_session_id = (pd.Timestamp(s["session_start"]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

        if prev_session_id not in profile_map:
            continue

        prev = profile_map[prev_session_id]

        val = float(prev["previous_val"])
        vah = float(prev["previous_vah"])
        poc = float(prev["previous_poc"])
        value_width = float(prev["value_width"])
        price_step = float(prev["profile_price_step"])

        if not np.isfinite(val) or not np.isfinite(vah) or not np.isfinite(poc):
            continue

        if value_width <= 0:
            continue

        session_start = pd.Timestamp(s["session_start"])
        session_end = pd.Timestamp(s["session_end"])

        window_start = session_start
        last_allowed_end = session_end

        if not args.allow_result_cross_session:
            last_allowed_start = session_end - pd.Timedelta(
                seconds=args.evidence_seconds + args.result_seconds
            )
        else:
            last_allowed_start = pd.Timestamp(dataset_end_ns, unit="ns", tz="UTC") - pd.Timedelta(
                seconds=args.evidence_seconds + args.result_seconds
            )

        while window_start <= last_allowed_start:
            evidence_end = window_start + pd.Timedelta(seconds=args.evidence_seconds)
            result_end = evidence_end + pd.Timedelta(seconds=args.result_seconds)

            window_start_ns = int(window_start.value)
            evidence_end_ns = int(evidence_end.value)
            result_end_ns = int(result_end.value)

            if result_end_ns > dataset_end_ns:
                break

            features = compute_window_features(
                t_ns=t_ns,
                price=price,
                qty=qty,
                notional=notional,
                side=side,
                window_start_ns=window_start_ns,
                window_end_ns=evidence_end_ns,
                val=val,
                vah=vah,
                poc=poc,
                value_width=value_width,
                price_step=price_step,
                bar_seconds=args.bar_seconds,
            )

            if features is not None:
                base = {
                    "symbol": args.symbol,
                    "session_id": session_id,
                    "previous_session_id": prev_session_id,
                    "window_start": window_start,
                    "evidence_end": evidence_end,
                    "result_end": result_end,
                    "evidence_seconds": args.evidence_seconds,
                    "result_seconds": args.result_seconds,
                    "previous_val": val,
                    "previous_vah": vah,
                    "previous_poc": poc,
                    "previous_value_width": value_width,
                    "profile_price_step": price_step,
                }

                row = {**base, **features}
                label = classify_regime(row, args)
                direction = regime_direction(label)

                row["regime_label"] = label
                row["regime_direction"] = direction

                future = compute_future_result(
                    t_ns=t_ns,
                    price=price,
                    entry_price=row["close_price"],
                    result_start_ns=evidence_end_ns,
                    result_end_ns=result_end_ns,
                    direction=direction,
                )

                if future is not None:
                    row.update(future)
                    rows.append(row)

            window_start += pd.Timedelta(seconds=args.step_seconds)

    result = pd.DataFrame(rows)

    if len(result) > 0:
        result["window_start"] = pd.to_datetime(result["window_start"], utc=True)
        result["evidence_end"] = pd.to_datetime(result["evidence_end"], utc=True)
        result["result_end"] = pd.to_datetime(result["result_end"], utc=True)

    return result


# =============================================================================
# Summary and validation export
# =============================================================================

def summarize_regimes(result: pd.DataFrame) -> pd.DataFrame:
    if len(result) == 0:
        return pd.DataFrame()

    g = result.groupby("regime_label", dropna=False)

    summary = g.agg(
        sample_n=("regime_label", "size"),
        median_future_return_pct=("future_return_pct", "median"),
        mean_future_return_pct=("future_return_pct", "mean"),
        median_directional_mfe_pct=("directional_mfe_pct", "median"),
        median_directional_mae_pct=("directional_mae_pct", "median"),
        hit_0_10pct_rate=("future_hit_0_10pct", "mean"),
        hit_0_20pct_rate=("future_hit_0_20pct", "mean"),
        hit_0_50pct_rate=("future_hit_0_50pct", "mean"),
        median_range_value_width=("range_value_width", "median"),
        median_signed_efficiency=("signed_efficiency", "median"),
        median_time_inside_value_share=("time_inside_value_share", "median"),
        median_time_above_vah_share=("time_above_vah_share", "median"),
        median_time_below_val_share=("time_below_val_share", "median"),
    ).reset_index()

    summary["sample_pct"] = summary["sample_n"] / summary["sample_n"].sum()
    summary = summary.sort_values("sample_n", ascending=False)

    return summary


def export_manual_sample(result: pd.DataFrame, out_dir: Path, samples_per_regime: int, seed: int):
    if samples_per_regime <= 0 or len(result) == 0:
        return pd.DataFrame()

    sampled = (
        result
        .groupby("regime_label", group_keys=False)
        .apply(lambda x: x.sample(min(len(x), samples_per_regime), random_state=seed))
        .reset_index(drop=True)
    )

    keep_cols = [
        "symbol",
        "session_id",
        "previous_session_id",
        "window_start",
        "evidence_end",
        "result_end",
        "regime_label",
        "regime_direction",
        "previous_val",
        "previous_vah",
        "previous_poc",
        "open_price",
        "close_price",
        "high_price",
        "low_price",
        "time_above_vah_share",
        "time_below_val_share",
        "time_inside_value_share",
        "qty_above_vah_share",
        "qty_below_val_share",
        "qty_inside_value_share",
        "median_change_value_width",
        "signed_efficiency",
        "new_high_count",
        "new_low_count",
        "future_return_pct",
        "directional_mfe_pct",
        "directional_mae_pct",
        "future_hit_0_10pct",
        "future_hit_0_20pct",
        "future_hit_0_50pct",
    ]

    sampled = sampled[[c for c in keep_cols if c in sampled.columns]]

    sampled.to_csv(out_dir / "manual_validation_sample.csv", index=False)

    sampled_excel = sampled.copy()
    for col in ["window_start", "evidence_end", "result_end"]:
        if col in sampled_excel.columns:
            sampled_excel[col] = timezone_aware_to_naive_utc(sampled_excel[col])

    try:
        sampled_excel.to_excel(out_dir / "manual_validation_sample.xlsx", index=False)
    except Exception as e:
        print(f"Could not export xlsx manual sample: {e}")

    return sampled


def create_validation_charts(
    df: pd.DataFrame,
    sampled: pd.DataFrame,
    out_dir: Path,
):
    if len(sampled) == 0:
        return

    required_cols = {
        "window_start",
        "evidence_end",
        "result_end",
        "session_id",
        "regime_label",
        "previous_val",
        "previous_vah",
        "previous_poc",
    }
    missing_cols = sorted(required_cols - set(sampled.columns))
    if missing_cols:
        print(
            "Skipping manual validation charts because sampled data is missing required columns: "
            + ", ".join(missing_cols)
        )
        return

    import matplotlib.pyplot as plt

    chart_dir = out_dir / "manual_validation_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    df_plot = df[["timestamp", "price"]].copy()
    df_plot = df_plot.set_index("timestamp")

    manifest = []

    for i, row in sampled.reset_index(drop=True).iterrows():
        regime_label = row.get("regime_label", "UNKNOWN_REGIME")
        session_id = row.get("session_id", "UNKNOWN_SESSION")
        start = pd.Timestamp(row["window_start"])
        evidence_end = pd.Timestamp(row["evidence_end"])
        result_end = pd.Timestamp(row["result_end"])

        g = df_plot.loc[start:result_end]

        if len(g) == 0:
            continue

        fig, ax = plt.subplots(figsize=(14, 6))
        ax.plot(g.index, g["price"], linewidth=1)

        ax.axhline(row["previous_val"], linestyle="--", linewidth=1, label="Previous VAL")
        ax.axhline(row["previous_vah"], linestyle="--", linewidth=1, label="Previous VAH")
        ax.axhline(row["previous_poc"], linestyle=":", linewidth=1, label="Previous POC")

        ax.axvspan(start, evidence_end, alpha=0.15, label="Evidence window")
        ax.axvspan(evidence_end, result_end, alpha=0.08, label="Future result window")

        ax.set_title(
            f"{regime_label} | {session_id} | {start.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        ax.set_xlabel("Time")
        ax.set_ylabel("Price")
        ax.legend(loc="best")

        file_name = f"{i:04d}_{regime_label}_{session_id}.png".replace("/", "-")
        path = chart_dir / file_name

        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)

        manifest.append(
            {
                "chart_file": str(path),
                "regime_label": regime_label,
                "session_id": session_id,
                "window_start": row.get("window_start"),
                "evidence_end": row.get("evidence_end"),
                "result_end": row.get("result_end"),
            }
        )

    pd.DataFrame(manifest).to_csv(out_dir / "manual_validation_chart_manifest.csv", index=False)


# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser()

    p.add_argument("--input", required=True, help="Raw aggTrade parquet path")
    p.add_argument("--symbol", required=True)
    p.add_argument("--output-dir", required=True)

    p.add_argument("--session-cutoff-utc", default="13:30")

    p.add_argument("--evidence-seconds", type=int, default=900)
    p.add_argument("--result-seconds", type=int, default=3600)
    p.add_argument("--step-seconds", type=int, default=900)
    p.add_argument("--bar-seconds", type=int, default=60)

    p.add_argument("--value-area-pct", type=float, default=0.70)
    p.add_argument("--tick-size", type=float, default=None)
    p.add_argument("--profile-bin-pct", type=float, default=0.0005)

    p.add_argument("--accept-share", type=float, default=0.60)
    p.add_argument("--balance-share", type=float, default=0.60)
    p.add_argument("--min-migration-value-width", type=float, default=0.10)
    p.add_argument("--flat-migration-value-width", type=float, default=0.10)
    p.add_argument("--min-efficiency", type=float, default=0.25)
    p.add_argument("--flat-efficiency", type=float, default=0.25)
    p.add_argument("--min-new-extreme-count", type=int, default=1)
    p.add_argument("--failed-second-half-share", type=float, default=0.50)

    p.add_argument("--manual-samples-per-regime", type=int, default=20)
    p.add_argument("--chart-samples-per-regime", type=int, default=5)
    p.add_argument("--seed", type=int, default=42)

    p.add_argument("--max-sessions", type=int, default=None, help="Debug mode: only scan last N sessions")
    p.add_argument("--allow-result-cross-session", action="store_true")

    return p.parse_args()


def main():
    args = parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "config.json", "w") as f:
        json.dump(vars(args), f, indent=2, default=str)

    print("=" * 80)
    print("1. Load raw aggTrades")
    print("=" * 80)

    df = load_raw_aggtrades(Path(args.input))

    print("=" * 80)
    print("2. Add session columns")
    print("=" * 80)

    df = add_session_columns(df, cutoff_utc=args.session_cutoff_utc)

    print("=" * 80)
    print("3. Build session profiles")
    print("=" * 80)

    profiles = build_session_profiles(
        df=df,
        tick_size=args.tick_size,
        profile_bin_pct=args.profile_bin_pct,
        value_area_pct=args.value_area_pct,
    )

    profiles.to_csv(out_dir / "session_profiles.csv", index=False)
    profiles.to_parquet(out_dir / "session_profiles.parquet", index=False)

    print(f"Session profiles: {len(profiles):,}")
    print(profiles.tail(5).to_string(index=False))

    print("=" * 80)
    print("4. Run regime scan")
    print("=" * 80)

    result = run_regime_scan(df=df, profiles=profiles, args=args)

    print(f"Regime windows: {len(result):,}")

    result.to_parquet(out_dir / "regime_windows.parquet", index=False)
    result.to_csv(out_dir / "regime_windows.csv", index=False)

    print("=" * 80)
    print("5. Summary")
    print("=" * 80)

    summary = summarize_regimes(result)
    summary.to_csv(out_dir / "regime_summary.csv", index=False)

    if len(summary) > 0:
        print(summary.to_string(index=False))

    print("=" * 80)
    print("6. Manual validation sample")
    print("=" * 80)

    sampled = export_manual_sample(
        result=result,
        out_dir=out_dir,
        samples_per_regime=args.manual_samples_per_regime,
        seed=args.seed,
    )

    if len(sampled) > 0 and "regime_label" in sampled.columns:
        chart_sample = (
            sampled
            .groupby("regime_label", group_keys=False)
            .head(args.chart_samples_per_regime)
            .reset_index(drop=True)
        )
    else:
        chart_sample = sampled

    create_validation_charts(df=df, sampled=chart_sample, out_dir=out_dir)

    print("=" * 80)
    print("Done")
    print("=" * 80)
    print(f"Output dir: {out_dir}")
    print("Main files:")
    print("- session_profiles.csv")
    print("- regime_windows.parquet")
    print("- regime_windows.csv")
    print("- regime_summary.csv")
    print("- manual_validation_sample.csv")
    print("- manual_validation_sample.xlsx")
    print("- manual_validation_charts/")


if __name__ == "__main__":
    main()