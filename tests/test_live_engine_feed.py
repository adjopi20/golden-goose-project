from datetime import date, timedelta
import json

import pandas as pd
import pytest

from backtest_engine.replay_aggtrades import Config
from live_engine.adapters.binance import BinancePublic, UnrecoverableGap, normalize
from live_engine.adapters.lighter import normalize as lighter_normalize
from live_engine.market_data import MarketData, MINUTE, QUARTER
from live_engine.order_manager import PaperOrderManager
from live_engine.state_store import StateStore, encode
from models.trend_following_preny_profile_15m.paper_bridge import evaluate_completed_bar
from models.trend_following_preny_profile_15m.paper_worker import (
    Worker, make_profile, profile_key, quarter_bars, prepare_context)
from models.trend_following_preny_profile_15m.strategy import clock_ms, prepare_aggtrades


def tick(i, stamp, price=100., buy=True):
    return dict(symbol="ETHUSDC", agg_trade_id=i, timestamp_ms=stamp, price=price, quantity=2., buy=buy)


def test_side_normalization_and_validation():
    r = dict(a=1,T=1000,p="100",q="2",m=True)
    assert normalize(r,"ETHUSDC")["buy"] is False
    assert normalize({**r,"m":False},"ETHUSDC")["buy"] is True
    with pytest.raises(ValueError): normalize({**r,"q":"nan"},"ETHUSDC")
    lr = dict(market_id=0,trade_id=2,timestamp=1000,price="100",size="2",type="trade",is_maker_ask=True)
    assert lighter_normalize(lr,0)["buy"] is True
    assert lighter_normalize({**lr,"type":"liquidation"},0) is None


def test_repair_is_sequential_and_bounded():
    api = BinancePublic()
    api.get = lambda *a,**kw:[dict(a=i,T=1000+i,p="100",q="2",m=False) for i in range(2,6)]
    assert [r["agg_trade_id"] for r in api.repair("ETHUSDC",tick(1,1001),tick(5,1005))] == [2,3,4]
    api.get = lambda *a,**kw:[]
    with pytest.raises(UnrecoverableGap): list(api.repair("ETHUSDC",tick(1,1001),tick(5,1005)))
    with pytest.raises(UnrecoverableGap): list(api.repair("ETHUSDC",tick(1,0),tick(5,48*3_600_000)))


def test_incremental_bars_restart_duplicate_and_gap(tmp_path):
    path = tmp_path/"feed.db"
    store = StateStore(path,"binance")
    data = MarketData(store)
    # First minute is partial and deliberately excluded.
    data.consume("ETHUSDC",[tick(1,1),tick(2,MINUTE,101),tick(3,MINUTE+1000,99,False),tick(4,2*MINUTE,102)])
    bar = json.loads(store.db.execute("SELECT payload FROM minutes").fetchone()[0])
    assert (bar["open"],bar["high"],bar["low"],bar["close"],bar["volume"],bar["delta"]) == (101,101,99,99,4,0)
    state = data.state("ETHUSDC")
    store.close()
    store = StateStore(path,"binance"); data = MarketData(store)
    data.consume("ETHUSDC",[tick(4,2*MINUTE,102)])
    assert data.state("ETHUSDC") == state
    with pytest.raises(ValueError,match="ID gap"): data.consume("ETHUSDC",[tick(6,3*MINUTE)])
    assert data.state("ETHUSDC") == state
    with pytest.raises(ValueError,match="Conflicting"): data.consume("ETHUSDC",[tick(4,2*MINUTE,105)])
    store.close()


def test_atomic_callback_rollback_and_order_before_new_bar(tmp_path):
    store = StateStore(tmp_path/"paper.db","binance")
    manager = PaperOrderManager(store)
    manager.register("a","ETHUSDC",Config(tp1_r=1,tp1_fraction=.1))
    data = MarketData(store,manager)
    data.consume("ETHUSDC",[tick(1,QUARTER-1000)],account="a")
    def callback(symbol,end,state):
        signal = dict(sample_id="s",session_day="2026-08-01",direction="long",symbol=symbol,
                      stop=90.,entry_reference=100.,feature_as_of_ms=end,entry_eligible_timestamp_ms=end,
                      entry_deadline_timestamp_ms=end+10000,force_exit_timestamp_ms=end+100000)
        manager.submit("a",signal,snapshot={"feature_as_of_ms":end},risk_fraction=.005)
    def broken(*a):
        callback(*a)
        raise RuntimeError("test rollback")
    with pytest.raises(RuntimeError):
        data.consume("ETHUSDC",[tick(2,QUARTER)],account="a",on_boundary=broken)
    assert store.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
    assert data.state("ETHUSDC")["cursor"]["agg_trade_id"] == 1
    data.consume("ETHUSDC",[tick(2,QUARTER)],account="a",on_boundary=callback)
    assert manager.status()[0]["position"]["actual_initial_risk"] == pytest.approx(5.)
    assert manager.status()[0]["last_trade"]["agg_trade_id"] == 2
    store.close()


