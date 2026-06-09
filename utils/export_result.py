"""
Export confirmed setup events and impulse group summaries from an event-study parquet.

Usage:
    python export_result.py --input research/1535.parquet --output research/analysis_outputs/confirmed_setups_impulse_groups.xlsx

This script exports every confirmed setup row from the input parquet.
Outcome columns are kept visible for review in Excel, but they are not used to:
    - decide whether an event is exported
    - decide whether an event belongs to a group
    - decide whether a group exists
    - choose the representative event

Grouping methods:
    1. gap-only
        same session_id + same directional_bias
        new group if gap between consecutive confirmed setups > gap_seconds

    2. gap + pullback reset
        same gap rule as above, plus a new group starts when the pullback ratio
        from the current group's favorable extreme at the current event reaches
        the configured threshold, using only raw trade data up to the current event timestamp.

Output workbook sheets:
    1. Confirmed Setups
    2. Impulse Groups
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


DEFAULT_GAP_SECONDS = 900
JAKARTA_TZ = "Asia/Jakarta"
GROUPING_METHOD_GAP_ONLY = "gap_only"
GROUPING_METHOD_GAP_PULLBACK = "gap_and_pullback_reset"


PREFERRED_CONFIRMED_SETUP_COLUMNS = [
    "impulse_group_key",
    "impulse_group_number",
    "impulse_group_event_index",
    "is_group_representative",
    "grouping_method",
    "group_reset_reason",
    "gap_seconds",
    "pullback_reset_ratio",
    "previous_event_gap_seconds",
    "gap_reset_triggered",
    "pullback_ratio_at_event",
    "pullback_reset_triggered",
    "group_anchor_price",
    "group_favorable_extreme_at_event",
    "impulse_group_start_timestamp",
    "impulse_group_end_timestamp",
    "impulse_group_start_utc",
    "impulse_group_end_utc",
    "impulse_group_start_wib",
    "impulse_group_end_wib",
    "event_id",
    "event_date_utc",
    "event_month_utc",
    "event_time_utc",
    "event_date_wib",
    "event_month_wib",
    "event_time_wib",
    "event_dt_utc",
    "event_dt_wib",
    "confirmation_dt_utc",
    "confirmation_dt_wib",
    "session_id",
    "previous_session_id",
    "directional_bias",
    "location",
    "bubble_side",
    "bubble_tier",
    "event_reaction_type",
    "setup_confirmed",
    "anchor_price",
    "confirmation_price",
    "observation_anchor_price",
    "previous_val",
    "previous_vah",
    "previous_poc",
    "distance_from_val_pct",
    "distance_from_vah_pct",
    "distance_from_poc_pct",
    "reaction_mfe_30s_pct",
    "reaction_mae_30s_pct",
    "reaction_efficiency_30s",
    "mfe_3600s_pct",
    "mae_3600s_pct",
    "efficiency_3600s",
    "max_expansion_pct",
    "reached_0.25_pct",
    "reached_0.50_pct",
    "reached_1.00_pct",
    "reached_2.00_pct",
    "reached_3.00_pct",
    "seconds_to_0.05_pct",
    "seconds_to_0.10_pct",
    "seconds_to_0.20_pct",
    "seconds_to_0.50_pct",
    "anchor_bubble_qty",
    "anchor_bubble_notional",
    "bubble_percentile_score",
    "bubble_medium_qty_threshold",
    "bubble_large_qty_threshold",
    "bubble_extreme_qty_threshold",
    "event_timestamp",
    "setup_confirmation_timestamp",
    "observation_start_timestamp",
]


PREFERRED_GROUP_COLUMNS = [
    "impulse_group_key",
    "session_id",
    "directional_bias",
    "location_mode",
    "dominant_bubble_tier",
    "grouping_method",
    "gap_seconds",
    "pullback_reset_ratio",
    "group_start_utc",
    "group_end_utc",
    "group_start_wib",
    "group_end_wib",
    "group_start_date_utc",
    "group_start_month_utc",
    "group_start_time_utc",
    "group_start_date_wib",
    "group_start_month_wib",
    "group_start_time_wib",
    "group_duration_minutes",
    "event_count",
    "representative_event_id",
    "representative_event_utc",
    "representative_event_wib",
    "representative_confirmation_utc",
    "representative_confirmation_wib",
    "representative_anchor_price",
    "representative_confirmation_price",
    "first_event_price",
    "last_event_price",
    "min_anchor_price",
    "max_anchor_price",
    "median_anchor_price",
    "result_median_mfe_3600s_pct",
    "result_median_mae_3600s_pct",
    "result_median_efficiency_3600s",
    "result_max_mfe_3600s_pct",
    "result_min_mae_3600s_pct",
    "result_max_efficiency_3600s",
    "result_reached_0.50_pct_count",
    "result_reached_1.00_pct_count",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export confirmed setup events and impulse groups into a 2-sheet Excel file."
    )
    parser.add_argument("--input", required=True, help="Input event-study parquet path")
    parser.add_argument("--output", required=True, help="Output .xlsx path")
    parser.add_argument("--gap-seconds", type=int, default=DEFAULT_GAP_SECONDS, help="Gap threshold in seconds for impulse grouping.")
    parser.add_argument("--raw-trades", default=None, help="Optional raw trade parquet/csv path for pullback-reset grouping.")
    parser.add_argument("--pullback-reset-ratio", type=float, default=None, help="If provided together with --raw-trades, use gap + pullback reset grouping.")
    args = parser.parse_args()

    if args.gap_seconds <= 0:
        raise ValueError("--gap-seconds must be > 0")
    if args.pullback_reset_ratio is not None and not (0 < args.pullback_reset_ratio <= 1):
        raise ValueError("--pullback-reset-ratio must be > 0 and <= 1")
    if args.pullback_reset_ratio is not None and not args.raw_trades:
        raise ValueError("--pullback-reset-ratio requires --raw-trades")
    if args.raw_trades and args.pullback_reset_ratio is None:
        raise ValueError("--raw-trades requires --pullback-reset-ratio to avoid grouping ambiguity")

    return args


def require_columns(df: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError(f"Input data is missing required columns: {missing}")


def mode_or_none(series: pd.Series):
    mode = series.dropna().mode()
    return mode.iloc[0] if len(mode) else None


def event_value(row: pd.Series, column: str):
    return row[column] if column in row.index else None


def choose_event_price(row: pd.Series):
    confirmation_price = row.get("confirmation_price")
    if pd.notna(confirmation_price):
        return confirmation_price
    anchor_price = row.get("anchor_price")
    if pd.notna(anchor_price):
        return anchor_price
    return np.nan


def add_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    require_columns(out, ["event_timestamp"])
    out["event_dt_utc"] = pd.to_datetime(out["event_timestamp"], unit="ms", utc=True)
    out["event_dt_wib"] = out["event_dt_utc"].dt.tz_convert(JAKARTA_TZ)

    out["event_date_utc"] = out["event_dt_utc"].dt.date.astype(str)
    out["event_month_utc"] = out["event_dt_utc"].dt.strftime("%Y-%m")
    out["event_time_utc"] = out["event_dt_utc"].dt.strftime("%H:%M:%S.%f").str[:-3]
    out["event_date_wib"] = out["event_dt_wib"].dt.date.astype(str)
    out["event_month_wib"] = out["event_dt_wib"].dt.strftime("%Y-%m")
    out["event_time_wib"] = out["event_dt_wib"].dt.strftime("%H:%M:%S.%f").str[:-3]

    if "setup_confirmation_timestamp" in out.columns:
        out["confirmation_dt_utc"] = pd.to_datetime(out["setup_confirmation_timestamp"], unit="ms", utc=True)
        out["confirmation_dt_wib"] = out["confirmation_dt_utc"].dt.tz_convert(JAKARTA_TZ)
    else:
        out["confirmation_dt_utc"] = pd.NaT
        out["confirmation_dt_wib"] = pd.NaT

    return out


def load_raw_trades(raw_trades_path: str | None) -> tuple[np.ndarray, np.ndarray] | None:
    if raw_trades_path is None:
        return None

    path = Path(raw_trades_path)
    if not path.exists():
        raise FileNotFoundError(f"Raw trades file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".parquet":
        raw_df = pd.read_parquet(path)
    elif suffix == ".csv":
        raw_df = pd.read_csv(path)
    else:
        raise ValueError("--raw-trades must be a parquet or csv file")

    require_columns(raw_df, ["timestamp", "price"])
    raw_df = raw_df[["timestamp", "price"]].copy()

    if pd.api.types.is_datetime64_any_dtype(raw_df["timestamp"]):
        ts = raw_df["timestamp"]
        if getattr(ts.dt, "tz", None) is None:
            ts = ts.dt.tz_localize("UTC")
        else:
            ts = ts.dt.tz_convert("UTC")
        raw_df["timestamp"] = (ts.astype("int64") // 10**6).astype("int64")
    elif pd.api.types.is_numeric_dtype(raw_df["timestamp"]):
        raw_df["timestamp"] = pd.to_numeric(raw_df["timestamp"], errors="raise").astype("int64")
    else:
        parsed = pd.to_datetime(raw_df["timestamp"], utc=True)
        raw_df["timestamp"] = (parsed.astype("int64") // 10**6).astype("int64")

    raw_df["price"] = pd.to_numeric(raw_df["price"], errors="coerce")
    raw_df = raw_df.dropna(subset=["timestamp", "price"]).sort_values("timestamp").reset_index(drop=True)
    return raw_df["timestamp"].to_numpy(dtype=np.int64), raw_df["price"].to_numpy(dtype=float)


def compute_pullback_state(direction: str, group_anchor_price: float, current_price: float, raw_timestamps: np.ndarray, raw_prices: np.ndarray, group_start_timestamp: int, current_timestamp: int) -> tuple[float | None, float | None, bool]:
    left = np.searchsorted(raw_timestamps, group_start_timestamp, side="left")
    right = np.searchsorted(raw_timestamps, current_timestamp, side="right")
    if right <= left:
        return None, None, False

    price_slice = raw_prices[left:right]
    if price_slice.size == 0 or pd.isna(group_anchor_price) or pd.isna(current_price):
        return None, None, False

    direction_normalized = str(direction).strip().lower()
    if direction_normalized in {"long", "bull", "bullish", "buy", "up"}:
        favorable_extreme = float(np.max(price_slice))
        group_move = favorable_extreme - float(group_anchor_price)
        pullback = favorable_extreme - float(current_price)
    else:
        favorable_extreme = float(np.min(price_slice))
        group_move = float(group_anchor_price) - favorable_extreme
        pullback = float(current_price) - favorable_extreme

    pullback_ratio = float(pullback / group_move) if group_move > 0 else 0.0
    return pullback_ratio, favorable_extreme, True


def create_confirmed_setups(df: pd.DataFrame, gap_seconds: int, raw_trade_data: tuple[np.ndarray, np.ndarray] | None, pullback_reset_ratio: float | None) -> pd.DataFrame:
    require_columns(df, ["session_id", "directional_bias", "event_timestamp"])
    out = add_datetime_columns(df)

    sort_columns = ["session_id", "directional_bias", "event_timestamp"]
    if "event_id" in out.columns:
        sort_columns.append("event_id")
    out = out.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)

    grouping_method = GROUPING_METHOD_GAP_PULLBACK if raw_trade_data is not None and pullback_reset_ratio is not None else GROUPING_METHOD_GAP_ONLY
    out["grouping_method"] = grouping_method
    out["gap_seconds"] = gap_seconds
    out["pullback_reset_ratio"] = pullback_reset_ratio
    out["previous_event_gap_seconds"] = np.nan
    out["gap_reset_triggered"] = False
    out["pullback_ratio_at_event"] = np.nan
    out["pullback_reset_triggered"] = False
    out["group_anchor_price"] = np.nan
    out["group_favorable_extreme_at_event"] = np.nan
    out["group_reset_reason"] = None
    out["impulse_group_number"] = 0
    out["impulse_group_event_index"] = 0
    out["impulse_group_key"] = None
    out["is_group_representative"] = False

    raw_timestamps = raw_prices = None
    if raw_trade_data is not None:
        raw_timestamps, raw_prices = raw_trade_data

    processed_groups: list[pd.DataFrame] = []
    for (session_id, directional_bias), subgroup in out.groupby(["session_id", "directional_bias"], observed=True, sort=False):
        subgroup = subgroup.copy()
        prev_timestamp: int | None = None
        current_group_number = 0
        current_group_start_timestamp: int | None = None
        current_group_anchor_price: float = np.nan
        current_group_event_index = 0

        for idx in subgroup.index:
            event_timestamp = int(subgroup.at[idx, "event_timestamp"])
            event_price = choose_event_price(subgroup.loc[idx])
            previous_gap_seconds = np.nan if prev_timestamp is None else (event_timestamp - prev_timestamp) / 1000.0
            gap_reset = prev_timestamp is None or (pd.notna(previous_gap_seconds) and previous_gap_seconds > gap_seconds)

            pullback_triggered = False
            pullback_ratio_at_event = np.nan
            favorable_extreme_at_event = np.nan
            if not gap_reset and raw_timestamps is not None and raw_prices is not None and pullback_reset_ratio is not None and current_group_start_timestamp is not None:
                pullback_ratio, favorable_extreme, has_slice = compute_pullback_state(
                    directional_bias,
                    current_group_anchor_price,
                    event_price,
                    raw_timestamps,
                    raw_prices,
                    current_group_start_timestamp,
                    event_timestamp,
                )
                if has_slice:
                    if pullback_ratio is not None:
                        pullback_ratio_at_event = pullback_ratio
                    if favorable_extreme is not None:
                        favorable_extreme_at_event = favorable_extreme
                    pullback_triggered = bool(pullback_ratio is not None and pullback_ratio >= pullback_reset_ratio)

            start_new_group = gap_reset or pullback_triggered
            if start_new_group:
                current_group_number += 1
                current_group_event_index = 1
                current_group_start_timestamp = event_timestamp
                current_group_anchor_price = event_price
            else:
                current_group_event_index += 1

            if prev_timestamp is None:
                reset_reason = "first_event"
            elif gap_reset and pullback_triggered:
                reset_reason = "gap_and_pullback"
            elif gap_reset:
                reset_reason = "gap"
            elif pullback_triggered:
                reset_reason = "pullback"
            else:
                reset_reason = "same_group"

            subgroup.at[idx, "previous_event_gap_seconds"] = previous_gap_seconds
            subgroup.at[idx, "gap_reset_triggered"] = bool(prev_timestamp is not None and gap_reset)
            subgroup.at[idx, "pullback_ratio_at_event"] = pullback_ratio_at_event
            subgroup.at[idx, "pullback_reset_triggered"] = pullback_triggered
            subgroup.at[idx, "group_anchor_price"] = current_group_anchor_price
            subgroup.at[idx, "group_favorable_extreme_at_event"] = favorable_extreme_at_event
            subgroup.at[idx, "group_reset_reason"] = reset_reason
            subgroup.at[idx, "impulse_group_number"] = current_group_number
            subgroup.at[idx, "impulse_group_event_index"] = current_group_event_index
            subgroup.at[idx, "impulse_group_key"] = f"{session_id}_{directional_bias}_{current_group_number}"
            subgroup.at[idx, "is_group_representative"] = current_group_event_index == 1
            prev_timestamp = event_timestamp

        processed_groups.append(subgroup)

    setups = pd.concat(processed_groups, ignore_index=True) if processed_groups else out.copy()
    group_bounds = setups.groupby("impulse_group_key", observed=True).agg(
        impulse_group_start_timestamp=("event_timestamp", "min"),
        impulse_group_end_timestamp=("event_timestamp", "max"),
    )
    setups = setups.merge(group_bounds, left_on="impulse_group_key", right_index=True, how="left")
    setups["impulse_group_start_utc"] = pd.to_datetime(setups["impulse_group_start_timestamp"], unit="ms", utc=True)
    setups["impulse_group_end_utc"] = pd.to_datetime(setups["impulse_group_end_timestamp"], unit="ms", utc=True)
    setups["impulse_group_start_wib"] = setups["impulse_group_start_utc"].dt.tz_convert(JAKARTA_TZ)
    setups["impulse_group_end_wib"] = setups["impulse_group_end_utc"].dt.tz_convert(JAKARTA_TZ)
    return setups


def build_group_summary(setups: pd.DataFrame) -> pd.DataFrame:
    if setups.empty:
        return pd.DataFrame(columns=PREFERRED_GROUP_COLUMNS)

    records: list[dict] = []
    for group_key, group in setups.groupby("impulse_group_key", observed=True, sort=True):
        group = group.sort_values(["event_timestamp", "event_id"] if "event_id" in group.columns else ["event_timestamp"], kind="mergesort")
        representative = group.iloc[0]
        group_start = pd.to_datetime(group["event_timestamp"].min(), unit="ms", utc=True)
        group_end = pd.to_datetime(group["event_timestamp"].max(), unit="ms", utc=True)
        group_start_wib = group_start.tz_convert(JAKARTA_TZ)

        record = {
            "impulse_group_key": group_key,
            "session_id": event_value(representative, "session_id"),
            "directional_bias": event_value(representative, "directional_bias"),
            "location_mode": mode_or_none(group["location"]) if "location" in group.columns else None,
            "dominant_bubble_tier": mode_or_none(group["bubble_tier"]) if "bubble_tier" in group.columns else None,
            "grouping_method": event_value(representative, "grouping_method"),
            "gap_seconds": event_value(representative, "gap_seconds"),
            "pullback_reset_ratio": event_value(representative, "pullback_reset_ratio"),
            "group_start_utc": group_start,
            "group_end_utc": group_end,
            "group_start_wib": group_start_wib,
            "group_end_wib": group_end.tz_convert(JAKARTA_TZ),
            "group_start_date_utc": group_start.date().isoformat(),
            "group_start_month_utc": group_start.strftime("%Y-%m"),
            "group_start_time_utc": group_start.strftime("%H:%M:%S.%f")[:-3],
            "group_start_date_wib": group_start_wib.date().isoformat(),
            "group_start_month_wib": group_start_wib.strftime("%Y-%m"),
            "group_start_time_wib": group_start_wib.strftime("%H:%M:%S.%f")[:-3],
            "group_duration_minutes": (group["event_timestamp"].max() - group["event_timestamp"].min()) / 1000 / 60,
            "event_count": int(len(group)),
            "representative_event_id": event_value(representative, "event_id"),
            "representative_event_utc": event_value(representative, "event_dt_utc"),
            "representative_event_wib": event_value(representative, "event_dt_wib"),
            "representative_confirmation_utc": event_value(representative, "confirmation_dt_utc"),
            "representative_confirmation_wib": event_value(representative, "confirmation_dt_wib"),
            "representative_anchor_price": event_value(representative, "anchor_price"),
            "representative_confirmation_price": event_value(representative, "confirmation_price"),
            "first_event_price": choose_event_price(group.iloc[0]),
            "last_event_price": choose_event_price(group.iloc[-1]),
            "min_anchor_price": group["anchor_price"].min() if "anchor_price" in group.columns else None,
            "max_anchor_price": group["anchor_price"].max() if "anchor_price" in group.columns else None,
            "median_anchor_price": group["anchor_price"].median() if "anchor_price" in group.columns else None,
            "result_median_mfe_3600s_pct": group["mfe_3600s_pct"].median() if "mfe_3600s_pct" in group.columns else None,
            "result_median_mae_3600s_pct": group["mae_3600s_pct"].median() if "mae_3600s_pct" in group.columns else None,
            "result_median_efficiency_3600s": group["efficiency_3600s"].median() if "efficiency_3600s" in group.columns else None,
            "result_max_mfe_3600s_pct": group["mfe_3600s_pct"].max() if "mfe_3600s_pct" in group.columns else None,
            "result_min_mae_3600s_pct": group["mae_3600s_pct"].min() if "mae_3600s_pct" in group.columns else None,
            "result_max_efficiency_3600s": group["efficiency_3600s"].max() if "efficiency_3600s" in group.columns else None,
            "result_reached_0.50_pct_count": int(group["reached_0.50_pct"].fillna(False).astype(bool).sum()) if "reached_0.50_pct" in group.columns else None,
            "result_reached_1.00_pct_count": int(group["reached_1.00_pct"].fillna(False).astype(bool).sum()) if "reached_1.00_pct" in group.columns else None,
        }
        records.append(record)

    summary = pd.DataFrame(records)
    return summary.sort_values(["session_id", "directional_bias", "group_start_utc"]).reset_index(drop=True)


def validate_grouping(input_df: pd.DataFrame, setups: pd.DataFrame, groups: pd.DataFrame) -> None:
    if len(setups) != len(input_df):
        raise ValueError(f"Exported confirmed setup count mismatch: input={len(input_df):,}, exported={len(setups):,}")
    if setups["impulse_group_key"].isna().any():
        raise ValueError("Every confirmed setup row must have impulse_group_key")

    event_index_min = setups.groupby("impulse_group_key", observed=True)["impulse_group_event_index"].min()
    if not (event_index_min == 1).all():
        raise ValueError("impulse_group_event_index must start from 1 within each group")

    representative_count = setups.groupby("impulse_group_key", observed=True)["is_group_representative"].sum()
    if not (representative_count == 1).all():
        raise ValueError("Each impulse_group_key must have exactly one representative event")

    representative_timestamps = (
        setups.loc[
            setups["is_group_representative"],
            ["impulse_group_key", "event_timestamp"],
        ]
        .set_index("impulse_group_key")["event_timestamp"]
        .sort_index()
    )

    earliest_timestamps = (
        setups.groupby("impulse_group_key", observed=True)["event_timestamp"]
        .min()
        .sort_index()
    )

    representative_timestamps, earliest_timestamps = representative_timestamps.align(
        earliest_timestamps,
        join="outer",
    )

    bad_representatives = representative_timestamps.ne(earliest_timestamps)

    if bad_representatives.any():
        mismatch = pd.DataFrame(
            {
                "representative_timestamp": representative_timestamps[bad_representatives],
                "earliest_timestamp": earliest_timestamps[bad_representatives],
            }
        )

        raise ValueError(
            "Representative event must be earliest event_timestamp in each group. "
            f"Mismatch: {mismatch.head(10).to_dict(orient='index')}"
        )
    
    group_counts_from_rows = (
        setups.groupby("impulse_group_key", observed=True)
        .size()
        .rename("row_count")
        .sort_index()
    )

    group_counts_from_summary = (
        groups.set_index("impulse_group_key")["event_count"]
        .rename("event_count")
        .sort_index()
    )

    group_counts_from_rows, group_counts_from_summary = group_counts_from_rows.align(
        group_counts_from_summary,
        join="outer",
    )

    bad_group_counts = group_counts_from_rows.ne(group_counts_from_summary)

    if bad_group_counts.any():
        mismatch = pd.DataFrame(
            {
                "row_count": group_counts_from_rows[bad_group_counts],
                "event_count": group_counts_from_summary[bad_group_counts],
            }
        )

        raise ValueError(
            "Group event_count mismatch detected. "
            f"Mismatch: {mismatch.head(10).to_dict(orient='index')}"
        )

def reorder_columns(df: pd.DataFrame, preferred: list[str]) -> pd.DataFrame:
    existing_preferred = [column for column in preferred if column in df.columns]
    remaining = [column for column in df.columns if column not in existing_preferred]
    return df[existing_preferred + remaining]


def prepare_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for column in out.columns:
        dtype = out[column].dtype
        if isinstance(dtype, pd.CategoricalDtype) or isinstance(dtype, pd.IntervalDtype):
            out[column] = out[column].astype(str)
            continue
        if pd.api.types.is_datetime64_any_dtype(dtype):
            out[column] = out[column].astype(str)
    return out


def write_excel(setups: pd.DataFrame, groups: pd.DataFrame, output_path: str) -> None:
    output = Path(output_path)
    if output.suffix.lower() != ".xlsx":
        raise ValueError("--output must end with .xlsx")
    if output.parent:
        output.parent.mkdir(parents=True, exist_ok=True)

    setups_export = prepare_for_excel(reorder_columns(setups, PREFERRED_CONFIRMED_SETUP_COLUMNS))
    groups_export = prepare_for_excel(reorder_columns(groups, PREFERRED_GROUP_COLUMNS))

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        setups_export.to_excel(writer, sheet_name="Confirmed Setups", index=False)
        groups_export.to_excel(writer, sheet_name="Impulse Groups", index=False)

        for sheet_name, data in [("Confirmed Setups", setups_export), ("Impulse Groups", groups_export)]:
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for idx, column in enumerate(data.columns, start=1):
                max_len = max([len(str(column))] + [len(str(value)) for value in data[column].head(200).fillna("").values])
                worksheet.column_dimensions[worksheet.cell(row=1, column=idx).column_letter].width = min(max_len + 2, 45)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input parquet not found: {input_path}")

    df = pd.read_parquet(input_path)
    raw_trade_data = load_raw_trades(args.raw_trades)
    setups = create_confirmed_setups(df, args.gap_seconds, raw_trade_data, args.pullback_reset_ratio)
    groups = build_group_summary(setups)
    validate_grouping(df, setups, groups)
    write_excel(setups, groups, args.output)

    grouping_method = GROUPING_METHOD_GAP_PULLBACK if raw_trade_data is not None else GROUPING_METHOD_GAP_ONLY
    print(f"Input confirmed setup rows: {len(df):,}")
    print(f"Exported confirmed setup rows: {len(setups):,}")
    print(f"Impulse groups exported: {len(groups):,}")
    print(f"Grouping method used: {grouping_method}")
    print(f"Gap seconds: {args.gap_seconds}")
    if args.pullback_reset_ratio is not None:
        print(f"Pullback reset ratio: {args.pullback_reset_ratio}")
    if args.raw_trades is not None:
        print(f"Raw trade path: {os.path.abspath(args.raw_trades)}")
    print(f"Output path: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()