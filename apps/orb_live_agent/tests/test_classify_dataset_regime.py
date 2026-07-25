from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "orb_live_agent" / "src"))

from orb_live_agent.regime.classify_dataset import classify_aggtrades


def test_classify_aggtrades_writes_observer_regime_file(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=260, freq="D", tz="UTC")
    rows = [
        {"timestamp": int((day + pd.Timedelta(hours=23)).timestamp() * 1000), "price": 100.0 + i}
        for i, day in enumerate(dates)
    ]
    input_path = tmp_path / "trades.parquet"
    output_path = tmp_path / "regimes.parquet"
    pd.DataFrame(rows).to_parquet(input_path, index=False)

    regimes = classify_aggtrades([input_path], output_path)

    assert output_path.exists()
    assert not regimes.empty
    assert regimes["observer_only"].all()
    assert {"market_cycle", "structure", "adx", "sma200", "gmma_spread", "z_score"}.issubset(regimes.columns)
