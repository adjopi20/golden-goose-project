from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from apps.orb_live_agent.src.orb_live_agent.execution_engine import ExecutionConfig, open_position
from apps.orb_live_agent.src.orb_live_agent.config import load_config
from apps.orb_live_agent.src.orb_live_agent.fast_orb_backtest import _btc_runtime_config, run_btc_orb_with_features
from apps.orb_live_agent.src.orb_live_agent.feature_cache import FeatureSet, build_or_load_feature_set
from models.orb.btc_opening_range_breakout import (
    BTCOpeningRangeBreakoutConfig,
    opening_range_breakout_decision,
)
from scripts.backtest import calculate_shared_backtest_metrics


def _candle(timestamp: datetime, close: float = 100.0, volume: float = 100.0) -> dict:
    return {
        "timestamp_ms": int(timestamp.timestamp() * 1000),
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": volume,
    }


def test_feature_cache_rejects_microsecond_timestamps(tmp_path) -> None:
    day = datetime(2025, 7, 1, tzinfo=timezone.utc).date()
    first_ms = int(datetime(2025, 7, 1, 13, 30, tzinfo=timezone.utc).timestamp() * 1000)
    input_path = tmp_path / "trades.parquet"
    pd.DataFrame(
        {
            "timestamp": [first_ms * 1_000, (first_ms + 60_000) * 1_000],
            "price": [100.0, 101.0],
            "qty": [1.0, 2.0],
            "is_buyer_maker": [False, True],
        }
    ).to_parquet(input_path, index=False)
    config = _btc_runtime_config(load_config(), BTCOpeningRangeBreakoutConfig())

    with pytest.raises(ValueError, match="must all be Unix milliseconds"):
        build_or_load_feature_set(input_path, day, day, config)


def test_btc_orb_signal_metrics_and_spot_cap() -> None:
    start = datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)
    candles = [_candle(start + timedelta(minutes=minute)) for minute in range(106)]
    candles[-1] = _candle(datetime(2024, 1, 2, 13, 45, tzinfo=timezone.utc), close=102.0, volume=160.0)

    decision = opening_range_breakout_decision(candles, BTCOpeningRangeBreakoutConfig())

    assert decision["decision"] == "TAKE"
    assert decision["direction"] == "long"
    assert decision["entry"] > decision["session_vwap"]
    assert decision["volume_ratio"] == pytest.approx(1.6)
    assert decision["stop_loss"] < decision["entry"]

    position, event = open_position(
        direction="long",
        requested_entry=100.0,
        stop_loss=99.9,
        equity=1_000.0,
        risk_fraction=0.01,
        config=ExecutionConfig(long_only_spot=True),
    )
    assert event["event"] == "paper_open"
    assert position is not None
    assert position.qty_total * position.entry + position.fee_paid <= 1_000.0 + 1e-9
    rejected, event = open_position(
        direction="short",
        requested_entry=100.0,
        stop_loss=101.0,
        equity=1_000.0,
        risk_fraction=0.01,
        config=ExecutionConfig(long_only_spot=True),
    )
    assert rejected is None
    assert event["reason"] == "short_not_available_on_spot"

    trades = [
        {
            "entry_time": "2024-01-02T13:46:00+00:00",
            "exit_time": "2024-01-02T14:46:00+00:00",
            "gross_pnl_before_costs": 100.0,
            "fees": 10.0,
            "slippage": 10.0,
            "pnl": 80.0,
            "r": 0.8,
        },
        {
            "entry_time": "2024-01-03T13:46:00+00:00",
            "exit_time": "2024-01-03T14:16:00+00:00",
            "gross_pnl_before_costs": -50.0,
            "fees": 10.0,
            "slippage": 10.0,
            "pnl": -70.0,
            "r": -0.7,
        },
    ]
    metrics = calculate_shared_backtest_metrics(trades, 1_000.0)

    assert metrics["expectancy"] == pytest.approx(5.0)
    assert metrics["average_r_per_trade"] == pytest.approx(0.05)
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["profit_factor"] == pytest.approx(80 / 70)
    assert metrics["average_holding_minutes"] == pytest.approx(45.0)
    assert metrics["gross_edge_before_fees_slippage"] == pytest.approx(25.0)
    assert metrics["net_edge_after_fees_slippage"] == pytest.approx(5.0)
    assert metrics["fee_to_gross_pnl_ratio"] == pytest.approx(0.2)


def test_btc_orb_fast_runner_emits_cost_aware_trade(tmp_path) -> None:
    day = datetime(2024, 1, 2, tzinfo=timezone.utc).date()
    start = datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)
    candles = [_candle(start + timedelta(minutes=minute)) for minute in range(106)]
    candles[-1] = _candle(datetime(2024, 1, 2, 13, 45, tzinfo=timezone.utc), close=102.0, volume=160.0)
    candles.extend(
        [
            {
                **_candle(datetime(2024, 1, 2, 13, 46, tzinfo=timezone.utc), close=103.0),
                "open": 102.0,
                "high": 103.5,
                "low": 101.8,
            },
            {
                **_candle(datetime(2024, 1, 2, 13, 47, tzinfo=timezone.utc), close=100.0),
                "open": 103.0,
                "high": 103.0,
                "low": 99.0,
            },
            _candle(datetime(2024, 1, 2, 13, 48, tzinfo=timezone.utc), close=100.0),
            _candle(
                datetime(2024, 1, 2, 13, 49, tzinfo=timezone.utc),
                close=102.0,
                volume=160.0,
            ),
            {
                **_candle(datetime(2024, 1, 2, 13, 50, tzinfo=timezone.utc), close=103.0),
                "open": 102.0,
                "high": 103.5,
                "low": 101.8,
            },
            {
                **_candle(datetime(2024, 1, 2, 13, 51, tzinfo=timezone.utc), close=100.0),
                "open": 103.0,
                "high": 103.0,
                "low": 99.0,
            },
        ]
    )
    features = FeatureSet(
        rows_loaded=len(candles),
        candles=candles,
        contexts={},
        triggers={},
        force_exit_trades={},
    )

    summary = run_btc_orb_with_features(replace(load_config(), symbol="BTCUSDC"), features, day, day, tmp_path)
    trades = (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()

    assert summary["market"] == "BTCUSDC spot"
    assert summary["trades_taken"] == 1
    assert len(trades) == 1
    assert summary["metrics"]["total_fees"] > 0
    assert summary["metrics"]["total_slippage"] > 0
    assert summary["metrics"]["gross_pnl_before_fees_slippage"] > summary["metrics"]["net_pnl_after_fees_slippage"]
