"""Causal feature snapshots and stop-first opportunity audit for daily_profile_1h.

This is research only. It does not change signals, fills, stops, or the backtest.
Minute-candle path results are bounded estimates, not raw aggTrade replay.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .strategy import NY, PROFILE_WINDOWS, clock_ms, read_rows

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
PATH_COLUMNS = ("timestamp_ms", "open", "high", "low", "close", "volume", "delta")


from trading_core.indicators import _finite, hourly_indicators, trend_indicators_15m, trend_snapshot_15m


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(_finite(row), allow_nan=False, sort_keys=True) + "\n"
                            for row in rows), encoding="utf-8")


def load_minutes(paths: list[Path]) -> pd.DataFrame:
    """Load only required minute fields; overlapping caches must agree exactly."""
    if not paths:
        raise ValueError("At least one minute candle cache is required")
    frames = []
    for path in paths:
        table = pq.read_table(path, columns=list(PATH_COLUMNS))
        frames.append(table.to_pandas())
    frame = pd.concat(frames, ignore_index=True).sort_values("timestamp_ms", kind="stable")
    duplicate = frame.duplicated("timestamp_ms", keep=False)
    if duplicate.any():
        for _, group in frame.loc[duplicate].groupby("timestamp_ms", sort=False):
            if not group[list(PATH_COLUMNS[1:])].eq(group.iloc[0][list(PATH_COLUMNS[1:])]).all().all():
                raise ValueError(f"Conflicting minute candles at {group.iloc[0]['timestamp_ms']}")
        frame = frame.drop_duplicates("timestamp_ms")
    frame = frame.reset_index(drop=True)
    if frame.empty or (frame["timestamp_ms"].diff().dropna() < MINUTE_MS).any():
        raise ValueError("Minute cache is empty, duplicated, or out of order")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Nonpositive cached prices")
    if (frame["volume"] < 0).any() or not np.isfinite(frame[list(PATH_COLUMNS[1:])].to_numpy()).all():
        raise ValueError("Invalid cached minute values")
    return frame


def _minute_slice(minutes: pd.DataFrame, stamps: np.ndarray, start: int, end: int) -> pd.DataFrame:
    left = int(np.searchsorted(stamps, start, side="left"))
    right = int(np.searchsorted(stamps, end, side="left"))
    return minutes.iloc[left:right]


def _window_features(minutes: pd.DataFrame, stamps: np.ndarray, as_of_ms: int,
                     duration_ms: int) -> dict:
    current = _minute_slice(minutes, stamps, as_of_ms-duration_ms, as_of_ms)
    expected = duration_ms // MINUTE_MS
    result = {"coverage": len(current)/expected, "volume": None, "delta": None,
              "delta_imbalance": None, "return_pct": None, "efficiency": None,
              "relative_volume_20_blocks": None, "delta_price_agreement": None}
    if len(current) < expected * .8:
        return result
    volume = float(current["volume"].sum())
    delta = float(current["delta"].sum())
    opening = float(current.iloc[0]["open"])
    ending = float(current.iloc[-1]["close"])
    travel = abs(float(current.iloc[0]["close"])-opening) + float(current["close"].diff().abs().sum())
    history = []
    for block in range(1, 21):
        older = _minute_slice(minutes, stamps, as_of_ms-(block+1)*duration_ms,
                              as_of_ms-block*duration_ms)
        if len(older) >= expected*.8:
            history.append(float(older["volume"].sum()))
    baseline = float(np.median(history)) if len(history) >= 10 else 0.0
    result.update(volume=volume, delta=delta, delta_imbalance=delta/volume if volume else None,
                  return_pct=100*(ending/opening-1),
                  efficiency=abs(ending-opening)/travel if travel else 0.0,
                  relative_volume_20_blocks=volume/baseline if baseline > 0 else None,
                  delta_price_agreement=(ending-opening)*delta > 0)
    return result


def feature_snapshot(minutes: pd.DataFrame, stamps: np.ndarray, hourly: pd.DataFrame,
                     as_of_ms: int, profile: dict) -> dict:
    """Every input uses minutes with start < as_of_ms and hours with end <= it."""
    day = date.fromisoformat(profile["session_day"])
    completed_cutoff = as_of_ms//MINUTE_MS*MINUTE_MS
    past_end = int(np.searchsorted(stamps, completed_cutoff, side="left"))
    if past_end == 0:
        return {"feature_as_of_ms": as_of_ms, "last_completed_minute_cutoff_ms": completed_cutoff,
                "feature_available": False}
    price = float(minutes.iloc[past_end-1]["close"])
    hour_ends = hourly["end_ms"].to_numpy(dtype=np.int64)
    hour_pos = int(np.searchsorted(hour_ends, as_of_ms, side="right")) - 1
    hour = hourly.iloc[hour_pos] if hour_pos >= 0 else None
    if hour is not None and int(hour["end_ms"]) > as_of_ms:
        raise ValueError("Hourly feature would use an unfinished future bar")
    atr = float(hour["atr14"]) if hour is not None else float("nan")
    prior = _minute_slice(minutes, stamps, completed_cutoff-24*HOUR_MS, completed_cutoff)
    ny = _minute_slice(minutes, stamps, clock_ms(day, 9), completed_cutoff)

    def approximate_vwap(bars: pd.DataFrame):
        vol = float(bars["volume"].sum())
        if vol <= 0:
            return None
        typical = (bars["high"]+bars["low"]+bars["close"])/3
        return float((typical*bars["volume"]).sum()/vol)

    one_hour = _window_features(minutes, stamps, completed_cutoff, HOUR_MS)
    three_hours = _window_features(minutes, stamps, completed_cutoff, 3*HOUR_MS)
    val, poc, vah = (float(profile[key]) for key in ("val", "poc", "vah"))
    vwap_prior = approximate_vwap(prior)
    vwap_ny = approximate_vwap(ny)
    return dict(feature_as_of_ms=as_of_ms, last_completed_minute_cutoff_ms=completed_cutoff,
                feature_available=True, price=price,
                location=("above_vah" if price > vah else "below_val" if price < val else "inside_value"),
                distance_to_vah_pct=100*(price/vah-1),
                distance_to_val_pct=100*(price/val-1),
                distance_to_poc_pct=100*(price/poc-1),
                value_width_pct=100*(vah/val-1),
                ema20_gap_pct=100*(price/float(hour["ema20"])-1) if hour is not None else None,
                ema200_gap_pct=100*(price/float(hour["ema200"])-1) if hour is not None else None,
                atr14_1h=atr, atr14_pct=100*atr/price,
                adx14_1h=float(hour["adx14"]) if hour is not None else None,
                realized_volatility_20h_pct=(float(hour["realized_volatility_20h_pct"])
                                              if hour is not None else None),
                atr14_to_20h_median=(float(hour["atr14_to_20h_median"])
                                     if hour is not None else None),
                last_completed_hour_end_ms=(int(hour["end_ms"]) if hour is not None else None),
                last_completed_hour_coverage=(float(hour["minute_count"])/60 if hour is not None else None),
                vwap_prior24h_bar_approx=vwap_prior,
                vwap_ny_bar_approx=vwap_ny,
                vwap_prior24h_gap_pct=100*(price/vwap_prior-1) if vwap_prior else None,
                vwap_ny_gap_pct=100*(price/vwap_ny-1) if vwap_ny else None,
                prior24h_minute_coverage=len(prior)/(24*60),
                ny_minute_coverage=(len(ny)/((completed_cutoff-clock_ms(day, 9))//MINUTE_MS)
                                    if completed_cutoff > clock_ms(day, 9) else None),
                one_hour=one_hour, three_hours=three_hours,
                price_delta_agreement_3h=(None if (three_hours["return_pct"] is None or
                                                 three_hours["delta_imbalance"] is None)
                                          else three_hours["return_pct"]*three_hours["delta_imbalance"] > 0))


def stop_first_path(minutes: pd.DataFrame, stamps: np.ndarray, *, entry_ms: int,
                    entry_price: float, direction: str, stop: float,
                    force_exit_ms: int, known_stop_ms: int | None = None,
                    atr_at_entry: float | None = None, infer_stop: bool = True,
                    entry_at_minute_open: bool = False) -> dict:
    """Conservative/optimistic minute-bar bounds; do not invent intraminute order."""
    if direction not in ("long", "short") or entry_price <= 0 or stop <= 0:
        raise ValueError("Invalid path reference")
    side = 1 if direction == "long" else -1
    if side*(entry_price-stop) <= 0:
        return {"status": "invalid_stop_geometry"}
    if not entry_ms < force_exit_ms:
        raise ValueError("Entry must precede forced exit")
    path = _minute_slice(minutes, stamps, entry_ms//MINUTE_MS*MINUTE_MS, force_exit_ms)
    if path.empty:
        return {"status": "missing_path"}
    stop_minute = None
    if known_stop_ms is not None:
        if known_stop_ms < entry_ms:
            raise ValueError("Known stop predates entry")
        stop_minute = known_stop_ms//MINUTE_MS*MINUTE_MS
    elif infer_stop:
        touched = path["low"].to_numpy() <= stop if side == 1 else path["high"].to_numpy() >= stop
        if touched.any():
            stop_minute = int(path.iloc[int(np.flatnonzero(touched)[0])]["timestamp_ms"])
    if stop_minute is not None and stop_minute < int(path.iloc[0]["timestamp_ms"]):
        raise ValueError("Known stop predates entry path")
    if stop_minute is not None and stop_minute > int(path.iloc[-1]["timestamp_ms"]):
        return {"status": "missing_stop_minute"}
    if stop_minute is None and int(stamps[-1])+MINUTE_MS < force_exit_ms:
        return {"status": "incomplete_horizon"}
    entry_minute = entry_ms//MINUTE_MS*MINUTE_MS
    # A fully completed minute strictly after entry and strictly before stop has known order.
    safe = path[(path["timestamp_ms"] >= entry_minute if entry_at_minute_open
                 else path["timestamp_ms"] > entry_minute) &
                ((path["timestamp_ms"] < stop_minute) if stop_minute is not None else True)]
    # The entry and stop minutes can contain a high/low on either side of the event.
    possible = path[path["timestamp_ms"] <= stop_minute] if stop_minute is not None else path
    extreme = "high" if side == 1 else "low"
    favorable = lambda bars: (float(bars[extreme].max()) if side == 1
                               else float(bars[extreme].min())) if len(bars) else entry_price
    lower = max(0.0, side*(favorable(safe)-entry_price))
    upper = max(lower, side*(favorable(possible)-entry_price), 0.0)
    risk = abs(entry_price-stop)
    return dict(status="stop_touched" if stop_minute is not None else "time_exit",
                stop_minute_ms=stop_minute, entry_minute_ambiguous=entry_ms % MINUTE_MS != 0,
                intraminute_order_ambiguous=upper > lower,
                mfe_before_stop_lower_pct=100*lower/entry_price,
                mfe_before_stop_upper_pct=100*upper/entry_price,
                mfe_before_stop_lower_r=lower/risk,
                mfe_before_stop_upper_r=upper/risk,
                mfe_before_stop_lower_atr=(lower/atr_at_entry if atr_at_entry and atr_at_entry > 0 else None),
                mfe_before_stop_upper_atr=(upper/atr_at_entry if atr_at_entry and atr_at_entry > 0 else None))


def session_excursion(minutes: pd.DataFrame, stamps: np.ndarray, day: date,
                      atr_at_open: float | None) -> dict:
    start, end = clock_ms(day, 9), clock_ms(day+timedelta(days=1), 8, 59)
    path = _minute_slice(minutes, stamps, start, end)
    expected = (end-start)//MINUTE_MS
    if path.empty:
        return {"status": "missing_session_data", "coverage": 0.0}
    if int(stamps[-1]) + MINUTE_MS < end:
        return {"status": "incomplete_horizon", "coverage": len(path)/expected}
    anchor = float(path.iloc[0]["open"])
    up = max(0.0, float(path["high"].max())-anchor)
    down = max(0.0, anchor-float(path["low"].min()))
    return dict(status="complete", anchor_price=anchor,
                anchor_minute_ms=int(path.iloc[0]["timestamp_ms"]),
                first_minute_delay_ms=int(path.iloc[0]["timestamp_ms"])-start,
                coverage=len(path)/expected, max_up_pct=100*up/anchor,
                max_down_pct=100*down/anchor,
                max_up_atr=up/atr_at_open if atr_at_open and atr_at_open > 0 else None,
                max_down_atr=down/atr_at_open if atr_at_open and atr_at_open > 0 else None,
                max_up_minute_ms=int(path.loc[path["high"].idxmax(), "timestamp_ms"]),
                max_down_minute_ms=int(path.loc[path["low"].idxmin(), "timestamp_ms"]))


def first_edge_test(profile: dict, bars: list[dict]) -> dict | None:
    """First completed 15m close outside value, independent of delta confirmation."""
    day = date.fromisoformat(profile["session_day"])
    start, cutoff = clock_ms(day, 9), clock_ms(day, 12)
    for bar in sorted(bars, key=lambda b: int(b["open_timestamp_ms"])):
        if not start <= int(bar["open_timestamp_ms"]) < cutoff:
            continue
        signal_ms = int(bar["close_timestamp_ms"])
        if signal_ms > cutoff:
            continue
        close = float(bar["close"])
        direction = ("long" if close > float(profile["vah"]) else
                     "short" if close < float(profile["val"]) else None)
        if direction:
            return dict(direction=direction, signal_timestamp_ms=signal_ms,
                        signal_close=close)
    return None


def _indexed_rows(path: Path, field: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = read_rows(path)
    result = {}
    for row in rows:
        key = str(row[field])
        if key in result:
            raise ValueError(f"Duplicate {field} {key} in {path}")
        result[key] = row
    return result


def run(*, observation_dir: Path, backtest_dir: Path, candle_caches: list[Path],
        start_day: date, end_day: date, output_dir: Path) -> dict:
    if end_day < start_day:
        raise ValueError("end-date precedes start-date")
    minutes = load_minutes(candle_caches)
    stamps = minutes["timestamp_ms"].to_numpy(dtype=np.int64)
    hourly = hourly_indicators(minutes)
    profiles = read_rows(observation_dir/"prepared_profiles.jsonl")
    bars = read_rows(observation_dir/"prepared_bars_15m.jsonl")
    by_day_bars: dict[str, list[dict]] = {}
    for bar in bars:
        day = str(datetime.fromtimestamp(int(bar["open_timestamp_ms"])/1000, NY).date())
        by_day_bars.setdefault(day, []).append(bar)
    profile_map = {(p["session_day"], p["profile_window"]): p for p in profiles}
    if len(profile_map) != len(profiles):
        raise ValueError("Duplicate profile day/window")
    signal_maps = {window: _indexed_rows(observation_dir/window/"signals.jsonl", "session_day")
                   for window in PROFILE_WINDOWS}
    trade_maps = {window: _indexed_rows(backtest_dir/window/"trades.jsonl", "session_day")
                  for window in PROFILE_WINDOWS}
    session_rows, setup_rows, feature_rows = [], [], []
    for offset in range((end_day-start_day).days+1):
        day = start_day+timedelta(days=offset)
        day_text = str(day)
        open_ms = clock_ms(day, 9)
        profile_for_open = next((profile_map[(day_text, w)] for w in PROFILE_WINDOWS
                                 if (day_text, w) in profile_map), None)
        open_features = (feature_snapshot(minutes, stamps, hourly, open_ms, profile_for_open)
                         if profile_for_open else None)
        atr_open = open_features.get("atr14_1h") if open_features else None
        session_rows.append(dict(session_day=day_text, **session_excursion(minutes, stamps, day, atr_open),
                                 any_profile_available=profile_for_open is not None))
        for window in PROFILE_WINDOWS:
            profile = profile_map.get((day_text, window))
            signal = signal_maps[window].get(day_text)
            trade = trade_maps[window].get(day_text)
            if not profile:
                setup_rows.append(dict(session_day=day_text, profile_window=window,
                                       actual_status="missing_profile", shadow_status="missing_profile"))
                continue
            snapshot_open = feature_snapshot(minutes, stamps, hourly, open_ms, profile)
            feature_rows.append(dict(session_day=day_text, profile_window=window,
                                     snapshot="at_0900", **snapshot_open))
            actual_status = "executed" if trade else "signal_not_filled" if signal else "no_signal"
            row = dict(session_day=day_text, profile_window=window, actual_status=actual_status,
                       actual_sample_id=signal.get("sample_id") if signal else None,
                       shadow_status=None, first_edge_direction=None)
            if signal:
                feature_rows.append(dict(session_day=day_text, profile_window=window,
                                         snapshot="at_actual_signal",
                                         **feature_snapshot(minutes, stamps, hourly,
                                                            int(signal["feature_as_of_ms"]), profile)))
            if trade:
                entry_ms = int(trade["entry_timestamp_ms"])
                entry_features = feature_snapshot(minutes, stamps, hourly, entry_ms, profile)
                feature_rows.append(dict(session_day=day_text, profile_window=window,
                                         snapshot="at_actual_fill", **entry_features))
                row.update(actual_entry_ms=entry_ms, actual_entry_price=float(trade["entry_price"]),
                           actual_direction=trade["direction"], actual_stop=float(trade["stop"]),
                           actual_net_r=float(trade["r"]), actual_exit_reason=trade["exit_reason"],
                           actual_path=stop_first_path(
                               minutes, stamps, entry_ms=entry_ms,
                               entry_price=float(trade["entry_price"]),
                               direction=trade["direction"], stop=float(trade["stop"]),
                               force_exit_ms=clock_ms(day+timedelta(days=1), 8, 59),
                               known_stop_ms=(int(trade["stop_trigger_timestamp_ms"])
                                              if trade.get("stop_trigger_timestamp_ms") is not None else None),
                               atr_at_entry=entry_features.get("atr14_1h"), infer_stop=False))
            edge = first_edge_test(profile, by_day_bars.get(day_text, []))
            if edge is None:
                row["shadow_status"] = "no_edge_close_by_1200"
            else:
                row.update(first_edge_direction=edge["direction"],
                           first_edge_signal_ms=edge["signal_timestamp_ms"])
                signal_ms = edge["signal_timestamp_ms"]
                feature_rows.append(dict(session_day=day_text, profile_window=window,
                                         snapshot="at_first_edge_close",
                                         **feature_snapshot(minutes, stamps, hourly, signal_ms, profile)))
                position = int(np.searchsorted(stamps, signal_ms, side="left"))
                if position >= len(stamps) or int(stamps[position])-signal_ms > 300_000 or int(stamps[position]) > clock_ms(day, 12):
                    row["shadow_status"] = "no_prompt_minute_fill"
                else:
                    entry_ms = int(stamps[position])
                    entry_price = float(minutes.iloc[position]["open"])
                    stop = float(profile["poc"])
                    entry_features = feature_snapshot(minutes, stamps, hourly, entry_ms, profile)
                    path = stop_first_path(minutes, stamps, entry_ms=entry_ms, entry_price=entry_price,
                                           direction=edge["direction"], stop=stop,
                                           force_exit_ms=clock_ms(day+timedelta(days=1), 8, 59),
                                           atr_at_entry=entry_features.get("atr14_1h"),
                                           entry_at_minute_open=True)
                    row.update(shadow_status=path["status"], shadow_entry_minute_ms=entry_ms,
                               shadow_entry_price=entry_price, shadow_stop=stop, shadow_path=path)
            setup_rows.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir/"session_opportunity.jsonl", session_rows)
    _write_jsonl(output_dir/"setup_audit.jsonl", setup_rows)
    _write_jsonl(output_dir/"feature_snapshots.jsonl", feature_rows)
    summary = dict(audit_schema_version=2, hourly_timestamp_unit="epoch_ms_explicit",
                   start_date=str(start_day), end_date=str(end_day), sessions=len(session_rows),
                   setups=len(setup_rows), feature_snapshots=len(feature_rows),
                   complete_session_paths=sum(row["status"] == "complete" for row in session_rows),
                   actual_executed=sum(row["actual_status"] == "executed" for row in setup_rows),
                   shadow_with_valid_geometry=sum(row["shadow_status"] in ("stop_touched", "time_exit")
                                                  for row in setup_rows),
                   method="minute_bar_bounds; no intraminute ordering asserted",
                   note="Shadow rows are hypothetical, not actual fills or P&L. VWAP is bar approximation.")
    (output_dir/"summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-dir", type=Path, required=True)
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--candle-cache", type=Path, action="append", required=True,
                        help="Repeat for adjacent 1-minute candle cache Parquet files")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(observation_dir=args.observation_dir, backtest_dir=args.backtest_dir,
                         candle_caches=args.candle_cache, start_day=args.start_date,
                         end_day=args.end_date, output_dir=args.output_dir), indent=2))


if __name__ == "__main__":
    main()
