from __future__ import annotations

from datetime import date

import pytest

from backtest_engine import (
    calculate_shared_backtest_metrics,
    write_shared_backtest_result,
)


def _trades() -> list[dict[str, object]]:
    return [
        {
            "entry_time": "2026-01-02T14:30:00+00:00",
            "exit_time": "2026-01-02T15:30:00+00:00",
            "pnl": 20.0,
            "gross_pnl_before_costs": 22.0,
            "fees": 1.0,
            "slippage": 1.0,
            "r": 2.0,
        },
        {
            "entry_time": "2026-01-03T14:30:00+00:00",
            "exit_time": "2026-01-03T15:00:00+00:00",
            "pnl": -10.0,
            "gross_pnl_before_costs": -8.0,
            "fees": 1.0,
            "slippage": 1.0,
            "r": -1.0,
        },
    ]


def test_calculate_shared_backtest_metrics() -> None:
    metrics = calculate_shared_backtest_metrics(
        _trades(),
        initial_equity=1_000.0,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 3),
    )

    assert metrics["trades"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["expectancy_r"] == pytest.approx(0.5)
    assert metrics["profit_factor_r"] == pytest.approx(2.0)
    assert metrics["final_equity"] == pytest.approx(1_010.0)
    assert metrics["total_fees"] == pytest.approx(2.0)
    assert metrics["total_slippage"] == pytest.approx(2.0)


def test_write_shared_backtest_result(tmp_path) -> None:
    result = write_shared_backtest_result(
        tmp_path,
        _trades(),
        initial_equity=1_000.0,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 3),
        summary={"strategy": "test"},
        orders=[{"event": "fill"}],
        decisions=[{"decision": "TAKE"}],
    )

    assert result["strategy"] == "test"
    assert result["metrics"]["final_equity"] == pytest.approx(1_010.0)
    assert (tmp_path / "trades.jsonl").exists()
    assert (tmp_path / "paper_orders.jsonl").exists()
    assert (tmp_path / "decisions.jsonl").exists()
    assert (tmp_path / "summary.json").exists()
