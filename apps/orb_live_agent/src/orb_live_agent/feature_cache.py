from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.parquet as pq

from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.volume_profile import build_volume_profile

from .config import load_config
from .trigger_observer import observe_triggers


@dataclass(frozen=True)
class FeatureSet:
    rows_loaded: int
    candles: list[dict[str, Any]]
    contexts: dict[date, dict[str, Any]]
    triggers: dict[int, dict[str, Any]]
    force_exit_trades: dict[int, dict[str, Any]]


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def ms(day: date, t: time, tz: ZoneInfo) -> int:
    dt = datetime.combine(day, t, tzinfo=tz)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _validate_parquet_timestamp_ms(path: Path) -> None:
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows == 0 or parquet.metadata.num_row_groups == 0:
        raise ValueError("Input parquet is empty")
    index = parquet.schema_arrow.get_field_index("timestamp")
    if index < 0:
        raise ValueError("Input parquet is missing timestamp")
    for row_group in range(parquet.metadata.num_row_groups):
        stats = parquet.metadata.row_group(row_group).column(index).statistics
        if not stats or not stats.has_min_max:
            raise ValueError("Input parquet requires timestamp statistics for millisecond validation")
        for sample in (stats.min, stats.max):
            if not isinstance(sample, (int, float)) or not (
                100_000_000_000 <= int(sample) <= 99_999_999_999_999
            ):
                raise ValueError(
                    "Input parquet timestamps must all be Unix milliseconds; "
                    f"row group {row_group} contains {sample}. Rebuild it with "
                    "utils/export_combine_aggtrades_parquets.py."
                )


