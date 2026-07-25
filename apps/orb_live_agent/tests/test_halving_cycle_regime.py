from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "orb_live_agent" / "src"))

from orb_live_agent.regime.halving_cycle import classify_halving_cycle


def test_halving_cycle_labels_simple_weekly_phases() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-05-17", "2021-05-16", "2024-04-21", "2025-04-20"], utc=True),
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )

    regimes = classify_halving_cycle(daily)

    assert regimes["halving_cycle"].to_list() == [
        "2020-05-11_halving_cycle",
        "2020-05-11_halving_cycle",
        "2024-04-20_halving_cycle",
        "2024-04-20_halving_cycle",
    ]
    assert regimes["halving_phase"].to_list() == [
        "post_halving_0_6m",
        "cycle_year_2",
        "post_halving_0_6m",
        "cycle_year_2",
    ]
    assert regimes["observer_only"].all()