def test_batch_execution_matches_single_ticks(tmp_path):
    states=[]; journals=[]
    for batched in (False,True):
        s=StateStore(tmp_path/f"{batched}.db","binance"); m=PaperOrderManager(s)
        m.register("a","ETHUSDC",Config(tp1_r=1,tp1_fraction=.1))
        signal=dict(sample_id="s",session_day="2026-08-01",direction="long",symbol="ETHUSDC",
            stop=90.,entry_reference=100.,feature_as_of_ms=1,entry_eligible_timestamp_ms=1,
            entry_deadline_timestamp_ms=50,force_exit_timestamp_ms=100)
        m.submit("a",signal,snapshot={"feature_as_of_ms":1},risk_fraction=.005)
        ticks=[tick(i,i,p) for i,p in enumerate([100,110,111,89,88],1)]
        if batched: m.ticks("a",ticks)
        else:
            for t in ticks:m.tick("a",t)
        states.append(m.status())
        journals.append(sorted((r[0],r[1]) for r in s.db.execute("SELECT kind,payload FROM journal")))
        s.close()
    assert states[0]==states[1]
    assert journals[0]==journals[1]


def test_lighter_nonconsecutive_trade_ids_recover_partial_and_time_exit(tmp_path):
    path = tmp_path/"lighter.db"
    store = StateStore(path, "lighter")
    manager = PaperOrderManager(store)
    manager.register("a", "ETHUSDC", Config(tp1_r=1, tp1_fraction=.1))
    data = MarketData(store, manager)
    signal = dict(sample_id="lighter-one", session_day="2026-08-01", symbol="ETHUSDC",
                  direction="long", stop=90., entry_reference=100., feature_as_of_ms=1000,
                  entry_eligible_timestamp_ms=1000, entry_deadline_timestamp_ms=5000,
                  force_exit_timestamp_ms=10000)
    manager.submit("a", signal, snapshot={"feature_as_of_ms":1000}, risk_fraction=.005)
    data.consume("ETHUSDC", [tick(100,1000,100.), tick(120,1500,111.),
                             tick(150,1600,112.)], account="a", consecutive_ids=False)
    assert manager.status()[0]["position"]["tp1_filled"]
    assert len([row for row in store.db.execute("SELECT kind FROM journal") if row[0]=="fill"]) == 2
    store.close()
    store = StateStore(path, "lighter")
    manager = PaperOrderManager(store)
    data = MarketData(store, manager)
    data.consume("ETHUSDC", [tick(150,1600,112.), tick(500,10000,105.)],
                 account="a", consecutive_ids=False)
    assert manager.status()[0]["position"] is None
    assert len([row for row in store.db.execute("SELECT kind FROM journal") if row[0]=="trade"]) == 1
    assert len([row for row in store.db.execute("SELECT kind FROM journal") if row[0]=="fill"]) == 3
    store.close()


def test_profile_and_quarters_match_research_raw_preparation(tmp_path):
    day=date(2026,8,19)
    rows=[];prices={}
    for i,p in enumerate([100.,102.,99.,103.,100.,101.]):
        rows.append(dict(timestamp=clock_ms(day,1)+i*1000,price=p,qty=float(i+1),is_buyer_maker=bool(i%2),agg_trade_id=i))
        prices[p]=prices.get(p,0)+i+1
    for i,p in enumerate([110.,111.,112.,109.]):
        rows.append(dict(timestamp=clock_ms(day,9)+i*MINUTE,price=p,qty=2.,is_buyer_maker=bool(i%2),agg_trade_id=10+i))
    path=tmp_path/"raw.parquet";pd.DataFrame(rows).to_parquet(path)
    profiles,bars,_=prepare_aggtrades(path,day,day)
    assert make_profile(day,prices)==profiles["pre_ny"][0]
    minutes=pd.DataFrame([dict(timestamp_ms=r["timestamp"],open=r["price"],high=r["price"],low=r["price"],close=r["price"],volume=r["qty"],delta=(-1 if r["is_buyer_maker"] else 1)*r["qty"]) for r in rows[6:]])
    assert quarter_bars(minutes)==bars
    assert profile_key(clock_ms(day,1))==str(day)
    assert profile_key(clock_ms(day,9)) is None


def test_context_uses_prior_sessions_and_causal_atr():
    day=date(2026,8,19); start=clock_ms(day-timedelta(days=21),0);end=clock_ms(day,9,30)
    stamps=list(range(start,end,15*MINUTE))
    frame=pd.DataFrame([dict(timestamp_ms=t,open=100.+i*.01,high=101.+i*.01,low=99.+i*.01,close=100.5+i*.01,volume=20.,delta=4.) for i,t in enumerate(stamps)])
    c=prepare_context(frame,day,end)
    assert c["trend"]["status"]=="ready"
    assert c["trend"]["end_ms"]==end
    assert c["references"]["slots"][0]["prior_sessions"]==20
    assert len(c["bars"])==2
    assert max(c["atr_before"])==clock_ms(day,9,15)
    with pytest.raises(Exception,match="future minute"):
        prepare_context(pd.concat([frame,frame.iloc[-1:].assign(timestamp_ms=end)]),day,end)


