from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "orb_live_agent" / "src"))

from orb_live_agent.regime.slow_cycle import compute_market_cycle_regimes


def test_slow_cycle_regime_is_persistent_and_observer_only() -> None:
    dates = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
    close = [100 + i for i in range(120)]
    daily = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [v + 1 for v in close],
            "low": [v - 1 for v in close],
            "close": close,
        }
    )

    regimes = compute_market_cycle_regimes(
        daily,
        {
            "persistence_weeks": 2,
            "sma_days": 10,
            "sma_slope_days": 3,
            "adx_days": 5,
            "z_days": 20,
        },
    )

    assert any(value.endswith("_bull_trend") for value in regimes["structure_raw"].to_list())
    assert any(value.endswith("_bull_trend") for value in regimes["structure"].to_list())
    assert regimes["market_cycle"].eq("not_classified").all()
    assert regimes["observer_only"].all()
    assert "take_trade" not in regimes
