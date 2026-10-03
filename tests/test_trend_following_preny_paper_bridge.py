from datetime import date, timedelta
import json

import pytest

from backtest_engine.replay_aggtrades import Config
from live_engine.order_manager import PaperOrderManager
from live_engine.market_data import MarketData
from live_engine.state_store import StateStore
from models.trend_following_preny_profile_15m.paper_bridge import (
    NotReady, evaluate_completed_bar, submit_completed_bar)
from models.trend_following_preny_profile_15m.strategy import BAR_MS, clock_ms
from models.trend_following_preny_profile_15m.paper_worker import paper_activation_signal
from models.trend_following_preny_profile_15m.calibrate_c1_native import calibrate


DAY = date(2026, 8, 19)
START = clock_ms(DAY, 9)


def profile():
    return dict(session_day=str(DAY), profile_window="pre_ny",
                profile_start_timestamp_ms=clock_ms(DAY, 1),
                profile_end_timestamp_ms=START, val=90., poc=100., vah=110.)


def bars():
    return [dict(open_timestamp_ms=START+i*BAR_MS, close_timestamp_ms=START+(i+1)*BAR_MS,
                 open=110.+i, low=109.+i, high=112.+i, close=111.+i,
                 buy_volume=10., sell_volume=2.) for i in range(2)]


def refs():
    return dict(through_session_day=str(DAY-timedelta(days=1)),
                slots={str(i):dict(prior_sessions=20, volume=20., buy_volume=5., sell_volume=5., body=.5)
                       for i in range(12)})


def trend(ema=.6, adx=31.):
    return dict(status="ready", end_ms=START+2*BAR_MS,
                price_vs_ema200_pct=ema, adx14=adx)


def evaluate(symbol="ETHUSDC", **overrides):
    inputs = dict(symbol=symbol, profile=profile(), bars=bars(), as_of_ms=START+2*BAR_MS,
                  references=refs(), atr_before={START+BAR_MS:2.}, trend=trend())
    inputs.update(overrides)
    return evaluate_completed_bar(**inputs)


def test_wait_before_first_confirmation_and_c2_entry():
    wait = evaluate(bars=bars()[:1], as_of_ms=START+BAR_MS)
    assert wait["state"] == "WAIT" and wait["signal"] is None
    ready = evaluate()
    assert ready["state"] == "ENTRY_READY"
    assert ready["signal"]["feature_as_of_ms"] == START+2*BAR_MS
    assert ready["risk_fraction"] == .005
    assert "initiative" in ready["snapshot"]["c2"]["directions"]["long"]["labels"]


def test_sizing_tiers_do_not_change_entry():
    assert evaluate(trend=trend(ema=.4))["risk_fraction"] == .0025
    assert evaluate("BNBUSDC", trend=trend(adx=29.))["risk_fraction"] == .0025
    assert evaluate("BNBUSDC", trend=trend(adx=31.))["risk_fraction"] == .005


def test_future_or_missing_inputs_fail_closed():
    with pytest.raises(ValueError, match="Future bar"):
        evaluate(bars=bars()+[dict(bars()[1], open_timestamp_ms=START+2*BAR_MS,
                                   close_timestamp_ms=START+3*BAR_MS)])
    with pytest.raises(ValueError, match="references include"):
        evaluate(references={**refs(), "through_session_day":str(DAY)})
    with pytest.raises(NotReady, match="trend snapshot"):
        evaluate(trend={**trend(), "end_ms":START+BAR_MS})
    with pytest.raises(NotReady, match="same-clock"):
        evaluate(references={**refs(), "slots":{}})
    with pytest.raises(ValueError, match="Future ATR"):
        evaluate(atr_before={START+2*BAR_MS:2.})


def test_hype_c1_frozen_calibration_and_expiry():
    artifact = dict(valid_from="2026-08-01", valid_to_exclusive="2026-09-01",
                    trained_through="2026-07-28", thresholds={
                        "directional_delta_imbalance":.5,"directional_result_atr":.5})
    assert evaluate("HYPEUSDT", c1_calibration=artifact)["selected"]
    bad = {**artifact,"thresholds":{**artifact["thresholds"],"directional_result_atr":2.}}
    assert evaluate("HYPEUSDT", c1_calibration=bad)["state"] == "SELECTOR_REJECTED"
    with pytest.raises(NotReady, match="not valid"):
        evaluate("HYPEUSDT", c1_calibration={**artifact,"valid_to_exclusive":"2026-08-19"})
    with pytest.raises(NotReady, match="missing"):
        evaluate("HYPEUSDT")


def test_hype_uses_completed_signal_time_atr():
    artifact = dict(valid_from="2026-08-01", valid_to_exclusive="2026-09-01",
                    trained_through="2026-07-28", thresholds={
                        "directional_delta_imbalance": .5, "directional_result_atr": .75})
    old_atr = evaluate("HYPEUSDT", c1_calibration=artifact)
    current_atr = evaluate("HYPEUSDT", c1_calibration=artifact,
                           signal_atr=dict(end_ms=START, atr14=4.))
    assert old_atr["selected"]
    assert not current_atr["selected"]
    assert current_atr["snapshot"]["c1_features"]["directional_result_atr"] == .5
    with pytest.raises(NotReady, match="signal-time"):
        evaluate("HYPEUSDT", c1_calibration=artifact,
                 signal_atr=dict(end_ms=START+3*BAR_MS, atr14=4.))


