from pathlib import Path

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


REQUIRED_COLUMNS = [
    "timestamp",
    "price",
    "qty",
    "is_buyer_maker",
]

BINANCE_AGGTRADES_NO_HEADER_COLUMNS = [
    "agg_trade_id",
    "price",
    "qty",
    "first_trade_id",
    "last_trade_id",
    "timestamp",
    "is_buyer_maker",
    "is_best_match",
]

SUPPORTED_EXTENSIONS = {".csv", ".parquet"}


@dataclass(frozen=True)
class MonthToken:
    year: int
    month: int
    start: int
    end: int
    separator: str

    @property
    def value(self) -> str:
        if self.separator == "":
            return f"{self.year:04d}{self.month:02d}"
        return f"{self.year:04d}{self.separator}{self.month:02d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert raw aggTrade CSV/parquet files into normalized monthly parquet parts "
            "and/or one combined sorted parquet."
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        nargs="+",
        help=(
            "Input source. Supports: "
            "(1) a directory containing CSV/parquet files, "
            "(2) comma-separated explicit files, or "
            "(3) two endpoint monthly files, which auto-expands the full inclusive month range. "
            "Example: --input storage/avaxusdc/excel/AVAXUSDC-aggTrades-2024-06.csv, "
            "storage/avaxusdc/excel/AVAXUSDC-aggTrades-2025-05.csv"
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output combined parquet path.",
    )
    parser.add_argument(
        "--input-mode",
        choices=["auto", "range", "explicit", "directory"],
        default="auto",
        help=(
            "auto = directory if input is a directory; monthly range if exactly 2 files with YYYY-MM; "
            "otherwise explicit file list. Default: auto."
        ),
    )
    parser.add_argument(
        "--parts-output-dir",
        default=None,
        help=(
            "Optional directory to export each normalized monthly file as its own parquet. "
            "If omitted, only the combined parquet is written."
        ),
    )
    parser.add_argument(
        "--csv-format",
        choices=["auto", "header", "binance-no-header"],
        default="auto",
        help=(
            "CSV reader format. auto tries header first, then Binance aggTrades no-header. "
            "Default: auto."
        ),
    )
    parser.add_argument(
        "--drop-duplicates",
        action="store_true",
        help="Drop duplicate rows after combining. Recommended if files may overlap.",
    )
    parser.add_argument(
        "--dedupe-subset",
        default=None,
        help=(
            "Optional comma-separated columns to use for duplicate detection. "
            "Example: agg_trade_id or timestamp,price,qty,is_buyer_maker. "
            "If omitted, drop_duplicates uses all columns."
        ),
    )
    parser.add_argument(
        "--compression",
        default="snappy",
        help="Parquet compression codec. Default: snappy.",
    )
    parser.add_argument(
        "--fail-on-unsorted-input",
        action="store_true",
        help="Raise an error if an individual input file is not timestamp-sorted.",
    )
    return parser.parse_args()


def split_input_tokens(input_tokens: list[str]) -> list[str]:
    """
    Mendukung kedua format:

      --input file1 file2 file3
      --input file1,file2,file3
    """
    parts: list[str] = []

    for token in input_tokens:
        for part in token.split(","):
            cleaned = part.strip().strip('"').strip("'")
            if cleaned:
                parts.append(cleaned)

    return parts


def extract_last_month_token(filename: str) -> MonthToken:
    """
    Find the last YYYY-MM, YYYY_MM, or YYYYMM token in a filename.

    Examples:
      AVAXUSDC-aggTrades-2024-06.csv -> 2024-06
      AVAXUSDC-aggTrades-202406.csv  -> 202406
    """
    pattern = re.compile(r"(?P<year>20\d{2})(?P<sep>[-_]?)(?P<month>0[1-9]|1[0-2])")
    matches = list(pattern.finditer(filename))
    if not matches:
        raise ValueError(f"Could not find YYYY-MM / YYYY_MM / YYYYMM month token in filename: {filename}")

    match = matches[-1]
    return MonthToken(
        year=int(match.group("year")),
        month=int(match.group("month")),
        start=match.start(),
        end=match.end(),
        separator=match.group("sep"),
    )


