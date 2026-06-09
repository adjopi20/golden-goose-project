from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


AGGTRADE_COLUMNS = [
    "agg_trade_id",
    "price",
    "qty",
    "first_trade_id",
    "last_trade_id",
    "timestamp",
    "is_buyer_maker",
    "is_best_match",
]

REQUIRED_COLUMNS = [
    "agg_trade_id",
    "price",
    "qty",
    "first_trade_id",
    "last_trade_id",
    "timestamp",
    "is_buyer_maker",
]

COLUMN_ALIASES = {
    "aggregate_trade_id": "agg_trade_id",
    "quantity": "qty",
    "transact_time": "timestamp",
    "trade_time": "timestamp",
    "maker": "is_buyer_maker",
    "was_best_price_match": "is_best_match",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Binance aggTrades CSV file to parquet.")
    parser.add_argument("--input", required=True, help="Input aggTrades CSV path")
    parser.add_argument("--output", required=True, help="Output parquet path")
    return parser.parse_args()


def normalize_bool_column(series: pd.Series, column_name: str) -> pd.Series:
    out = series.astype(str).str.strip().str.lower().map(
        {
            "true": True,
            "false": False,
            "1": True,
            "0": False,
        }
    )

    if out.isna().any():
        bad_values = series[out.isna()].drop_duplicates().head(10).tolist()
        raise ValueError(f"Invalid boolean values in {column_name}: {bad_values}")

    return out.astype(bool)


def normalize_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    chunk.columns = [str(column).strip() for column in chunk.columns]

    renamed_columns: dict[str, str] = {}
    for column in chunk.columns:
        normalized_name = COLUMN_ALIASES.get(column.strip().lower())
        if normalized_name is not None and column != normalized_name:
            renamed_columns[column] = normalized_name

    if renamed_columns:
        chunk = chunk.rename(columns=renamed_columns)

    missing = [column for column in REQUIRED_COLUMNS if column not in chunk.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}. Existing columns: {list(chunk.columns)}")

    if "is_best_match" not in chunk.columns:
        chunk["is_best_match"] = True

    chunk = chunk[AGGTRADE_COLUMNS].copy()

    chunk["agg_trade_id"] = pd.to_numeric(chunk["agg_trade_id"], errors="raise").astype("int64")
    chunk["price"] = pd.to_numeric(chunk["price"], errors="raise").astype("float64")
    chunk["qty"] = pd.to_numeric(chunk["qty"], errors="raise").astype("float64")
    chunk["first_trade_id"] = pd.to_numeric(chunk["first_trade_id"], errors="raise").astype("int64")
    chunk["last_trade_id"] = pd.to_numeric(chunk["last_trade_id"], errors="raise").astype("int64")
    chunk["timestamp"] = pd.to_numeric(chunk["timestamp"], errors="raise").astype("int64")
    chunk["is_buyer_maker"] = normalize_bool_column(chunk["is_buyer_maker"], "is_buyer_maker")
    chunk["is_best_match"] = normalize_bool_column(chunk["is_best_match"], "is_best_match")

    return chunk


def detect_header(input_path: Path) -> bool:
    first_line = input_path.open("r", encoding="utf-8").readline().strip().lower()
    return (
        "agg_trade_id" in first_line
        or "aggregate_trade_id" in first_line
        or ("price" in first_line and ("qty" in first_line or "quantity" in first_line))
    )


def convert_csv_to_parquet(input_path: Path, output_path: Path) -> None:
    has_header = detect_header(input_path)

    read_kwargs = {
        "chunksize": 1_000_000,
        "low_memory": False,
    }

    if has_header:
        read_kwargs["header"] = 0
    else:
        read_kwargs["header"] = None
        read_kwargs["names"] = AGGTRADE_COLUMNS

    chunks: list[pd.DataFrame] = []
    total_rows = 0

    print(f"Input has header: {has_header}", flush=True)

    for chunk_number, chunk in enumerate(pd.read_csv(input_path, **read_kwargs), start=1):
        normalized = normalize_chunk(chunk)
        chunks.append(normalized)
        total_rows += len(normalized)
        print(f"Processed chunk {chunk_number:,} | rows so far: {total_rows:,}", flush=True)

    if not chunks:
        raise ValueError("No rows were read from the input CSV")

    df = pd.concat(chunks, ignore_index=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Rows loaded: {len(df):,}", flush=True)
    print(f"First timestamp UTC: {pd.to_datetime(df['timestamp'].min(), unit='ms', utc=True)}", flush=True)
    print(f"Last timestamp UTC:  {pd.to_datetime(df['timestamp'].max(), unit='ms', utc=True)}", flush=True)
    print(f"Writing parquet: {output_path}", flush=True)

    df.to_parquet(output_path, index=False)

    print("Done.", flush=True)


def main() -> None:
    args = parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    if output_path.suffix.lower() != ".parquet":
        raise ValueError("--output must end with .parquet")

    print(f"Reading CSV: {input_path}", flush=True)
    convert_csv_to_parquet(input_path, output_path)


if __name__ == "__main__":
    main()