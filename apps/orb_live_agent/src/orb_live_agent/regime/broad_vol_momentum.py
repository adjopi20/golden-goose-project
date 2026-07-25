from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


DEFAULT_PARAMS = {
    "bar_size": "1h",
    "short_vol_hours": 6,
    "long_vol_days": 7,
    "momentum_hours": 24,
    "normalization_days": 30,
    "expansion_vol_ratio": 1.25,
    "expansion_momentum_z": 1.0,
    "contraction_vol_ratio": 0.80,
    "contraction_momentum_z": 0.75,
}


def build_or_load_regime_cache(
    feature_cache_dir: Path,
    cache_dir: Path | None = None,
    refresh: bool = False,
    **params: Any,
) -> pd.DataFrame:
    cfg = {**DEFAULT_PARAMS, **{k: v for k, v in params.items() if v is not None}}
    out_dir = cache_dir or feature_cache_dir
    regime_path = out_dir / "regime_1h.parquet"
    manifest_path = out_dir / "regime_manifest.json"
    if not refresh and _manifest_matches(manifest_path, cfg) and regime_path.exists():
        return pd.read_parquet(regime_path)

    candles = pd.read_parquet(feature_cache_dir / "candles_1m.parquet")
    regimes = compute_regimes(candles, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    regimes.to_parquet(regime_path, index=False)
    manifest_path.write_text(json.dumps({"params": cfg, "rows": len(regimes)}, indent=2, sort_keys=True), encoding="utf-8")
    return regimes


def compute_regimes(candles: pd.DataFrame, params: dict[str, Any] | None = None) -> pd.DataFrame:
    cfg = {**DEFAULT_PARAMS, **(params or {})}
    if candles.empty:
        return pd.DataFrame()

    df = candles.copy()
    df["dt"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.set_index("dt").sort_index()
    bars = df.resample(str(cfg["bar_size"])).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    close = bars["close"].astype(float)
    returns = (close / close.shift(1)).apply(_safe_log)

    short_n = int(cfg["short_vol_hours"])
    long_n = int(cfg["long_vol_days"]) * 24
    mom_n = int(cfg["momentum_hours"])
    norm_n = int(cfg["normalization_days"]) * 24

    rv_short = returns.pow(2).rolling(short_n).mean().pow(0.5)
    rv_long = returns.pow(2).rolling(long_n).mean().pow(0.5)
    vol_ratio = rv_short / rv_long
    momentum = (close / close.shift(mom_n)).apply(_safe_log)
    momentum_z = (momentum - momentum.rolling(norm_n).mean()) / momentum.rolling(norm_n).std()

    out = pd.DataFrame(
        {
            "timestamp_ms": [int(dt.timestamp() * 1000) for dt in bars.index],
            "regime_raw": ["neutral"] * len(bars),
            "direction": ["none"] * len(bars),
            "vol_ratio": vol_ratio.to_numpy(),
            "momentum_z": momentum_z.to_numpy(),
            "rv_short": rv_short.to_numpy(),
            "rv_long": rv_long.to_numpy(),
        }
    )

    up = out["momentum_z"] >= float(cfg["expansion_momentum_z"])
    down = out["momentum_z"] <= -float(cfg["expansion_momentum_z"])
    expansion = (out["vol_ratio"] >= float(cfg["expansion_vol_ratio"])) & (up | down)
    contraction = (out["vol_ratio"] <= float(cfg["contraction_vol_ratio"])) & (out["momentum_z"].abs() <= float(cfg["contraction_momentum_z"]))
    out.loc[expansion, "regime_raw"] = "expansion"
    out.loc[contraction, "regime_raw"] = "contraction"
    out.loc[up, "direction"] = "up"
    out.loc[down, "direction"] = "down"
    out["confidence"] = _confidence(out, cfg)
    out["regime_effective"] = _three_bar_confirm(out["regime_raw"])
    return out.where(pd.notnull(out), None)


def frozen_regime_for_session(regimes: pd.DataFrame, session_day: date, session_tz: str, ny_open_time: str = "09:30") -> dict[str, Any] | None:
    if regimes.empty:
        return None
    hour, minute = ny_open_time.split(":", maxsplit=1)
    ny_open = datetime.combine(session_day, time(int(hour), int(minute)), tzinfo=ZoneInfo(session_tz)).astimezone(timezone.utc)
    cutoff_ms = int(ny_open.timestamp() * 1000)
    prior = regimes.loc[regimes["timestamp_ms"] < cutoff_ms]
    if prior.empty:
        return None
    row = prior.iloc[-1].to_dict()
    row["frozen_at_session_open"] = True
    row["session_day"] = session_day.isoformat()
    return row


def _safe_log(value: float) -> float | None:
    if value is None or value <= 0:
        return None
    import math

    return math.log(float(value))


def _confidence(out: pd.DataFrame, cfg: dict[str, Any]) -> pd.Series:
    expansion_vol = ((out["vol_ratio"] - 1.0) / (float(cfg["expansion_vol_ratio"]) - 1.0)).clip(0, 1)
    expansion_mom = ((out["momentum_z"].abs() - float(cfg["expansion_momentum_z"])) / float(cfg["expansion_momentum_z"])).clip(0, 1)
    contraction_vol = ((1.0 - out["vol_ratio"]) / (1.0 - float(cfg["contraction_vol_ratio"]))).clip(0, 1)
    contraction_mom = (1.0 - (out["momentum_z"].abs() / float(cfg["contraction_momentum_z"]))).clip(0, 1)
    confidence = pd.Series(0.0, index=out.index)
    confidence[out["regime_raw"] == "expansion"] = pd.concat([expansion_vol, expansion_mom], axis=1).min(axis=1)
    confidence[out["regime_raw"] == "contraction"] = pd.concat([contraction_vol, contraction_mom], axis=1).min(axis=1)
    return confidence.fillna(0.0)


def _three_bar_confirm(raw: pd.Series) -> list[str]:
    effective: list[str] = []
    current = "neutral"
    for i, value in enumerate(raw.to_list()):
        if i >= 2 and raw.iloc[i - 2] == value and raw.iloc[i - 1] == value:
            current = str(value)
        effective.append(current)
    return effective


def _manifest_matches(path: Path, params: dict[str, Any]) -> bool:
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("params") == params
    except json.JSONDecodeError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Build broad frozen regime cache from ORB feature-cache candles.")
    parser.add_argument("--feature-cache-dir", required=True)
    parser.add_argument("--cache-dir")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--bar-size", default=DEFAULT_PARAMS["bar_size"])
    parser.add_argument("--short-vol-hours", type=int, default=DEFAULT_PARAMS["short_vol_hours"])
    parser.add_argument("--long-vol-days", type=int, default=DEFAULT_PARAMS["long_vol_days"])
    parser.add_argument("--momentum-hours", type=int, default=DEFAULT_PARAMS["momentum_hours"])
    parser.add_argument("--normalization-days", type=int, default=DEFAULT_PARAMS["normalization_days"])
    args = parser.parse_args()
    regimes = build_or_load_regime_cache(
        Path(args.feature_cache_dir),
        Path(args.cache_dir) if args.cache_dir else None,
        refresh=args.refresh,
        bar_size=args.bar_size,
        short_vol_hours=args.short_vol_hours,
        long_vol_days=args.long_vol_days,
        momentum_hours=args.momentum_hours,
        normalization_days=args.normalization_days,
    )
    print(json.dumps({"event": "regime_cache_built", "rows": len(regimes), "feature_cache_dir": args.feature_cache_dir}, indent=2))


if __name__ == "__main__":
    main()
