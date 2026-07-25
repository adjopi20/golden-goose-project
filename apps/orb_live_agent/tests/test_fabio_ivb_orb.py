from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "orb_live_agent" / "src"))

from orb_live_agent.fast_fabio_ivb_backtest import run


def _ms_ny(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> int:
    dt = datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def test_fabio_ivb_backtest_enters_long_after_orb_and_exits_at_1r(tmp_path: Path) -> None:
    rows = [
        {"timestamp": _ms_ny(2024, 7, 1, 8, 30), "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 8, 35), "price": 101.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 8, 40), "price": 99.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 8, 45), "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 8, 50), "price": 101.0, "qty": 1.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 8, 55), "price": 100.0, "qty": 1.0, "is_buyer_maker": True},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 0), "price": 102.0, "qty": 250.0, "is_buyer_maker": False},
        {"timestamp": _ms_ny(2024, 7, 1, 9, 6), "price": 105.0, "qty": 1.0, "is_buyer_maker": False},
    ]
    input_path = tmp_path / "trades.parquet"
    output_dir = tmp_path / "fabio"
    pd.DataFrame(rows).to_parquet(input_path, index=False)

    summary = run(
        argparse.Namespace(
            input=str(input_path),
            start_date="2024-07-01",
            end_date="2024-07-01",
            output_dir=str(output_dir),
            symbol="TEST",
            timezone="America/New_York",
            orb_start="08:30",
            orb_minutes=30,
            trade_end="14:00",
            delta_threshold=200.0,
            use_cumulative_delta=False,
            cumulative_delta_threshold=500.0,
            tp_rr=1.0,
            qty=1.0,
            initial_equity=1000.0,
            fee_bps=0.0,
            slippage_bps=0.0,
            chunk_days=31,
        )
    )
    trades = [json.loads(line) for line in (output_dir / "trades.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary["trades_taken"] == 1
    assert trades[0]["entry_time"].startswith("2024-07-01T09:05")
    assert trades[0]["exit_time"].startswith("2024-07-01T09:06")
    assert trades[0]["stop_loss"] == 99.0
    assert trades[0]["take_profit"] == 105.0
    assert trades[0]["reason"] == "take_profit_1r"
    assert trades[0]["r"] == 1.0