def local_datetime(timestamp_ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(tz)


def in_entry_window(timestamp_ms: int, tz: ZoneInfo, window_minutes: int, entry_start: time | None = None) -> tuple[date, bool]:
    local_dt = local_datetime(timestamp_ms, tz)
    start = datetime.combine(local_dt.date(), entry_start or time(9, 45), tzinfo=tz)
    return local_dt.date(), start <= local_dt < start + timedelta(minutes=window_minutes)


def build_or_load_feature_set(
    input_path: Path,
    start_day: date,
    end_day: date,
    config: Any,
    cache_dir: Path | None = None,
    use_cache: bool = False,
    refresh_cache: bool = False,
) -> FeatureSet:
    if use_cache and cache_dir is None:
        raise ValueError("--use-cache requires --cache-dir")
    _validate_parquet_timestamp_ms(input_path)
    if use_cache and cache_dir is not None and not refresh_cache and _cache_ready(cache_dir):
        return _load_cache(cache_dir)

    features = _build_feature_set(input_path, start_day, end_day, config)
    if use_cache and cache_dir is not None:
        _write_cache(cache_dir, features, input_path, start_day, end_day, config)
    return features


def _build_feature_set(input_path: Path, start_day: date, end_day: date, config: Any) -> FeatureSet:
    tz = ZoneInfo(config.session_timezone)
    session_start = parse_time(getattr(config, "orb_session_start_time", config.ny_open_time))
    force_exit_time = parse_time(getattr(config, "paper_max_hold_exit_time", config.pre_ny_start_time))
    preload_ms = ms(start_day, session_start, tz) - 25 * 60 * 60_000
    replay_end_ms = ms(end_day + timedelta(days=1), force_exit_time, tz) + 60 * 60_000
    candles_by_ms: dict[int, dict[str, Any]] = {}
    contexts: dict[date, dict[str, Any]] = {}
    triggers: dict[int, dict[str, Any]] = {}
    force_exit_trades: dict[int, dict[str, Any]] = {}
    rows_loaded = 0
    chunk_start = start_day

    while chunk_start <= end_day:
        chunk_end = min(chunk_start + timedelta(days=6), end_day)
        read_start_ms = ms(chunk_start, session_start, tz) - 25 * 60 * 60_000
        read_end_ms = max(
            ms(chunk_end + timedelta(days=1), force_exit_time, tz) + 60 * 60_000,
            ms(chunk_end + timedelta(days=1), session_start, tz),
        )
        df = pd.read_parquet(
            input_path,
            filters=[("timestamp", ">=", read_start_ms), ("timestamp", "<", read_end_ms)],
        ).sort_values("timestamp", kind="mergesort").reset_index(drop=True)
        ts = [int(v) for v in df["timestamp"].to_list()]
        chunk_candles = aggregate_trades_to_ohlcv(df, config.symbol, "1m")
        chunk_contexts = _session_contexts(df, ts, chunk_start, chunk_end, config, tz)

        keep_start_ms = preload_ms if chunk_start == start_day else ms(chunk_start, session_start, tz)
        keep_end_ms = replay_end_ms if chunk_end == end_day else ms(chunk_end + timedelta(days=1), session_start, tz)
        rows_loaded += int(((df["timestamp"] >= keep_start_ms) & (df["timestamp"] < keep_end_ms)).sum())
        candles_by_ms.update(
            (int(candle["timestamp_ms"]), candle)
            for candle in chunk_candles
            if keep_start_ms <= int(candle["timestamp_ms"]) < keep_end_ms
        )
        contexts.update(chunk_contexts)
        triggers.update(_minute_triggers(df, ts, chunk_candles, chunk_contexts, config, tz))
        force_exit_trades.update(_force_exit_trades(df, ts, chunk_start, chunk_end, config, tz))
        chunk_start = chunk_end + timedelta(days=1)

    candles = [candles_by_ms[key] for key in sorted(candles_by_ms)]
    return FeatureSet(
        rows_loaded=rows_loaded,
        candles=candles,
        contexts=contexts,
        triggers=triggers,
        force_exit_trades=force_exit_trades,
    )


def _slice(df: pd.DataFrame, ts: list[int], start_ms: int, end_ms: int) -> pd.DataFrame:
    return df.iloc[bisect_left(ts, start_ms):bisect_left(ts, end_ms)]


def _profile_for(df: pd.DataFrame, bins: int, profile_type: str, start_ms: int, end_ms: int, tz: ZoneInfo) -> dict[str, Any] | None:
    if len(df) < 2:
        return None
    profile = build_volume_profile(df, n_bins=bins)
    total_volume = sum(float(row["total_volume"]) for row in profile.get("volume_profile", []))
    if total_volume > 0:
        profile["total_volume"] = total_volume
        profile["poc_volume_pct"] = float(profile["poc_volume"]) / total_volume
    if profile.get("val") is not None and profile.get("vah") is not None:
        profile["value_area_width"] = float(profile["vah"]) - float(profile["val"])
    profile["profile_type"] = profile_type
    profile["window_start"] = datetime.fromtimestamp(start_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
    profile["window_end"] = datetime.fromtimestamp(end_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
    profile["timezone"] = str(tz)
    return profile


def _extreme(df: pd.DataFrame, session: str, day: date, tz: ZoneInfo) -> dict[str, Any] | None:
    if df.empty:
        return None
    return {
        "session": session,
        "session_day": day.isoformat(),
        "timezone": str(tz),
        "high": float(df["price"].max()),
        "low": float(df["price"].min()),
    }


def _session_contexts(df: pd.DataFrame, ts: list[int], start_day: date, end_day: date, config: Any, tz: ZoneInfo) -> dict[date, dict[str, Any]]:
    ny_open = parse_time(config.ny_open_time)
    orb_session_start = parse_time(getattr(config, "orb_session_start_time", config.ny_open_time))
    pre_ny_start = parse_time(config.pre_ny_start_time)
    overnight_start = parse_time(config.overnight_start_time)
    setup_cutoff = parse_time(config.setup_cutoff_time)
    out: dict[date, dict[str, Any]] = {}
    for offset in range((end_day - start_day).days + 1):
        day = start_day + timedelta(days=offset)
        ny_open_ms = ms(day, ny_open, tz)
        orb_start_ms = ms(day, orb_session_start, tz)
        orb_end_ms = orb_start_ms + 15 * 60_000
        prior_start_ms = orb_start_ms - 24 * 60 * 60_000

        prior24 = _slice(df, ts, prior_start_ms, orb_start_ms)
        orb = _slice(df, ts, orb_start_ms, orb_end_ms)
        previous_ny = _slice(df, ts, ms(day - timedelta(days=1), ny_open, tz), ms(day - timedelta(days=1), setup_cutoff, tz))
        overnight = _slice(df, ts, ms(day - timedelta(days=1), overnight_start, tz), ms(day, pre_ny_start, tz))
        pre_ny = _slice(df, ts, ms(day, pre_ny_start, tz), ny_open_ms)

        previous_profile = _profile_for(prior24, config.volume_profile_bins, "previous_24h_profile_for_session", prior_start_ms, ny_open_ms, tz)
        if previous_profile:
            previous_profile["frozen_at_session_open"] = True
        orb_profile = _profile_for(orb, config.volume_profile_bins, "ny_first_15m_profile", ny_open_ms, orb_end_ms, tz)
        if orb_profile:
            orb_profile["frozen_at_window_end"] = True

        out[day] = {
            "session_extremes": {
                "pre_ny": _extreme(pre_ny, "pre_ny", day, tz),
                "overnight": _extreme(overnight, "overnight", day, tz),
                "previous_ny": _extreme(previous_ny, "ny", day - timedelta(days=1), tz),
            },
            "previous_24h_profile_for_session": previous_profile,
            "ny_first_15m_profile": orb_profile,
            "p95_qty": float(prior24["qty"].quantile(config.bubble_percentile)) if not prior24.empty else None,
        }
    return out


def _bubbles(df: pd.DataFrame, p95_qty: float | None) -> list[dict[str, Any]]:
    if p95_qty is None or df.empty:
        return []
    rows = df.loc[df["qty"] >= p95_qty]
    return [{"price": float(r.price), "qty": float(r.qty), "is_buyer_maker": bool(r.is_buyer_maker)} for r in rows.itertuples()]


def _minute_triggers(
    df: pd.DataFrame,
    ts: list[int],
    candles: list[dict[str, Any]],
    contexts: dict[date, dict[str, Any]],
    config: Any,
    tz: ZoneInfo,
) -> dict[int, dict[str, Any]]:
    triggers: dict[int, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    entry_start = parse_time(getattr(config, "orb_entry_start_time", "09:45"))
    for candle in candles:
        candle_ms = int(candle["timestamp_ms"])
        recent.append(candle)
        day, active = in_entry_window(candle_ms, tz, config.orb_entry_window_minutes, entry_start)
        if not active:
            continue
        context = contexts.get(day)
        if not context:
            continue
        snapshot = {
            "symbol": config.symbol,
            "snapshot_timestamp_ms": candle_ms,
            "target_session_day": day.isoformat(),
            "session_timezone": config.session_timezone,
            "setup_observation_active": True,
            "last_candle": candle,
            "recent_candles": recent[-30:],
            "orb_entry_start_time": getattr(config, "orb_entry_start_time", "09:45"),
            **context,
        }
        minute_trades = _slice(df, ts, candle_ms, candle_ms + 60_000)
        triggers[candle_ms] = observe_triggers(snapshot, _bubbles(minute_trades, context.get("p95_qty")), context)
    return triggers


def _force_exit_trades(df: pd.DataFrame, ts: list[int], start_day: date, end_day: date, config: Any, tz: ZoneInfo) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    exit_time = parse_time(getattr(config, "paper_max_hold_exit_time", config.pre_ny_start_time))
    for offset in range((end_day - start_day).days + 1):
        exit_ms = ms(start_day + timedelta(days=offset + 1), exit_time, tz)
        idx = bisect_left(ts, exit_ms)
        if idx < len(df):
            row = df.iloc[int(idx)]
            out[exit_ms] = {"timestamp": int(row["timestamp"]), "price": float(row["price"])}
    return out


def _cache_ready(cache_dir: Path) -> bool:
    return all((cache_dir / name).exists() for name in ("candles_1m.parquet", "session_contexts.json", "minute_orderflow.parquet", "force_exit_trades.json", "manifest.json"))


def _write_cache(cache_dir: Path, features: FeatureSet, input_path: Path, start_day: date, end_day: date, config: Any) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(features.candles).to_parquet(cache_dir / "candles_1m.parquet", index=False)
    (cache_dir / "session_contexts.json").write_text(
        json.dumps({day.isoformat(): value for day, value in features.contexts.items()}, sort_keys=True, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    pd.DataFrame([_flatten_trigger(trigger) for trigger in features.triggers.values()]).to_parquet(cache_dir / "minute_orderflow.parquet", index=False)
    (cache_dir / "force_exit_trades.json").write_text(
        json.dumps({str(k): v for k, v in features.force_exit_trades.items()}, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    manifest = {
        "input": str(input_path),
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "session_timezone": config.session_timezone,
        "ny_open_time": config.ny_open_time,
        "pre_ny_start_time": config.pre_ny_start_time,
        "orb_session_start_time": getattr(config, "orb_session_start_time", config.ny_open_time),
        "orb_entry_start_time": getattr(config, "orb_entry_start_time", "09:45"),
        "paper_max_hold_exit_time": getattr(config, "paper_max_hold_exit_time", config.pre_ny_start_time),
        "overnight_start_time": config.overnight_start_time,
        "setup_cutoff_time": config.setup_cutoff_time,
        "volume_profile_bins": config.volume_profile_bins,
        "bubble_percentile": config.bubble_percentile,
        "bubble_lookback_min_trades": config.bubble_lookback_min_trades,
        "rows_loaded": features.rows_loaded,
        "candles": len(features.candles),
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _load_cache(cache_dir: Path) -> FeatureSet:
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    candles = pd.read_parquet(cache_dir / "candles_1m.parquet").to_dict("records")
    contexts = {date.fromisoformat(k): v for k, v in json.loads((cache_dir / "session_contexts.json").read_text(encoding="utf-8")).items()}
    trigger_rows = pd.read_parquet(cache_dir / "minute_orderflow.parquet").to_dict("records")
    triggers = {int(row["snapshot_timestamp_ms"]): _inflate_trigger(row) for row in trigger_rows}
    force_exit_trades = {int(k): v for k, v in json.loads((cache_dir / "force_exit_trades.json").read_text(encoding="utf-8")).items()}
    return FeatureSet(
        rows_loaded=int(manifest.get("rows_loaded", 0)),
        candles=candles,
        contexts=contexts,
        triggers=triggers,
        force_exit_trades=force_exit_trades,
    )


def _flatten_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
    features = trigger.get("orderflow_features") or {}
    return {
        "snapshot_timestamp_ms": int(trigger["snapshot_timestamp_ms"]),
        "setup_observation_active": bool(trigger.get("setup_observation_active")),
        "triggered": bool(trigger.get("triggered")),
        "reasons_json": json.dumps(trigger.get("reasons") or [], separators=(",", ":")),
        **features,
    }


def _inflate_trigger(row: dict[str, Any]) -> dict[str, Any]:
    feature_keys = set(row) - {"snapshot_timestamp_ms", "setup_observation_active", "triggered", "reasons_json"}
    features = {key: _clean_value(row[key]) for key in feature_keys}
    return {
        "snapshot_timestamp_ms": int(row["snapshot_timestamp_ms"]),
        "setup_observation_active": bool(row.get("setup_observation_active")),
        "triggered": bool(row.get("triggered")),
        "reasons": json.loads(str(row.get("reasons_json") or "[]")),
        "orderflow_features": features,
        "mode": "observe_only",
    }


def _clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Build reusable ORB backtest feature cache.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args()
    config = load_config()
    features = build_or_load_feature_set(
        Path(args.input),
        parse_date(args.start_date),
        parse_date(args.end_date),
        config,
        Path(args.cache_dir),
        use_cache=True,
        refresh_cache=True,
    )
    print(json.dumps({"event": "feature_cache_built", "rows_loaded": features.rows_loaded, "candles": len(features.candles), "cache_dir": args.cache_dir}, indent=2))


if __name__ == "__main__":
    main()
