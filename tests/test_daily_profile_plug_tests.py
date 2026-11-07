from datetime import date, timedelta

from model_development.models.trend_following_preny_profile_15m.legacy.evaluate_plugs import (
    _gate,
    _same_clock_relative_volume,
    evaluate,
)


def _bar(day, quarter, volume):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    hour, minute = divmod(quarter * 15, 60)
    stamp = int(datetime.fromisoformat(day).replace(hour=hour, minute=minute,
                                                   tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    return {"open_timestamp_ms": stamp, "buy_volume": volume / 2,
            "sell_volume": volume / 2}


def test_relative_volume_never_uses_future_sessions():
    start = date(2025, 1, 1)
    days = [str(start + timedelta(days=i)) for i in range(12)]
    bars = {day: [_bar(day, 36, 10)] for day in days}
    bars[days[-1]] = [_bar(days[-1], 36, 1_000_000)]
    assert _same_clock_relative_volume(days[10], [_bar(days[10], 36, 20)],
                                       bars, days) == 2.0


def test_walk_forward_threshold_is_train_only_and_embargoed():
    start = date(2025, 1, 1)
    rows = []
    for i in range(33):
        day = str(start + timedelta(days=i))
        score = float(i + 1 if i < 30 else 1_000_000 if i == 30 else 5 if i == 31 else 25)
        rows.append({"symbol": "TESTUSDT", "profile_window": "pre_ny",
                     "session_day": day, "net_r": -1.0, "mfe_lower_pct": 0.1,
                     "mfe_lower_atr": 0.1,
                     "features": {"vwap_persistence": score,
                                  "value_edge_acceptance": score,
                                  "same_clock_relative_volume": score,
                                  "directional_result_atr": score,
                                  "directional_delta_imbalance": score,
                                  "atr_activation": score,
                                  "fee_stop_burden": score}})
    predictions, folds = evaluate(rows, train_days=30, test_days=10,
                                  embargo_days=1, min_train_trades=25)
    vwap = [row for row in predictions if row["plug"] == "vwap_persistence"]
    assert [row["session_day"] for row in vwap] == [rows[31]["session_day"], rows[32]["session_day"]]
    assert [row["gate"] for row in vwap] == [False, True]
    assert all(row["thresholds"]["vwap_persistence"] == 15.5 for row in vwap)
    assert len(folds) == 6


def test_missing_feature_does_not_reject_baseline():
    assert _gate({"vwap_persistence": None}, "vwap_persistence",
                 {"vwap_persistence": 0.5}) is None
    assert _gate({"same_clock_relative_volume": 2.0,
                  "directional_result_atr": 0.1}, "volume_without_result",
                 {"same_clock_relative_volume": 1.5,
                  "directional_result_atr": 0.5}) is False
