from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .classify_dataset import daily_ohlc_from_aggtrades


HALVINGS = (date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20))


def classify_halving_cycle(daily: pd.DataFrame, frequency: str = "W-SUN") -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    df = _daily_index(daily)
    weekly = df[["close"]].resample(frequency).last().dropna().reset_index(names="date")
    labels = [_label(ts.date()) for ts in weekly["date"]]
    weekly["halving_cycle"] = [row["cycle"] for row in labels]
    weekly["halving_phase"] = [row["phase"] for row in labels]
    weekly["cycle_week"] = [row["week"] for row in labels]
    weekly["observer_only"] = True
    return weekly


def classify_aggtrades_halving(paths: list[Path], output: Path, csv: bool = False) -> pd.DataFrame:
    regimes = classify_halving_cycle(daily_ohlc_from_aggtrades(paths))
    output.parent.mkdir(parents=True, exist_ok=True)
    if csv:
        regimes.to_csv(output, index=False)
    else:
        regimes.to_parquet(output, index=False)
    return regimes


def _label(day: date) -> dict[str, Any]:
    previous = max(halving for halving in HALVINGS if halving <= day)
    days = (day - previous).days
    return {
        "cycle": f"{previous.isoformat()}_halving_cycle",
        "phase": _phase(days),
        "week": days // 7,
    }


def _phase(days_since_halving: int) -> str:
    if days_since_halving < 180:
        return "post_halving_0_6m"
    if days_since_halving < 365:
        return "post_halving_6_12m"
    if days_since_halving < 730:
        return "cycle_year_2"
    if days_since_halving < 1095:
        return "cycle_year_3"
    return "cycle_year_4"


def _daily_index(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()
    if "date" in df:
        dt = pd.to_datetime(df["date"], utc=True)
    elif "timestamp_ms" in df:
        dt = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    else:
        dt = pd.to_datetime(df.index, utc=True)
    if "close" not in df:
        raise ValueError("daily data must include a close column")
    return df.assign(dt=dt).set_index("dt").sort_index()


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify aggTrade history into simple BTC halving-cycle regimes.")
    parser.add_argument("--input", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()
    regimes = classify_aggtrades_halving([Path(p) for p in args.input], Path(args.output), args.csv)
    print({"rows": len(regimes), "output": args.output})


if __name__ == "__main__":
    main()
