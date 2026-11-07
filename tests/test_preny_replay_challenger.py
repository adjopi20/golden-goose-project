from datetime import date
import json

import pytest

from model_development.phase_2.backtest import Config
from model_development.models.trend_following_preny_profile_15m.legacy.strategy import clock_ms
from model_development.models.trend_following_preny_profile_15m.legacy.replay_challenger import (
    PLUG, select_signals, write_rows, write_json, run_replay, report,
)


def fixtures():
    limits = dict(directional_delta_imbalance=.1, directional_result_atr=1.)
    signals, predictions = [], []
    for i, day in enumerate((date(2026, 8, 19), date(2026, 8, 20))):
        stamp = clock_ms(day, 9, 30)
        signals.append(dict(sample_id=str(day), symbol="TEST", profile_window="pre_ny",
            session_day=str(day), direction="long", stop=95., entry_reference=100.,
            feature_as_of_ms=stamp, entry_eligible_timestamp_ms=stamp,
            entry_deadline_timestamp_ms=clock_ms(day, 12),
            force_exit_timestamp_ms=clock_ms(date(2026, 8, day.day+1), 8, 59)))
        predictions.append(dict(symbol="TEST", session_day=str(day), fold=1, gate=bool(i),
            thresholds=limits, features=dict(directional_delta_imbalance=.2,
            directional_result_atr=.5 if i == 0 else 2.), mfe_lower_pct=2., mfe_lower_atr=3.,
            baseline_net_r=99999.))
    folds = [dict(fold=1, train_end_exclusive="2026-08-17", test_start="2026-08-18",
                  test_end_exclusive="2026-09-01", thresholds=limits)]
    return signals, predictions, folds


def test_gate_does_not_read_outcomes_or_change_signal():
    signals, predictions, folds = fixtures()
    baseline, kept, _ = select_signals(signals, predictions, folds)
    assert baseline == signals and kept == signals[1:]
    for p in predictions:
        p["baseline_net_r"] = -100000.
        p["mfe_lower_pct"] = None
    assert select_signals(signals, predictions, folds)[:2] == (baseline, kept)


def test_future_train_rejected():
    signals, predictions, folds = fixtures()
    folds[0]["train_end_exclusive"] = "2026-08-19"
    with pytest.raises(ValueError, match="leakage"):
        select_signals(signals, predictions, folds)


def test_missing_feature_keeps_trade():
    signals, predictions, folds = fixtures()
    predictions[0]["features"]["directional_delta_imbalance"] = None
    predictions[0]["gate"] = None
    assert len(select_signals(signals, predictions, folds)[1]) == 2


def test_duplicate_prediction_rejected():
    signals, predictions, folds = fixtures()
    with pytest.raises(ValueError, match="Duplicate prediction"):
        select_signals(signals, predictions + predictions[:1], folds)


def test_shared_raw_replay_reports_and_verified_resume(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    signals, predictions, folds = fixtures()
    baseline, kept, decisions = select_signals(signals, predictions, folds)
    stamps_prices = [(clock_ms(date(2026, 8, 19), 9, 30), 100.),
                     (clock_ms(date(2026, 8, 20), 8, 59), 102.),
                     (clock_ms(date(2026, 8, 20), 9, 30), 100.),
                     (clock_ms(date(2026, 8, 20), 9, 31), 94.),
                     (clock_ms(date(2026, 8, 20), 9, 32), 93.),
                     (clock_ms(date(2026, 8, 21), 8, 59), 100.)]
    raw = tmp_path / "raw.parquet"
    pq.write_table(pa.Table.from_pylist([dict(timestamp=t, price=p, agg_trade_id=i)
                   for i, (t, p) in enumerate(stamps_prices)]), raw)
    config = Config()
    manifest = dict(symbol="TEST", raw=str(raw), start_date="2026-08-19", end_date="2026-08-20",
                    config=dict(initial_equity=1000.))
    write_json(tmp_path / "manifest.json", manifest)
    for label, rows in (("baseline", baseline), ("challenger", kept)):
        write_rows(tmp_path / f"signals/{label}/signals.jsonl", rows)
    write_rows(tmp_path / "decisions.jsonl", decisions)
    run_replay(tmp_path, manifest, config)
    result = report(tmp_path, manifest, 10, 42)
    assert [r["trade_count"] for r in result] == [2, 1]
    assert (tmp_path / "random_summary.json").is_file()
    run_replay(tmp_path, manifest, config)  # verified resume without scanning raw again
    path = tmp_path / "replay/challenger/trades.jsonl"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="receipt"):
        run_replay(tmp_path, manifest, config)
