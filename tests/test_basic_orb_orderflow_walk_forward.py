from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from models.orb.scripts.basic_orb_orderflow_walk_forward import run


def test_walk_forward_flags_only_completed_three_part_failures(tmp_path: Path) -> None:
    observation_dir = tmp_path / "observation"
    output_dir = tmp_path / "walk_forward"
    observation_dir.mkdir()
    samples = []
    path_rows = []
    cases = [
        ("2024-01-10", "loss", 99.0, -0.2),
        ("2024-02-10", "win", 101.0, 0.2),
        ("2024-04-10", "loss", 99.0, -0.2),
        ("2024-05-10", "win", 101.0, 0.2),
    ]
    for day, result, follow_close, directional_delta in cases:
        breakout_time = f"{day}T09:45:10-04:00"
        outcome_time = f"{day}T10:00:00-04:00"
        samples.append(
            {
                "session_day": day,
                "sample": True,
                "direction": "long",
                "result": result,
                "breakout_time": breakout_time,
                "outcome_time": outcome_time,
                "breakout_level": 100.0,
            }
        )
        path_rows.extend(
            [
                {
                    "session_day": day,
                    "breakout_time": breakout_time,
                    "minutes_from_breakout_candle": 0,
                    "is_breakout_candle": True,
                    "orderflow_candle_complete_time": f"{day}T09:46:00-04:00",
                    "open": 100.0,
                    "close": 101.0,
                    "directional_delta_ratio": 0.2,
                    "directional_bubble_qty_imbalance": 0.5,
                    "volume_expansion_ratio": 2.0,
                },
                {
                    "session_day": day,
                    "breakout_time": breakout_time,
                    "minutes_from_breakout_candle": 1,
                    "is_breakout_candle": False,
                    "orderflow_candle_complete_time": f"{day}T09:47:00-04:00",
                    "open": 100.0,
                    "close": follow_close,
                    "directional_delta_ratio": directional_delta,
                    "directional_bubble_qty_imbalance": -0.5 if result == "loss" else 0.5,
                    "volume_expansion_ratio": 0.8 if result == "loss" else 1.2,
                },
            ]
        )

    (observation_dir / "samples.jsonl").write_text(
        "\n".join(json.dumps(row) for row in samples) + "\n", encoding="utf-8"
    )
    pd.DataFrame(path_rows).to_parquet(observation_dir / "orderflow_path.parquet", index=False)

    summary = run(observation_dir, output_dir)
    flags = pd.read_parquet(output_dir / "filter_observations.parquet")

    assert flags["reject_candidate"].tolist() == [True, False, True, False]
    assert summary["folds"] == 1
    assert summary["oos"]["win_rate"] == 0.5
    assert summary["oos"]["kept_win_rate"] == 1.0
    assert summary["oos"]["rejected_losses"] == 1
