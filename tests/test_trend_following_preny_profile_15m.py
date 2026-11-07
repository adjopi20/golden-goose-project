from datetime import date

import pytest

from model_development.models.trend_following_preny_profile_15m.legacy.strategy import (
    BAR_MS, PROFILE_WINDOWS, clock_ms, evaluate_session, prepare_aggtrades,
)
from model_development.models.trend_following_preny_profile_15m.legacy.evaluate_plugs import PROFILE_WINDOWS as STUDY_WINDOWS


def profile():
    day = date(2026, 8, 19)
    return dict(session_day=str(day), profile_window="pre_ny",
                profile_start_timestamp_ms=clock_ms(day, 1),
                profile_end_timestamp_ms=clock_ms(day, 9), val=90, poc=100, vah=110)


def bars():
    start = clock_ms(date(2026, 8, 19), 9)
    return [dict(open_timestamp_ms=start+i*BAR_MS, close_timestamp_ms=start+(i+1)*BAR_MS,
                 open=110+i, low=109+i, high=112+i, close=111+i,
                 buy_volume=10, sell_volume=2) for i in range(2)]


def test_preny_only_and_completed_15m_signal():
    assert PROFILE_WINDOWS == STUDY_WINDOWS == ("pre_ny",)
    signals, _ = evaluate_session(profile(), bars(), symbol="BTCUSDC")
    assert len(signals) == 1
    signal = signals[0]
    assert signal["signal_timestamp_ms"] == clock_ms(date(2026, 8, 19), 9, 30)
    assert signal["stop"] == 100
    assert signal["force_exit_timestamp_ms"] == clock_ms(date(2026, 8, 20), 8, 59)


def test_prior24_profile_rejected():
    with pytest.raises(ValueError, match="profile_window"):
        evaluate_session({**profile(), "profile_window": "prior_24h"}, bars(), symbol="BTCUSDC")


def test_hour_candle_not_accepted_as_decision_bar():
    bad = bars()[0]
    bad["close_timestamp_ms"] = bad["open_timestamp_ms"] + 3_600_000
    with pytest.raises(ValueError, match="15 minutes"):
        evaluate_session(profile(), [bad], symbol="BTCUSDC")


def test_no_extra_gate_added():
    # Baseline remains willing to enter with positive delta and positive progress.
    data = bars()
    data[-1].update(close=111.01, open=111, buy_volume=1000, sell_volume=0)
    signals, _ = evaluate_session(profile(), data, symbol="BTCUSDC")
    assert len(signals) == 1


def test_price_only_ignores_delta_but_keeps_price_progress_and_persistence():
    from model_development.models.trend_following_preny_profile_15m.strategy import evaluate_session as maintained

    data = [dict(b, buy_volume=2, sell_volume=10) for b in bars()]
    assert maintained(profile(), data, symbol="HYPEUSDT")[0] == []
    signals, _ = maintained(profile(), data, symbol="HYPEUSDT", require_delta=False)
    assert len(signals) == 1 and signals[0]["stop"] == 100
    assert signals[0]["signal_timestamp_ms"] == clock_ms(date(2026, 8, 19), 9, 30)
    assert not maintained(profile(), data[:1], symbol="HYPEUSDT", require_delta=False)[0]
    data[-1].update(open=110.5, close=111)
    assert not maintained(profile(), data, symbol="HYPEUSDT", require_delta=False)[0]


def test_preparation_excludes_previous_day_and_hourly_output(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    day = date(2026, 8, 19)
    ticks = [dict(timestamp=clock_ms(day, h), price=p, qty=1.,
                  is_buyer_maker=False, agg_trade_id=i)
             for i, (h, p) in enumerate([(0, 999.), (1, 100.), (8, 110.), (9, 112.)])]
    path = tmp_path / "tiny.parquet"
    pq.write_table(pa.Table.from_pylist(ticks), path)
    profiles, observed, hourly = prepare_aggtrades(path, day, day)
    assert set(profiles) == {"pre_ny"}
    assert profiles["pre_ny"][0]["profile_trade_volume"] == 2
    assert len(observed) == 1 and hourly == []
