from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pandas as pd
from model_development.phase_1.legacy_orb.feature_cache import _bubbles, _minute_triggers
from model_development.phase_1.legacy_orb.trigger_observer import observe_triggers

def test_independent_p99_and_full_session_coverage():
    tz = ZoneInfo('America/New_York')
    def ms(day, hour, minute):
        return int(datetime(2026, 8, day, hour, minute, tzinfo=tz).timestamp()*1000)
    times = [ms(19,9,30), ms(19,12,0), ms(20,1,0)]
    trades = pd.DataFrame([dict(timestamp=t,price=100.,qty=q,is_buyer_maker=s)
        for t in times for q,s in [(3.,False),(5.,True)]])
    candles = [dict(timestamp_ms=t,open=100.,high=101.,low=99.,close=100.,
        volume=8.,delta=-2.,range=2.,body=0.) for t in times]
    context = dict(p95_qty=4.,p99_qty=2.)
    config = SimpleNamespace(ny_open_time='09:30',orb_entry_start_time='09:45',
        orb_entry_window_minutes=30,symbol='BTCUSDT',session_timezone='America/New_York')
    result = _minute_triggers(trades, trades.timestamp.tolist(), candles,
        {date(2026,8,19):context},config,tz)
    assert set(result)==set(times)
    for t in times:
        f=result[t]['orderflow_features']
        assert f['bubble_count']==1
        assert f['p99_bubble_count']==2
        assert f['p99_buy_bubble_qty']==3.
        assert f['p99_sell_bubble_qty']==5.
        assert not result[t]['setup_observation_active']
    primary_missing = _bubbles(trades.iloc[:2],None,2.)
    f=observe_triggers({'last_candle':candles[0],**context},primary_missing)['orderflow_features']
    assert f['bubble_count']==0 and f['p99_bubble_count']==2

def test_repair_keeps_source_and_profiles(tmp_path):
    import json
    from model_development.phase_1.legacy_orb.feature_cache import repair_orderflow
    tz = ZoneInfo('America/New_York')
    t = int(datetime(2026,8,19,12,tzinfo=tz).timestamp()*1000)
    src = tmp_path/'source'
    src.mkdir()
    raw = tmp_path/'raw.parquet'
    pd.DataFrame([dict(timestamp=t,price=100.,qty=3.,is_buyer_maker=False)]).to_parquet(raw)
    pd.DataFrame([dict(timestamp_ms=t,symbol='BTCUSDT',open=100.,high=101.,low=99.,close=100.,volume=3.,delta=3.,range=2.,body=0.)]).to_parquet(src/'candles_1m.parquet')
    (src/'session_contexts.json').write_text(json.dumps({'2026-08-19':{'p95_qty':4.,'p99_qty':2.}}))
    (src/'force_exit_trades.json').write_text('{}')
    (src/'manifest.json').write_text(json.dumps(dict(session_timezone='America/New_York',ny_open_time='09:30',orb_entry_start_time='09:45')))
    before = {p.name:p.read_bytes() for p in src.iterdir()}
    out = tmp_path/'repaired'
    repair_orderflow(raw,src,out)
    assert before == {p.name:p.read_bytes() for p in src.iterdir()}
    assert (out/'session_contexts.json').read_bytes()==before['session_contexts.json']
    f = pd.read_parquet(out/'minute_orderflow.parquet').iloc[0]
    assert f['p99_bubble_count']==1 and f['bubble_count']==0
    assert json.loads((out/'manifest.json').read_text())['orderflow_revision']==3
