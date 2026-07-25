from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models.orb.scripts.basic_orb_1r_observation import _load_orderflow_features, _orderflow_at_breakout, _orderflow_path, run
from models.orb.scripts.basic_orb_1r_sweep import run as run_sweep


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def test_basic_orb_observes_first_breakout_1r_before_sl(tmp_path: Path) -> None:
    rows = [
        {"timestamp": _ms(2024, 7, 1, 9, 30), "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 44), "price": 110.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 46), "price": 111.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 10, 0), "price": 120.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 2, 9, 30), "price": 200.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 2, 9, 44), "price": 210.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 2, 9, 46), "price": 199.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms(2024, 7, 2, 10, 0), "price": 210.0, "qty": 1.0, "is_buyer_maker": False},
    ]
    input_path = tmp_path / "trades.parquet"
    output_dir = tmp_path / "out"
    pd.DataFrame(rows).to_parquet(input_path, index=False)

    summary = run(
        argparse.Namespace(
            input=str(input_path),
            start_date="2024-07-01",
            end_date="2024-07-02",
            output_dir=str(output_dir),
            timezone="America/New_York",
            orb_start="09:30",
            orb_minutes=15,
            breakout_window_minutes=30,
            outcome_end="04:30",
            risk_model="opposite_extreme",
            bins=10,
            chunk_days=31,
        )
    )
    samples = [json.loads(line) for line in (output_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary["samples"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert samples[0]["direction"] == "long"
    assert samples[0]["target_1r"] == 120.0
    assert samples[0]["result"] == "win"
    assert samples[1]["direction"] == "short"
    assert samples[1]["target_1r"] == 190.0
    assert samples[1]["result"] == "loss"


def test_basic_orb_poc_risk_model_changes_1r_target(tmp_path: Path) -> None:
    rows = [
        {"timestamp": _ms(2024, 7, 1, 9, 30), "price": 100.0, "qty": 10.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 40), "price": 110.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 46), "price": 111.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 10, 0), "price": 120.0, "qty": 1.0, "is_buyer_maker": False},
    ]
    input_path = tmp_path / "trades.parquet"
    output_dir = tmp_path / "out"
    pd.DataFrame(rows).to_parquet(input_path, index=False)

    run(
        argparse.Namespace(
            input=str(input_path),
            start_date="2024-07-01",
            end_date="2024-07-01",
            output_dir=str(output_dir),
            timezone="America/New_York",
            orb_start="09:30",
            orb_minutes=15,
            breakout_window_minutes=30,
            outcome_end="04:30",
            risk_model="poc",
            bins=10,
            chunk_days=31,
        )
    )
    sample = json.loads((output_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()[0])

    assert sample["risk_model"] == "poc"
    assert sample["stop_loss"] == sample["poc_price"]
    assert sample["target_1r"] == sample["breakout_level"] + (sample["breakout_level"] - sample["poc_price"])


def test_basic_orb_sweep_writes_all_requested_variants(tmp_path: Path) -> None:
    rows = [
        {"timestamp": _ms(2024, 7, 1, 8, 30), "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 8, 44), "price": 110.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 8, 46), "price": 111.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 0), "price": 120.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 0), "price": 200.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 14), "price": 210.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 16), "price": 211.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms(2024, 7, 1, 9, 30), "price": 220.0, "qty": 1.0, "is_buyer_maker": False},
    ]
    input_path = tmp_path / "trades.parquet"
    output_dir = tmp_path / "sweep"
    pd.DataFrame(rows).to_parquet(input_path, index=False)

    results = run_sweep(
        argparse.Namespace(
            input=str(input_path),
            start_date="2024-07-01",
            end_date="2024-07-01",
            output_dir=str(output_dir),
            timezone="America/New_York",
            orb_starts="08:30,09:00",
            orb_minutes=15,
            breakout_window_minutes="30,45",
            risk_models="opposite_extreme,poc",
            outcome_end="04:30",
            bins=10,
            chunk_days=31,
        )
    )

    assert len(results) == 8
    assert (output_dir / "sweep_summary.json").exists()
    assert (output_dir / "variant_001_0830_30m_opposite_extreme" / "samples.jsonl").exists()


def test_orderflow_observation_starts_with_completed_breakout_candle(tmp_path: Path) -> None:
    start = _ms(2024, 7, 1, 9, 15)
    candles = pd.DataFrame(
        {
            "timestamp_ms": [start + i * 60_000 for i in range(31)],
            "open": [100.0] * 31,
            "high": [101.0] * 31,
            "low": [99.0] * 31,
            "close": [100.0] * 31,
            "volume": [10.0] * 31,
            "delta": [2.0] * 30 + [-4.0],
        }
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    candles.to_parquet(cache_dir / "candles_1m.parquet", index=False)
    pd.DataFrame(
        {
            "snapshot_timestamp_ms": [start + 30 * 60_000],
            "bubble_count": [2],
            "buy_bubble_count": [2],
            "sell_bubble_count": [0],
            "buy_bubble_qty": [8.0],
            "sell_bubble_qty": [0.0],
            "max_bubble_qty": [5.0],
            "max_bubble_side": ["buy"],
        }
    ).to_parquet(cache_dir / "minute_orderflow.parquet", index=False)

    features = _load_orderflow_features(cache_dir)
    breakout_ts = _ms(2024, 7, 1, 9, 45) + 30_000
    observed = _orderflow_at_breakout(features, breakout_ts, "long", ZoneInfo("America/New_York"))

    assert observed["orderflow_available"] is True
    assert observed["orderflow_candle_start_time"].startswith("2024-07-01T09:45:00")
    assert observed["orderflow_candle_complete_time"].startswith("2024-07-01T09:46:00")
    assert observed["candle_delta_ratio"] == -0.4
    assert observed["cvd_ratio_30"] == 0.18
    assert observed["directional_delta_ratio"] == -0.4
    assert observed["volume_expansion_ratio"] == 1.0
    assert observed["p95_bubble_present"] is True
    assert observed["supportive_bubble_count"] == 2
    assert observed["directional_bubble_qty_imbalance"] == 1.0

    path = _orderflow_path(features, breakout_ts, _ms(2024, 7, 1, 9, 46), "long", ZoneInfo("America/New_York"))
    assert len(path) == 1
    assert path[0]["is_breakout_candle"] is True
    assert path[0]["diagnostic_only_after_raw_breakout"] is True
