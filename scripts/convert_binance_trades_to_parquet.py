import argparse
from pathlib import Path
import pandas as pd
import numpy as np

# Binance trade CSV format (headerless)
RAW_COLS = ["trade_id", "price", "qty", "quote_qty", "timestamp", "is_buyer_maker"]

AGG_RAW_COLS = [
    "agg_trade_id",
    "price",
    "qty",
    "first_trade_id",
    "last_trade_id",
    "timestamp",
    "is_buyer_maker",
]

def convert_csv_to_parquet(csv_path: Path, parquet_path: Path) -> None:
    """Convert a single Binance trade CSV to Parquet format with proper typing."""
    probe = pd.read_csv(csv_path, header=None, nrows=2, sep=None, engine="python", dtype=str)
    col_count = probe.shape[1]
    first_cell = str(probe.iloc[0, 0]).strip().lower() if not probe.empty else ""
    has_header = ("trade" in first_cell) or ("price" in first_cell) or (not first_cell.replace(".", "", 1).isdigit())
    skiprows = 1 if has_header else 0

    if col_count == 6:
        df = pd.read_csv(
            csv_path,
            header=None,
            names=RAW_COLS,
            sep=None,
            engine="python",
            skiprows=skiprows,
        )

        df["trade_id"] = pd.to_numeric(df["trade_id"], errors="coerce").astype("int64")
        df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float32")
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("float32")
        df["quote_qty"] = pd.to_numeric(df["quote_qty"], errors="coerce").astype("float32")
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("int64")
        maker = df["is_buyer_maker"].astype(str).str.strip().str.lower().map({"true": True, "false": False, "1": True, "0": False})
        df["is_buyer_maker"] = maker.astype(bool)
    elif col_count == 7:
        # aggTrades format: agg_trade_id, price, qty, first_trade_id, last_trade_id, timestamp, is_buyer_maker
        agg_df = pd.read_csv(
            csv_path,
            header=None,
            names=AGG_RAW_COLS,
            sep=None,
            engine="python",
            skiprows=skiprows,
        )

        agg_df["agg_trade_id"] = pd.to_numeric(agg_df["agg_trade_id"], errors="coerce").astype("int64")
        agg_df["price"] = pd.to_numeric(agg_df["price"], errors="coerce").astype("float32")
        agg_df["qty"] = pd.to_numeric(agg_df["qty"], errors="coerce").astype("float32")
        agg_df["first_trade_id"] = pd.to_numeric(agg_df["first_trade_id"], errors="coerce").astype("int64")
        agg_df["last_trade_id"] = pd.to_numeric(agg_df["last_trade_id"], errors="coerce").astype("int64")
        agg_df["timestamp"] = pd.to_numeric(agg_df["timestamp"], errors="coerce").astype("int64")
        maker = agg_df["is_buyer_maker"].astype(str).str.strip().str.lower().map({"true": True, "false": False, "1": True, "0": False})
        agg_df["is_buyer_maker"] = maker.astype(bool)

        df = pd.DataFrame(
            {
                "trade_id": agg_df["agg_trade_id"],
                "price": agg_df["price"],
                "qty": agg_df["qty"],
                "quote_qty": (agg_df["price"] * agg_df["qty"]).astype("float32"),
                "timestamp": agg_df["timestamp"],
                "is_buyer_maker": agg_df["is_buyer_maker"],
            }
        )
    else:
        raise ValueError(f"Unsupported CSV format with {col_count} columns: {csv_path}")
    
    # Sort by timestamp using stable sort
    df = df.sort_values("timestamp", kind="mergesort")
    
    # Save as Parquet
    df.to_parquet(parquet_path, index=False)

def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Binance trade CSVs to Parquet format")
    parser.add_argument("--input", required=True, help="Input CSV file")
    parser.add_argument("--output", required=True, help="Output Parquet file")
    args = parser.parse_args()
    
    input_path = Path(args.input)
    output_path = Path(args.output)
    
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    convert_csv_to_parquet(input_path, output_path)
    print(f"Successfully converted {input_path} to {output_path}")

if __name__ == "__main__":
    main()