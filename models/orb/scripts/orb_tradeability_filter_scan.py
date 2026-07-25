from __future__ import annotations

import argparse
import math
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


TRAIN_END = "2025-06-30"
TEST_START = "2025-07-01"


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows._"
    cols = list(df.columns)
    rows = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in df.iterrows():
        rows.append("| " + " | ".join(_fmt(row[col]) for col in cols) + " |")
    return "\n".join(rows)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.6g}"
    return str(value)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["session_date"] = pd.to_datetime(out["session_day"])
    out["breakout_close_follow_through"] = np.where(
        out["direction"].eq("long"),
        out["breakout_close_position_in_range"],
        1.0 - out["breakout_close_position_in_range"],
    )
    out["abs_distance_previous_24h_poc"] = out["distance_to_previous_24h_poc_price"].abs()
    out["failed_lt_1r"] = out["max_favorable_excursion_r"] < 1.0
    return out


def _candidate_filters(train: pd.DataFrame) -> list[tuple[str, str, float]]:
    specs = [
        ("orb_profile_width", "<="),
        ("from_orb_end_to_breakout_range", "<="),
        ("last_15m_range", "<="),
        ("pre_breakout_p95_bubble_count", "<="),
        ("time_from_orb_end_to_breakout_seconds", "<="),
        ("breakout_close_follow_through", ">="),
        ("abs_distance_previous_24h_poc", ">="),
    ]
    filters: list[tuple[str, str, float]] = []
    for col, op in specs:
        values = train[col].dropna()
        if values.empty:
            continue
        quantiles = (0.25, 0.40, 0.50, 0.60, 0.75) if op == "<=" else (0.25, 0.40, 0.50, 0.60, 0.75)
        for q in quantiles:
            value = float(values.quantile(q))
            filters.append((col, op, value))
    return filters


def _mask(df: pd.DataFrame, filters: tuple[tuple[str, str, float], ...]) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, op, value in filters:
        if op == "<=":
            mask &= df[col] <= value
        else:
            mask &= df[col] >= value
    return mask


def _metrics(df: pd.DataFrame, prefix: str, all_hit4: int) -> dict[str, Any]:
    if df.empty:
        return {
            f"{prefix}_samples": 0,
            f"{prefix}_fail_lt_1r_rate": math.nan,
            f"{prefix}_hit_2r_rate": math.nan,
            f"{prefix}_hit_4r_rate": math.nan,
            f"{prefix}_median_mfe_r": math.nan,
            f"{prefix}_hit4_retention": 0.0,
        }
    hit4 = int(df["hit_4r_before_invalidation"].sum())
    return {
        f"{prefix}_samples": int(len(df)),
        f"{prefix}_fail_lt_1r_rate": float(df["failed_lt_1r"].mean()),
        f"{prefix}_hit_2r_rate": float(df["hit_2r_before_invalidation"].mean()),
        f"{prefix}_hit_4r_rate": float(df["hit_4r_before_invalidation"].mean()),
        f"{prefix}_median_mfe_r": float(df["max_favorable_excursion_r"].median()),
        f"{prefix}_hit4_retention": hit4 / all_hit4 if all_hit4 else 0.0,
    }


def _label(filters: tuple[tuple[str, str, float], ...]) -> str:
    if not filters:
        return "BASELINE"
    return " AND ".join(f"{col} {op} {value:.6g}" for col, op, value in filters)


def run(input_path: Path, output_dir: Path, min_train: int, min_test: int) -> pd.DataFrame:
    output_dir.mkdir(parents=True, exist_ok=True)
    df = _prepare(pd.read_parquet(input_path))
    train = df[df["session_date"] <= pd.Timestamp(TRAIN_END)]
    test = df[df["session_date"] >= pd.Timestamp(TEST_START)]
    train_hit4 = int(train["hit_4r_before_invalidation"].sum())
    test_hit4 = int(test["hit_4r_before_invalidation"].sum())

    candidate_sets: list[tuple[tuple[str, str, float], ...]] = [tuple()]
    singles = [(f,) for f in _candidate_filters(train)]
    candidate_sets.extend(singles)
    candidate_sets.extend(tuple(pair) for pair in combinations([s[0] for s in singles], 2))

    rows = []
    for filters in candidate_sets:
        train_kept = train[_mask(train, filters)]
        test_kept = test[_mask(test, filters)]
        if filters and (len(train_kept) < min_train or len(test_kept) < min_test):
            continue
        row = {"filter": _label(filters), "filter_count": len(filters)}
        row.update(_metrics(train_kept, "train", train_hit4))
        row.update(_metrics(test_kept, "test", test_hit4))
        row["train_fail_reduction"] = float(train["failed_lt_1r"].mean() - row["train_fail_lt_1r_rate"])
        row["test_fail_reduction"] = float(test["failed_lt_1r"].mean() - row["test_fail_lt_1r_rate"])
        row["train_score"] = row["train_hit_2r_rate"] * row["train_hit4_retention"] * math.log1p(row["train_samples"])
        rows.append(row)

    result = pd.DataFrame(rows).sort_values(["train_score", "test_hit_2r_rate"], ascending=False)
    result.to_parquet(output_dir / "train_test_filter_scan.parquet", index=False)
    _write_report(output_dir, input_path, result, train, test)
    return result


def _write_report(output_dir: Path, input_path: Path, result: pd.DataFrame, train: pd.DataFrame, test: pd.DataFrame) -> None:
    baseline = result[result["filter"].eq("BASELINE")]
    viable = result[
        (result["filter"] != "BASELINE")
        & (result["train_fail_reduction"] > 0)
        & (result["test_fail_reduction"] > 0)
        & (result["test_hit_2r_rate"] >= baseline["test_hit_2r_rate"].iloc[0])
    ]
    cols = [
        "filter",
        "train_samples",
        "train_fail_lt_1r_rate",
        "train_hit_2r_rate",
        "train_hit_4r_rate",
        "train_hit4_retention",
        "test_samples",
        "test_fail_lt_1r_rate",
        "test_hit_2r_rate",
        "test_hit_4r_rate",
        "test_hit4_retention",
        "test_fail_reduction",
    ]
    lines = [
        "# ORB Tradeability Filter Scan",
        "",
        f"Input: `{input_path}`",
        f"Train: `{train['session_day'].min()}` to `{train['session_day'].max()}`",
        f"Test: `{test['session_day'].min()}` to `{test['session_day'].max()}`",
        "",
        "Selection is ranked by train only. Test columns are out-of-sample evidence, not optimizer inputs.",
        "",
        "## Baseline",
        "",
        _md_table(baseline[cols]),
        "",
        "## Best Train-Ranked Filters",
        "",
        _md_table(result[result["filter"] != "BASELINE"].head(25)[cols]),
        "",
        "## Viable OOS Filters",
        "",
        "Viable means fail rate improves in both train and test, and test hit_2R is at least baseline.",
        "",
        _md_table(viable.head(25)[cols]),
        "",
    ]
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-train", type=int, default=80)
    parser.add_argument("--min-test", type=int, default=50)
    args = parser.parse_args()
    result = run(Path(args.input), Path(args.output_dir), args.min_train, args.min_test)
    print({"filters_tested": int(len(result)), "output_dir": args.output_dir})


if __name__ == "__main__":
    main()
