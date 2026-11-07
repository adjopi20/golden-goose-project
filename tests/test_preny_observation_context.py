import json

import numpy as np
import pandas as pd
import pytest

from model_development.models.trend_following_preny_profile_15m.legacy import evaluate_plugs as plugs
from model_development.models.trend_following_preny_profile_15m.legacy.audit_opportunity import (
    trend_indicators_15m, trend_snapshot_15m,
)


def minutes(count=230, flat=False):
    prices = np.repeat(np.full(count, 100.) if flat else 100. + np.arange(count), 15)
    return pd.DataFrame({"timestamp_ms": np.arange(len(prices)) * 60_000,
                         "open": prices, "high": prices, "low": prices,
                         "close": prices, "volume": 1., "delta": .5})


def test_sma_ema_seed_and_direction():
    frame = trend_indicators_15m(minutes())
    assert pd.isna(frame.iloc[198].ema200)
    assert frame.iloc[199].ema200 == pytest.approx(199.5)
    assert frame.iloc[200].ema200 == pytest.approx(200.5)
    assert frame.iloc[199].sma20 == pytest.approx(289.5)
    row = trend_snapshot_15m(frame, 230 * 900_000, "long")
    assert row["status"] == "ready"
    assert row["adx14"] == pytest.approx(100.)
    assert row["ma_structure"] == "bullish_stack"
    assert row["trade_alignment"] == "aligned"
    assert trend_snapshot_15m(frame, 230 * 900_000, "short")["trade_alignment"] == "opposed"


def test_flat_adx_zero_and_warmup():
    frame = trend_indicators_15m(minutes(flat=True))
    row = trend_snapshot_15m(frame, 230 * 900_000, "long")
    assert row["adx14"] == 0
    assert row["ma_structure"] == "mixed"
    assert trend_snapshot_15m(frame, 30 * 900_000, "long")["status"] == "warmup"


def test_future_and_unfinished_bars_do_not_change_snapshot():
    data = minutes()
    cutoff = 215 * 900_000
    original = trend_snapshot_15m(trend_indicators_15m(data), cutoff, "long")
    data.loc[data.timestamp_ms >= cutoff, ["open", "high", "low", "close"]] *= 100
    changed = trend_snapshot_15m(trend_indicators_15m(data), cutoff, "long")
    truncated = trend_snapshot_15m(trend_indicators_15m(data[data.timestamp_ms < cutoff]), cutoff, "long")
    assert original == changed == truncated
    assert trend_snapshot_15m(trend_indicators_15m(data), cutoff + 899_999, "long") == original


def test_full_gap_resets_partial_gap_keeps_coverage():
    data = minutes(450)
    partial = trend_indicators_15m(data.drop(index=215 * 15))
    assert partial.iloc[215].minute_coverage == pytest.approx(14 / 15)
    assert pd.notna(partial.iloc[215].ema200)
    gapped = trend_indicators_15m(data.drop(index=range(215 * 15, 216 * 15)))
    assert trend_snapshot_15m(gapped, 216 * 900_000, "long")["status"] == "missing_completed_bar"
    assert trend_snapshot_15m(gapped, 217 * 900_000, "long")["status"] == "warmup"
    assert trend_snapshot_15m(gapped, 450 * 900_000, "long")["status"] == "ready"


def test_context_export_preserves_features_outcomes_gates(tmp_path, monkeypatch):
    cache = tmp_path / "minute.parquet"
    minutes().to_parquet(cache)
    manifest = tmp_path / "study.json"
    manifest.write_text(json.dumps({"datasets": [{"symbol": "TEST", "observation_dir": "unused",
                                                 "audit_dir": "unused", "fee_bps": 4}]}))
    features = {"vwap_persistence": .5, "atr_activation": 1.1}
    row = dict(symbol="TEST", profile_window="pre_ny", session_day="2025-01-01", net_r=-1.,
               features=features, sample_id="test", direction="long", feature_as_of_ms=230*900_000,
               entry_ms=230*900_000, entry_price=330, stop=300,
               audit_context={"atr14_1h": 20}, profile={"poc": 300}, path_outcome={"mfe_before_stop_lower_pct": .5})
    monkeypatch.setattr(plugs, "load_dataset", lambda *args: {"pre_ny": [row]})
    previous = dict(symbol="TEST", profile_window="pre_ny", session_day="2025-01-01",
                    features=features, baseline_net_r=-1., plug="delta_without_result", gate=False,
                    fold=1, thresholds={"directional_delta_imbalance": .2})
    (tmp_path / "predictions.jsonl").write_text(json.dumps(previous) + "\n")
    out = tmp_path / "output"
    result = plugs.export_context(manifest, [["TEST", str(cache)]], out)
    record = json.loads((out / "candidate_context.jsonl").read_text())
    assert record["features"] == features
    assert record["research_gates"]["delta_without_result"]["gate"] is False
    assert record["outcomes"]["net_r"] == -1
    assert record["audit_context"]["atr14_1h"] == 20
    assert record["trend_15m"]["status"] == "ready"
    assert result["coverage"][0]["candidates"] == 1
    assert (out / "candidate_context.csv").exists()
    with pytest.raises(FileExistsError):
        plugs.export_context(manifest, [["TEST", str(cache)]], out)
    previous["features"] = {"changed": 1}
    (tmp_path / "predictions.jsonl").write_text(json.dumps(previous) + "\n")
    with pytest.raises(ValueError, match="inputs changed"):
        plugs.export_context(manifest, [["TEST", str(cache)]], tmp_path / "invalid")


def test_context_cache_symbol_mismatch_fails(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"datasets": [{"symbol": "TEST"}]}))
    with pytest.raises(ValueError, match="Cache symbols"):
        plugs.export_context(path, [], tmp_path / "output")