def month_index(year: int, month: int) -> int:
    return year * 12 + month


def iter_months_inclusive(start: MonthToken, end: MonthToken) -> Iterable[tuple[int, int]]:
    start_index = month_index(start.year, start.month)
    end_index = month_index(end.year, end.month)

    if start_index > end_index:
        raise ValueError(
            f"Start month {start.year:04d}-{start.month:02d} is after "
            f"end month {end.year:04d}-{end.month:02d}"
        )

    for idx in range(start_index, end_index + 1):
        year = idx // 12
        month = idx % 12
        if month == 0:
            year -= 1
            month = 12
        yield year, month


def replace_month_token(path: Path, token: MonthToken, year: int, month: int) -> Path:
    name = path.name
    replacement = f"{year:04d}{token.separator}{month:02d}" if token.separator else f"{year:04d}{month:02d}"
    new_name = name[: token.start] + replacement + name[token.end :]
    return path.with_name(new_name)


def resolve_month_range_paths(start_path: Path, end_path: Path) -> list[Path]:
    start_token = extract_last_month_token(start_path.name)
    end_token = extract_last_month_token(end_path.name)

    if start_path.suffix.lower() != end_path.suffix.lower():
        raise ValueError(
            f"Range endpoints must use the same extension: {start_path.suffix} vs {end_path.suffix}"
        )

    if start_path.parent != end_path.parent:
        raise ValueError(
            f"Range endpoints must be in the same directory: {start_path.parent} vs {end_path.parent}"
        )

    expected_end_path = replace_month_token(
        start_path,
        start_token,
        end_token.year,
        end_token.month,
    )
    if expected_end_path.name != end_path.name:
        raise ValueError(
            "Range endpoints do not appear to share the same filename pattern.\n"
            f"Start pattern predicts end file: {expected_end_path.name}\n"
            f"Actual end file:              {end_path.name}"
        )

    paths = [
        replace_month_token(start_path, start_token, year, month)
        for year, month in iter_months_inclusive(start_token, end_token)
    ]

    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing monthly files inside requested range:\n"
            + "\n".join(f" - {path}" for path in missing)
        )

    return paths


def resolve_directory_paths(directory: Path) -> list[Path]:
    if not directory.exists():
        raise FileNotFoundError(f"Input directory does not exist: {directory}")
    if not directory.is_dir():
        raise ValueError(f"Input is not a directory: {directory}")

    paths = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not paths:
        raise ValueError(f"No CSV/parquet files found in directory: {directory}")
    return paths


def resolve_explicit_paths(raw_paths: list[Path]) -> list[Path]:
    if not raw_paths:
        raise ValueError("No input paths provided")

    invalid_ext = [str(path) for path in raw_paths if path.suffix.lower() not in SUPPORTED_EXTENSIONS]
    if invalid_ext:
        raise ValueError(
            "Unsupported input extension. Only .csv and .parquet are supported:\n"
            + "\n".join(f" - {path}" for path in invalid_ext)
        )

    missing = [str(path) for path in raw_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing input files:\n"
            + "\n".join(f" - {path}" for path in missing)
        )

    return raw_paths


def resolve_input_paths(input_tokens: list[str], input_mode: str) -> list[Path]:
    input_strings = split_input_tokens(input_tokens)
    raw_paths = [Path(value) for value in input_strings]

    if input_mode == "directory":
        if len(raw_paths) != 1:
            raise ValueError("--input-mode directory requires exactly one directory path")
        return resolve_directory_paths(raw_paths[0])

    if input_mode == "range":
        if len(raw_paths) != 2:
            raise ValueError("--input-mode range requires exactly two endpoint file paths")
        return resolve_month_range_paths(raw_paths[0], raw_paths[1])

    if input_mode == "explicit":
        return resolve_explicit_paths(raw_paths)

    # auto mode
    if len(raw_paths) == 1 and raw_paths[0].exists() and raw_paths[0].is_dir():
        return resolve_directory_paths(raw_paths[0])

    if len(raw_paths) == 2:
        try:
            return resolve_month_range_paths(raw_paths[0], raw_paths[1])
        except ValueError:
            # If they supplied two unrelated files, use explicit mode.
            # FileNotFoundError should still fail because a missing monthly range is likely user error.
            return resolve_explicit_paths(raw_paths)

    return resolve_explicit_paths(raw_paths)


