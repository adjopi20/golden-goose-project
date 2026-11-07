from __future__ import annotations

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from model_development.phase_1.legacy_orb.feature_cache import _bubbles, _session_contexts, in_entry_window
from model_development.phase_1.legacy_orb.trigger_observer import observe_triggers
from model_development.models.orb.scripts.basic_orb_1r_observation import _load_orderflow_features, _orderflow_values
from model_development.models.orb.scripts.backtest_avax_pre_ny_profile_room import _cache_manifest


TZ = ZoneInfo("America/New_York")


def _ms(day: int, hour: int, minute: int) -> int:
    return int(datetime(2024, 7, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc).timestamp() * 1000)


def test_cache_exposes_complete_session_profiles_and_p99_orderflow() -> None:
    rows = [
        (_ms(1, 9, 31), 10.0, 1.0, False),
        (_ms(1, 17, 0), 11.0, 2.0, True),
        (_ms(1, 17, 31), 11.0, 3.0, False),
        (_ms(2, 1, 20), 12.0, 4.0, True),
        (_ms(2, 1, 31), 12.0, 5.0, False),
        (_ms(2, 9, 20), 13.0, 6.0, True),
        (_ms(2, 9, 31), 13.0, 7.0, False),
        (_ms(2, 17, 0), 14.0, 100.0, True),
    ]
    trades = pd.DataFrame(rows, columns=["timestamp", "price", "qty", "is_buyer_maker"])
    timestamps = trades["timestamp"].astype(int).tolist()
    config = SimpleNamespace(
        ny_open_time="09:30",
        orb_session_start_time="09:30",
        pre_ny_start_time="01:30",
        overnight_start_time="17:30",
        setup_cutoff_time="17:30",
        volume_profile_bins=10,
        bubble_percentile=0.95,
    )

    context = _session_contexts(trades, timestamps, date(2024, 7, 2), date(2024, 7, 2), config, TZ)[date(2024, 7, 2)]

    assert set(context["session_profiles"]) == {"previous_ny", "before_pre_ny", "pre_ny", "ny"}
    for profile in context["session_profiles"].values():
        assert profile["val"] is not None
        assert profile["vah"] is not None
        assert profile["poc_price"] is not None
        assert "hvn_regions" in profile
        assert "lvn_regions" in profile
        assert len(profile["volume_profile"]) == 10
        assert profile["qty_quantiles"]["p99"] > 0
        assert profile["largest_trade"]["timestamp_ms"] > 0

    assert context["session_profiles"]["ny"]["usable_at_target_ny_open"] is False
    assert context["session_profiles"]["pre_ny"]["usable_at_target_ny_open"] is True

    minute = trades.iloc[-2:].copy()
    bubbles = _bubbles(minute, p95_qty=5.0, p99_qty=50.0)
    candle = {
        "timestamp_ms": _ms(2, 17, 0),
        "open": 13.0,
        "high": 14.0,
        "low": 13.0,
        "close": 14.0,
        "volume": 107.0,
        "delta": -93.0,
        "range": 1.0,
        "body": 1.0,
    }
    observation = observe_triggers(
        {"last_candle": candle, "recent_candles": [candle], **context},
        bubbles,
        context,
    )

    features = observation["orderflow_features"]
    assert features["p99_bubble_count"] == 1
    assert features["p99_sell_bubble_qty"] == 100.0
    assert features["max_bubble_price"] == 14.0
    assert features["candle_body_ratio"] == 1.0


def test_basic_orb_observer_keeps_effort_result_and_p99_fields(tmp_path) -> None:
    candle_ms = _ms(2, 9, 45)
    pd.DataFrame([{
        "timestamp_ms": candle_ms,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.8,
        "volume": 100.0,
        "delta": 30.0,
    }]).to_parquet(tmp_path / "candles_1m.parquet", index=False)
    pd.DataFrame([{
        "snapshot_timestamp_ms": candle_ms,
        "bubble_count": 1,
        "buy_bubble_count": 1,
        "sell_bubble_count": 0,
        "buy_bubble_qty": 20.0,
        "sell_bubble_qty": 0.0,
        "max_bubble_qty": 20.0,
        "max_bubble_side": "buy",
        "range_result_per_volume_effort": 1.4,
        "p99_bubble_count": 1,
        "p99_buy_bubble_count": 1,
        "p99_sell_bubble_count": 0,
        "p99_buy_bubble_qty": 20.0,
        "p99_sell_bubble_qty": 0.0,
        "p99_bubble_delta": 20.0,
        "p99_bubble_delta_ratio": 1.0,
        "p99_buy_price_position": 0.9,
        "max_bubble_price": 10.9,
    }]).to_parquet(tmp_path / "minute_orderflow.parquet", index=False)

    values = _orderflow_values(_load_orderflow_features(tmp_path), candle_ms, "long", TZ)

    assert values["range_result_per_volume_effort"] == 1.4
    assert values["p99_supportive_bubble_qty"] == 20.0
    assert values["directional_p99_bubble_delta_ratio"] == 1.0
    assert values["max_bubble_price"] == 10.9


def test_profile_backtest_accepts_broader_observation_cache(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text(
        """{
          "start_date": "2024-04-01",
          "end_date": "2026-06-30",
          "session_timezone": "America/New_York",
          "orb_session_start_time": "09:30",
          "orb_entry_start_time": "09:30",
          "pre_ny_start_time": "01:30"
        }""",
        encoding="utf-8",
    )

    manifest = _cache_manifest(tmp_path, date(2025, 9, 21), date(2026, 2, 8))

    assert manifest["orb_entry_start_time"] == "09:30"


def test_entry_window_can_continue_after_midnight() -> None:
    target_day, active = in_entry_window(_ms(2, 0, 30), TZ, 960, time(9, 30))
    assert active is True
    assert target_day == date(2024, 7, 1)

    _, active_at_end = in_entry_window(_ms(2, 1, 30), TZ, 960, time(9, 30))
    assert active_at_end is False
