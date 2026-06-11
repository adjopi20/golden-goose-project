import argparse
import os
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = [
    "timestamp",
    "price",
    "qty",
    "is_buyer_maker",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine multiple raw aggTrade parquet files into one sorted parquet."
    )
    parser.add_argument(
        "--input",
        required=True,
        help=(
            "Comma-separated parquet paths, or a directory containing parquet files. "
            "Example: storage/btcusdt/BTCUSDT-aggTrades-2026-01.parquet,"
            "storage/btcusdt/BTCUSDT-aggTrades-2026-02.parquet"
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output combined parquet path.",
    )
    parser.add_argument(
        "--drop-duplicates",
        action="store_true",
        help="Drop duplicate rows after combining. Recommended if files may overlap.",
    )
    parser.add_argument(
        "--compression",
        default="snappy",
        help="Parquet compression codec. Default: snappy.",
    )
    return parser.parse_args()


def resolve_input_paths(raw_input: str) -> list[Path]:
    raw_input = raw_input.strip()

    # Directory mode
    input_path = Path(raw_input)
    if input_path.exists() and input_path.is_dir():
        paths = sorted(input_path.glob("*.parquet"))
        if not paths:
            raise ValueError(f"No parquet files found in directory: {input_path}")
        return paths

    # Comma-separated file mode
    paths = [Path(part.strip()) for part in raw_input.split(",") if part.strip()]
    if not paths:
        raise ValueError("No input parquet paths provided")

    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing input parquet files: {missing}")

    return paths


def require_columns(df: pd.DataFrame, path: Path) -> None:
    missing = sorted(set(REQUIRED_COLUMNS) - set(df.columns))
    if missing:
        raise ValueError(f"{path} is missing required columns: {missing}")


def main() -> None:
    args = parse_args()

    input_paths = resolve_input_paths(args.input)
    output_path = Path(args.output)

    if output_path.parent:
        os.makedirs(output_path.parent, exist_ok=True)

    print("Input parquet files:")
    for path in input_paths:
        print(f" - {path}")

    frames: list[pd.DataFrame] = []

    for path in input_paths:
        print(f"Loading: {path}")
        df = pd.read_parquet(path)
        require_columns(df, path)

        # Normalize core dtypes
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="raise").astype("int64")
        df["price"] = pd.to_numeric(df["price"], errors="raise").astype("float64")
        df["qty"] = pd.to_numeric(df["qty"], errors="raise").astype("float64")
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)

        frames.append(df)

        print(
            f"   rows={len(df):,} | "
            f"min_ts={pd.to_datetime(df['timestamp'].min(), unit='ms', utc=True)} | "
            f"max_ts={pd.to_datetime(df['timestamp'].max(), unit='ms', utc=True)}"
        )

    combined = pd.concat(frames, ignore_index=True)

    print(f"\nCombined rows before duplicate handling: {len(combined):,}")

    if args.drop_duplicates:
        before = len(combined)
        combined = combined.drop_duplicates()
        after = len(combined)
        print(f"Dropped duplicates: {before - after:,}")

    # Sort from oldest to latest.
    sort_columns = ["timestamp"]

    # If aggTrade id exists, use it as secondary sort for deterministic order.
    for possible_id_col in ["agg_trade_id", "a", "trade_id", "id"]:
        if possible_id_col in combined.columns:
            sort_columns.append(possible_id_col)
            break

    combined = combined.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)

    if combined["timestamp"].isna().any():
        raise ValueError("Combined dataframe contains null timestamp values")

    if len(combined) > 1:
        timestamps = combined["timestamp"].to_numpy()
        if (timestamps[1:] < timestamps[:-1]).any():
            raise ValueError("Sort validation failed: timestamps are not ascending")

    print("\nCombined output:")
    print(f"rows={len(combined):,}")
    print(f"first timestamp UTC={pd.to_datetime(combined['timestamp'].iloc[0], unit='ms', utc=True)}")
    print(f"last timestamp UTC={pd.to_datetime(combined['timestamp'].iloc[-1], unit='ms', utc=True)}")
    print(f"writing to: {output_path}")

    combined.to_parquet(output_path, index=False, compression=args.compression)

    print("Done.")


if __name__ == "__main__":
    main()