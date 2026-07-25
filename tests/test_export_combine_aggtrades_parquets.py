import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from utils import export_combine_aggtrades_parquets as combine


def test_combines_out_of_order_files_without_concat(tmp_path: Path, monkeypatch) -> None:
    later = tmp_path / "later.csv"
    earlier = tmp_path / "earlier.csv"
    output = tmp_path / "combined.parquet"
    columns = ["timestamp", "price", "qty", "is_buyer_maker"]

    pd.DataFrame([[1_700_000_000_003, 30, 1, False], [1_700_000_000_004, 40, 1, True]], columns=columns).to_csv(later, index=False)
    pd.DataFrame([[1_700_000_000_001, 10, 1, True], [1_700_000_000_002, 20, 1, False]], columns=columns).to_csv(earlier, index=False)
    monkeypatch.setattr(pd, "concat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("concat called")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["combine", "--input", str(later), str(earlier), "--input-mode", "explicit", "--output", str(output)],
    )

    combine.main()

    assert pd.read_parquet(output)["timestamp"].tolist() == [1_700_000_000_001, 1_700_000_000_002, 1_700_000_000_003, 1_700_000_000_004]


def test_combines_files_when_one_has_is_best_match_extra_column(tmp_path: Path, monkeypatch) -> None:
    with_extra = tmp_path / "with_extra.csv"
    without_extra = tmp_path / "without_extra.csv"
    output = tmp_path / "combined.parquet"

    last_2024_ms = 1_735_689_599_118
    first_2025_ms = 1_735_689_600_095
    pd.DataFrame(
        [[1, 10, 1, 100, 100, last_2024_ms, False, True]],
        columns=["agg_trade_id", "price", "qty", "first_trade_id", "last_trade_id", "timestamp", "is_buyer_maker", "is_best_match"],
    ).to_csv(with_extra, index=False)
    pd.DataFrame(
        [[2, 20, 2, 200, 200, first_2025_ms * 1_000, True]],
        columns=["agg_trade_id", "price", "qty", "first_trade_id", "last_trade_id", "timestamp", "is_buyer_maker"],
    ).to_csv(without_extra, index=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["combine", "--input", str(with_extra), str(without_extra), "--input-mode", "explicit", "--output", str(output)],
    )

    combine.main()

    combined = pd.read_parquet(output)
    assert "is_best_match" not in combined.columns
    assert combined["timestamp"].tolist() == [last_2024_ms, first_2025_ms]


def test_streams_parquet_input_without_pd_read_parquet(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.parquet"
    output = tmp_path / "combined.parquet"
    pd.DataFrame(
        [[1, 10, 1, 100, 100, 1_700_000_001_000, False], [2, 20, 2, 200, 200, 1_700_000_002_000, True]],
        columns=["agg_trade_id", "price", "qty", "first_trade_id", "last_trade_id", "timestamp", "is_buyer_maker"],
    ).to_parquet(source, index=False)
    monkeypatch.setattr(pd, "read_parquet", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read_parquet called")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["combine", "--input", str(source), "--input-mode", "explicit", "--output", str(output)],
    )

    combine.main()

    assert pq.read_table(output).to_pandas()["timestamp"].tolist() == [1_700_000_001_000, 1_700_000_002_000]
