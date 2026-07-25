from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .slow_cycle import compute_market_cycle_regimes


def daily_ohlc_from_aggtrades(paths: list[Path], batch_size: int = 1_000_000) -> pd.DataFrame:
    rows = []
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(columns=["timestamp", "price"], batch_size=batch_size):
            df = batch.to_pandas()
            df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.floor("D")
            rows.append(
                df.groupby("date", as_index=False).agg(
                    open=("price", "first"),
                    high=("price", "max"),
                    low=("price", "min"),
                    close=("price", "last"),
                )
            )
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    daily = pd.concat(rows, ignore_index=True).sort_values("date")
    return daily.groupby("date", as_index=False).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))


def classify_aggtrades(paths: list[Path], output: Path, csv: bool = False) -> pd.DataFrame:
    regimes = compute_market_cycle_regimes(daily_ohlc_from_aggtrades(paths))
    output.parent.mkdir(parents=True, exist_ok=True)
    if csv:
        regimes.to_csv(output, index=False)
    else:
        regimes.to_parquet(output, index=False)
    return regimes


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify aggTrade history into observer-only slow regime labels.")
    parser.add_argument("--input", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()
    regimes = classify_aggtrades([Path(p) for p in args.input], Path(args.output), args.csv)
    print({"rows": len(regimes), "output": args.output})


if __name__ == "__main__":
    main()
