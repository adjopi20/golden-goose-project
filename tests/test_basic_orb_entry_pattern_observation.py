from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from models.orb.scripts.basic_orb_entry_pattern_observation import run


def test_observer_finds_three_no_lookahead_entry_patterns(tmp_path: Path) -> None:
    observation = tmp_path / "observation"
    output = tmp_path / "output"
    observation.mkdir()
    samples = []
    path_rows = []
    scenarios = {
        "2024-07-01": [
            (100.0, 102.0, 99.8, 101.5),
            (101.5, 101.6, 99.8, 100.2),
            (100.2, 101.8, 100.1, 101.7),
            (101.8, 102.0, 101.6, 101.9),
        ],
        "2024-07-02": [
            (100.0, 102.0, 100.1, 101.5),
            (101.5, 101.6, 100.5, 100.8),
            (100.8, 101.8, 100.7, 101.7),
            (101.8, 102.0, 101.6, 101.9),
        ],
    }
    for day, candles in scenarios.items():
        breakout_time = f"{day}T09:45:10-04:00"
        samples.append(
            {
                "session_day": day,
                "sample": True,
                "breakout_time": breakout_time,
                "breakout_level": 100.0,
                "stop_loss": 95.0,
                "risk_abs": 5.0,
                "direction": "long",
                "result": "win",
                "path_end_time": f"{day}T10:00:00-04:00",
                "max_favorable_r_before_invalidation": 3.0,
            }
        )
        for minute, (open_, high, low, close) in enumerate(candles):
            path_rows.append(
                {
                    "session_day": day,
                    "breakout_time": breakout_time,
                    "minutes_from_breakout_candle": minute,
                    "orderflow_candle_start_time": f"{day}T09:{45 + minute:02d}:00-04:00",
                    "orderflow_candle_complete_time": f"{day}T09:{46 + minute:02d}:00-04:00",
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume_expansion_ratio": 3.0 if minute == 0 else 1.0,
                    "directional_delta_ratio": 0.3,
                    "directional_cvd_ratio_30": 0.1,
                    "directional_bubble_qty_imbalance": 0.5,
                }
            )
    (observation / "samples.jsonl").write_text(
        "\n".join(json.dumps(row) for row in samples) + "\n", encoding="utf-8"
    )
    pd.DataFrame(path_rows).to_parquet(observation / "orderflow_path.parquet", index=False)

    run(observation, output)
    rows = pd.read_parquet(output / "entry_pattern_candidates.parquet")

    assert set(rows["pattern"]) == {
        "immediate_expansion_observation",
        "retest_extreme_then_continuation",
        "held_outside_pullback_then_continuation",
    }
    assert (rows["entry_time"] == rows["confirmation_candle_complete_time"]).all()
    assert rows.loc[
        rows["pattern"].eq("immediate_expansion_observation"), "threshold_status"
    ].eq("train_price_and_volume_thresholds_required").all()