def require_columns(df: pd.DataFrame, path: Path) -> None:
    missing = sorted(set(REQUIRED_COLUMNS) - set(df.columns))
    if missing:
        raise ValueError(f"{path} is missing required columns: {missing}")


def looks_like_header_csv(path: Path) -> bool:
    with path.open("r", encoding="utf-8", errors="replace") as file:
        first_line = file.readline().strip().lower()
    return any(name in first_line for name in REQUIRED_COLUMNS) or "quantity" in first_line


def normalize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}

    # Common variants from Binance exports or manual conversions.
    candidates = {
        "timestamp": ["timestamp", "time", "transact_time", "transactTime", "T"],
        "price": ["price", "p"],
        "qty": ["qty", "quantity", "q"],
        "is_buyer_maker": ["is_buyer_maker", "isBuyerMaker", "m"],
        "agg_trade_id": ["agg_trade_id", "aggTradeId", "agg_tradeId", "a"],
    }

    existing_lower = {str(col).lower(): col for col in df.columns}

    for canonical, names in candidates.items():
        if canonical in df.columns:
            continue

        for name in names:
            source = existing_lower.get(name.lower())
            if source is not None:
                rename_map[source] = canonical
                break

    if rename_map:
        df = df.rename(columns=rename_map)

    return df


def read_csv_auto(path: Path, csv_format: str) -> pd.DataFrame:
    if csv_format == "header":
        return pd.read_csv(path)

    if csv_format == "binance-no-header":
        return pd.read_csv(path, header=None, names=BINANCE_AGGTRADES_NO_HEADER_COLUMNS)

    # auto
    if looks_like_header_csv(path):
        df = pd.read_csv(path)
        df = normalize_column_names(df)
        if set(REQUIRED_COLUMNS).issubset(df.columns):
            return df

    # Fallback for official Binance aggTrades CSV without a header.
    return pd.read_csv(path, header=None, names=BINANCE_AGGTRADES_NO_HEADER_COLUMNS)


def normalize_core_dtypes(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    df = normalize_column_names(df)
    require_columns(df, path)

    df = df.copy()

    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="raise").astype("int64")
    df["price"] = pd.to_numeric(df["price"], errors="raise").astype("float64")
    df["qty"] = pd.to_numeric(df["qty"], errors="raise").astype("float64")

    # Handles bools, 0/1, true/false, TRUE/FALSE.
    if df["is_buyer_maker"].dtype == bool:
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
    else:
        text = df["is_buyer_maker"].astype(str).str.strip().str.lower()
        bool_map = {
            "true": True,
            "false": False,
            "1": True,
            "0": False,
            "t": True,
            "f": False,
        }
        mapped = text.map(bool_map)
        if mapped.isna().any():
            bad_values = sorted(text[mapped.isna()].dropna().unique().tolist())
            raise ValueError(f"{path} has invalid is_buyer_maker values: {bad_values[:10]}")
        df["is_buyer_maker"] = mapped.astype(bool)

    if "agg_trade_id" in df.columns:
        df["agg_trade_id"] = pd.to_numeric(df["agg_trade_id"], errors="raise").astype("int64")

    return df


def read_and_normalize(path: Path, csv_format: str) -> pd.DataFrame:
    suffix = path.suffix.lower()

    if suffix == ".csv":
        df = read_csv_auto(path, csv_format=csv_format)
    elif suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        raise ValueError(f"Unsupported input extension for {path}")

    return normalize_core_dtypes(df, path)


def part_output_path(input_path: Path, parts_output_dir: Path) -> Path:
    return parts_output_dir / f"{input_path.stem}.parquet"


def validate_timestamp_order(df: pd.DataFrame, label: str, fail: bool = True) -> bool:
    if len(df) <= 1:
        return True

    timestamps = df["timestamp"].to_numpy()
    is_sorted = not (timestamps[1:] < timestamps[:-1]).any()

    if fail and not is_sorted:
        raise ValueError(f"Timestamp order validation failed for {label}: timestamps are not ascending")

    return is_sorted