def test_paper_activation_waits_for_computation_and_respects_deadline():
    candidate = dict(feature_as_of_ms=START+2*BAR_MS,
                     entry_eligible_timestamp_ms=START+2*BAR_MS,
                     entry_deadline_timestamp_ms=START+3*BAR_MS)
    activated = paper_activation_signal(candidate, START+2*BAR_MS+20_000)
    assert activated["entry_eligible_timestamp_ms"] == START+2*BAR_MS+20_000
    assert activated["entry_deadline_timestamp_ms"] == START+2*BAR_MS+300_000
    assert candidate["entry_eligible_timestamp_ms"] == START+2*BAR_MS
    with pytest.raises(NotReady, match="deadline"):
        paper_activation_signal(candidate, START+2*BAR_MS+300_001)


def test_lighter_c1_calibration_uses_only_prior_native_candidates(tmp_path):
    path = tmp_path/"lighter.db"
    store = StateStore(path, "lighter")
    MarketData(store)
    for i in range(25):
        day = date(2026, 9, 7)+timedelta(days=i)
        asof = clock_ms(day, 9, 30)
        row = dict(session_day=str(day), as_of_ms=asof, candidate=True,
                   c1_candidate_features={"directional_delta_imbalance":.1+i*.001,
                                          "directional_result_atr":.5+i*.01})
        store.db.execute("INSERT INTO evaluations VALUES (?,?,?)",
                         ("HYPEUSDT", asof, json.dumps(row)))
    store.close()
    artifact = calibrate(path, "lighter", date(2026,10,2))
    assert artifact["candidate_count"] == 25
    assert artifact["trained_through"] == "2026-10-01"
    assert artifact["thresholds"]["directional_delta_imbalance"] == pytest.approx(.112)
    assert artifact["thresholds"]["directional_result_atr"] == pytest.approx(.62)
    with pytest.raises(ValueError, match="venue"):
        calibrate(path, "binance", date(2026,10,2))
    with pytest.raises(ValueError, match="Need 26"):
        calibrate(path, "lighter", date(2026,10,2), min_candidates=26)


def test_bridge_persists_first_candidate_and_no_duplicate_fill(tmp_path):
    path = tmp_path/"paper.db"
    store = StateStore(path, "binance")
    manager = PaperOrderManager(store)
    manager.register("tf-preny-eth-v1", "ETHUSDC", Config(tp1_r=1.,tp1_fraction=.1),
                     allowed_risks=[.0025,.005], contract="TF-PRENY-ETH-001")
    result = submit_completed_bar(manager, symbol="ETHUSDC", profile=profile(), bars=bars(),
                                  as_of_ms=START+2*BAR_MS, references=refs(),
                                  atr_before={START+BAR_MS:2.}, trend=trend())
    assert result["paper_result"] == "pending"
    store.close()
    store = StateStore(path, "binance")
    manager = PaperOrderManager(store)
    assert submit_completed_bar(manager, symbol="ETHUSDC", profile=profile(), bars=bars(),
                                as_of_ms=START+2*BAR_MS, references=refs(),
                                atr_before={START+BAR_MS:2.}, trend=trend())["paper_result"] == "duplicate"
    manager.tick("tf-preny-eth-v1", dict(timestamp_ms=START+2*BAR_MS,
                                         agg_trade_id=1, price=112.))
    assert manager.status()[0]["position"]["actual_initial_risk"] == pytest.approx(5.)
    assert manager.tick("tf-preny-eth-v1", dict(timestamp_ms=START+2*BAR_MS,
                                                 agg_trade_id=1, price=112.)) == "duplicate"
    store.close()


def test_rejected_first_candidate_blocks_later_session_reentry(tmp_path):
    store = StateStore(tmp_path/"paper.db", "binance")
    manager = PaperOrderManager(store)
    manager.register("tf-preny-hype-v1", "HYPEUSDT", Config(tp1_r=1.,tp1_fraction=.1))
    artifact = dict(valid_from="2026-08-01", valid_to_exclusive="2026-09-01",
                    trained_through="2026-07-28", thresholds={
                        "directional_delta_imbalance":.5,"directional_result_atr":2.})
    base = dict(symbol="HYPEUSDT", profile=profile(), bars=bars(),
                as_of_ms=START+2*BAR_MS, references=refs(),
                atr_before={START+BAR_MS:2.}, c1_calibration=artifact)
    result = submit_completed_bar(manager, **base)
    assert result["paper_result"] == "selector_rejected"
    later = dict(bars()[1], open_timestamp_ms=START+2*BAR_MS,
                 close_timestamp_ms=START+3*BAR_MS, open=112., close=113., low=111., high=114.)
    assert submit_completed_bar(manager, **{**base,"bars":bars()+[later],
                     "as_of_ms":START+3*BAR_MS})["paper_result"] == "session_locked"
    assert manager.status()[0]["pending_candidates"] == 0
    store.close()