def test_hype_does_not_require_c2_history():
    day=date(2026,8,19);start=clock_ms(day,9)
    profile=dict(session_day=str(day),profile_window="pre_ny",profile_start_timestamp_ms=clock_ms(day,1),profile_end_timestamp_ms=start,val=90.,poc=100.,vah=110.)
    bars=[dict(open_timestamp_ms=start+i*QUARTER,close_timestamp_ms=start+(i+1)*QUARTER,
        open=110.+i,high=112.+i,low=109.+i,close=111.+i,buy_volume=10.,sell_volume=2.) for i in range(2)]
    cal=dict(valid_from="2026-08-01",valid_to_exclusive="2026-09-01",trained_through="2026-07-28",
             thresholds=dict(directional_delta_imbalance=.5,directional_result_atr=.5))
    r=evaluate_completed_bar(symbol="HYPEUSDT",profile=profile,bars=bars,as_of_ms=start+2*QUARTER,
       atr_before={start+QUARTER:2.},c1_calibration=cal)
    assert r["selected"] and r["snapshot"]["c2"] is None


def test_worker_no_partial_profile_and_no_duplicate_evaluations(tmp_path):
    day=date(2026,8,19);asof=clock_ms(day,9)
    s=StateStore(tmp_path/"w.db","binance");m=PaperOrderManager(s);MarketData(s,m)
    w=Worker(s,m,now_ms=lambda:asof)
    state=dict(coverage_start_ms=clock_ms(day,2),next_event_ms=asof)
    with s.transaction():
        w.boundary("ETHUSDC",asof,state)
        w.boundary("ETHUSDC",asof,state)
    rows=s.db.execute("SELECT payload FROM evaluations").fetchall()
    assert len(rows)==1 and json.loads(rows[0][0])["state"]=="NOT_READY"
    s.close()


def test_single_writer_lock(tmp_path):
    a=StateStore(tmp_path/"lock.db","binance");b=StateStore(tmp_path/"lock.db","binance")
    a.acquire_writer()
    with pytest.raises(OSError): b.acquire_writer()
    a.close()
    b.acquire_writer()
    b.close()


@pytest.mark.parametrize("historical",[False,True])
def test_worker_candidate_fills_or_locks_without_retrospective_entry(tmp_path,monkeypatch,historical):
    day=date(2026,8,19);start=clock_ms(day,9);asof=start+2*QUARTER
    s=StateStore(tmp_path/"run.db","binance");m=PaperOrderManager(s)
    m.register("tf-preny-eth-v1","ETHUSDC",Config(tp1_r=1,tp1_fraction=.1),allowed_risks=[.0025,.005])
    data=MarketData(s,m)
    p=dict(session_day=str(day),profile_window="pre_ny",profile_start_timestamp_ms=clock_ms(day,1),profile_end_timestamp_ms=start,val=90.,poc=100.,vah=110.)
    s.db.execute("INSERT INTO profiles VALUES (?,?,?)",("ETHUSDC",str(day),encode(p)))
    bars=[dict(open_timestamp_ms=start+i*QUARTER,close_timestamp_ms=start+(i+1)*QUARTER,
        open=110.+i,high=112.+i,low=109.+i,close=111.+i,buy_volume=10.,sell_volume=2.) for i in range(2)]
    refs=dict(through_session_day=str(day-timedelta(days=1)),slots={i:dict(prior_sessions=20,volume=20.,buy_volume=5.,sell_volume=5.,body=.5) for i in range(12)})
    monkeypatch.setattr("models.trend_following_preny_profile_15m.paper_worker.prepare_context",
        lambda *a:dict(bars=bars,references=refs,atr_before={},trend=dict(status="ready",end_ms=asof,price_vs_ema200_pct=.6,adx14=31.)))
    row=dict(timestamp_ms=start,open=110.,high=112.,low=109.,close=111.,volume=12.,delta=8.)
    s.db.execute("INSERT INTO minutes VALUES (?,?,?,?)",("ETHUSDC",start,encode(row),"test"))
    w=Worker(s,m,paper=True,now_ms=lambda:asof+(600000 if historical else 0))
    data.consume("ETHUSDC",[tick(1,asof-1000,111.)],account="tf-preny-eth-v1")
    data.consume("ETHUSDC",[tick(2,asof,112.)],account="tf-preny-eth-v1",on_boundary=w.boundary)
    result=json.loads(s.db.execute("SELECT payload FROM evaluations").fetchone()[0])
    assert result["candidate"]
    if historical:
        assert result["state"]=="NOT_READY"
        assert m.status()[0]["position"] is None
        w.boundary("ETHUSDC",asof+QUARTER,dict(coverage_start_ms=start,next_event_ms=asof+QUARTER))
        last=json.loads(s.db.execute("SELECT payload FROM evaluations ORDER BY asof DESC LIMIT 1").fetchone()[0])
        assert last["state"]=="SESSION_LOCKED"
    else:
        assert result["state"]=="ENTRY_READY"
        assert m.status()[0]["position"]["actual_initial_risk"]==pytest.approx(5.)
    s.close()