def parse_dedupe_subset(raw_subset: str | None) -> list[str] | None:
    if raw_subset is None:
        return None
    subset = [part.strip() for part in raw_subset.split(",") if part.strip()]
    return subset or None


def print_file_summary(df: pd.DataFrame, path: Path) -> None:
    min_ts = pd.to_datetime(df["timestamp"].min(), unit="ms", utc=True)
    max_ts = pd.to_datetime(df["timestamp"].max(), unit="ms", utc=True)

    print(
        f"   rows={len(df):,} | "
        f"min_ts={min_ts} | "
        f"max_ts={max_ts}"
    )


def main() -> None:
    args = parse_args()

    input_paths = resolve_input_paths(args.input, args.input_mode)
    output_path = Path(args.output)
    parts_output_dir = Path(args.parts_output_dir) if args.parts_output_dir else None
    dedupe_subset = parse_dedupe_subset(args.dedupe_subset)

    if output_path.parent:
        os.makedirs(output_path.parent, exist_ok=True)

    if parts_output_dir is not None:
        os.makedirs(parts_output_dir, exist_ok=True)

    print("Resolved input files:")
    for path in input_paths:
        print(f" - {path}")

    frames: list[pd.DataFrame] = []
    previous_max_ts: int | None = None

    for path in input_paths:
        print(f"\nLoading: {path}")
        df = read_and_normalize(path, csv_format=args.csv_format)

        input_sorted = validate_timestamp_order(
            df,
            label=str(path),
            fail=args.fail_on_unsorted_input,
        )
        if not input_sorted:
            print("   warning: input file is not timestamp-sorted; combined output will be sorted later")

        current_min_ts = int(df["timestamp"].min())
        current_max_ts = int(df["timestamp"].max())

        if previous_max_ts is not None and current_min_ts < previous_max_ts:
            print(
                "   warning: this file overlaps or starts before the previous file ends. "
                "Use --drop-duplicates if overlap is expected."
            )

        previous_max_ts = current_max_ts

        print_file_summary(df, path)

        if parts_output_dir is not None:
            part_path = part_output_path(path, parts_output_dir)
            df.sort_values(["timestamp"], kind="mergesort").reset_index(drop=True).to_parquet(
                part_path,
                index=False,
                compression=args.compression,
            )
            print(f"   wrote normalized monthly parquet: {part_path}")

        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)

    print(f"\nCombined rows before duplicate handling: {len(combined):,}")

    if args.drop_duplicates:
        before = len(combined)

        if dedupe_subset is not None:
            missing_dedupe_cols = sorted(set(dedupe_subset) - set(combined.columns))
            if missing_dedupe_cols:
                raise ValueError(f"--dedupe-subset contains missing columns: {missing_dedupe_cols}")
            combined = combined.drop_duplicates(subset=dedupe_subset)
        else:
            combined = combined.drop_duplicates()

        after = len(combined)
        print(f"Dropped duplicates: {before - after:,}")

    sort_columns = ["timestamp"]
    for possible_id_col in ["agg_trade_id", "a", "trade_id", "id"]:
        if possible_id_col in combined.columns:
            sort_columns.append(possible_id_col)
            break

    combined = combined.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)

    if combined["timestamp"].isna().any():
        raise ValueError("Combined dataframe contains null timestamp values")

    validate_timestamp_order(combined, label="combined output", fail=True)

    print("\nCombined output:")
    print(f"rows={len(combined):,}")
    print(f"first timestamp UTC={pd.to_datetime(combined['timestamp'].iloc[0], unit='ms', utc=True)}")
    print(f"last timestamp UTC={pd.to_datetime(combined['timestamp'].iloc[-1], unit='ms', utc=True)}")
    print(f"writing combined parquet to: {output_path}")

    combined.to_parquet(output_path, index=False, compression=args.compression)

    print("Done.")


if __name__ == "__main__":
    main()


# path = Path("/mnt/data/combine_aggtrades_range.py")
# path.write_text(script, encoding="utf-8")
# print(f"Created: {path}")