import asyncio
from datetime import date
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from live_engine.state_store import StateStore
from live_engine.market_data import MarketData, MINUTE
from live_engine.adapters.lighter_public import LighterPublic
from market_data.collector import Collector
from market_data.store import DataStore, Backpressure
from market_data.broker import flush_outbox
from market_data.shadow import ReceiptStore
from models.trend_following_preny_profile_15m.runtime.preparation import make_profile, profile_key, quarter_bars
from trading_core.session import clock_ms


def tick(i,stamp,price=100.,buy=True):
    return dict(symbol='ETHUSDC',agg_trade_id=i,timestamp_ms=stamp,price=price,quantity=.3,buy=buy)


@pytest.mark.parametrize('venue,step',[('binance',1),('lighter',7)])
def test_identical_source_exact_candle_delta_profile_parity(tmp_path,venue,step):
    import pandas as pd
    day=date(2026,10,3)
    start=clock_ms(day,1)-MINUTE
    ticks=[tick(1+i*step,start+i*MINUTE,100.+i%11/10,i%3!=0) for i in range(482)]
    legacy=StateStore(tmp_path/'legacy.sqlite',venue)
    old=MarketData(legacy)
    old.consume('ETHUSDC',ticks,profile_key=profile_key,consecutive_ids=venue=='binance')
    store=DataStore(tmp_path/'collector.sqlite',venue)
    collector=Collector(store,{'ETHUSDC':'ETHUSDC' if venue=='binance' else '0'})
    for chunk in (ticks[:250],ticks[250:]):
        collector.consume('ETHUSDC',chunk,ticks[-1]['timestamp_ms']+1)
    assert [tuple(r) for r in store.db.execute('SELECT * FROM minutes ORDER BY timestamp_ms')] == [tuple(r) for r in legacy.db.execute('SELECT * FROM minutes ORDER BY timestamp_ms')]
    prices=dict(legacy.db.execute('SELECT price,quantity FROM profile_prices ORDER BY price'))
    profile=json.loads(store.db.execute('SELECT payload FROM profiles').fetchone()[0])
    assert profile==make_profile(day,prices)
    old_minutes=[json.loads(r[0]) for r in legacy.db.execute('SELECT payload FROM minutes ORDER BY timestamp_ms')]
    new_minutes=[json.loads(r[0]) for r in store.db.execute('SELECT payload FROM minutes ORDER BY timestamp_ms')]
    assert quarter_bars(pd.DataFrame(new_minutes))==quarter_bars(pd.DataFrame(old_minutes))
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name IN ('accounts','intents','journal')").fetchall()
    store.close(); legacy.close()


def test_atomic_outbox_failure_does_not_advance_state(tmp_path):
    store=DataStore(tmp_path/'c.sqlite','binance',max_outbox_bytes=10)
    c=Collector(store,{'ETHUSDC':'ETHUSDC'})
    with pytest.raises(Backpressure): c.consume('ETHUSDC',[tick(1,1)],10)
    assert c.market.state('ETHUSDC') is None
    assert store.get_meta('stream_sequence')=='0'
    assert not store.pending()
    store.close()


def test_collector_cannot_mutate_or_reuse_legacy_ledger(tmp_path):
    path=tmp_path/'old.sqlite'
    old=StateStore(path,'binance'); old.close()
    with pytest.raises(ValueError,match='Not this collector'): DataStore(path,'binance')
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='outbox'").fetchone()


def test_stream_specs_use_installed_client_api(monkeypatch):
    pytest.importorskip('nats')
    from market_data.broker import initialize
    from nats.js.errors import NotFoundError
    configurations=[]
    class JS:
        async def stream_info(self,name): raise NotFoundError()
        async def add_stream(self,config): configurations.append(config)
    asyncio.run(initialize(JS()))
    assert [c.name for c in configurations]==['GG_MARKET_V1','GG_EXEC_V1']
    assert configurations[0].max_age==72*3600
    assert configurations[1].max_age==2*3600


def test_restart_duplicate_conflict_and_sequence(tmp_path):
    path=tmp_path/'c.sqlite'
    s=DataStore(path,'binance'); c=Collector(s,{'ETHUSDC':'ETHUSDC'})
    rows=[tick(1,1),tick(2,MINUTE)]
    c.consume('ETHUSDC',rows,2*MINUTE)
    count=len(s.pending()); sequence=s.get_meta('stream_sequence')
    s.close()
    s=DataStore(path,'binance'); c=Collector(s,{'ETHUSDC':'ETHUSDC'})
    c.consume('ETHUSDC',[rows[-1],rows[-1]],2*MINUTE)
    assert len(s.pending())==count and s.get_meta('stream_sequence')==sequence
    with pytest.raises(ValueError,match='Conflicting'): c.consume('ETHUSDC',[{**rows[-1],'price':99}],2*MINUTE)
    with pytest.raises(ValueError,match='ID gap'): c.consume('ETHUSDC',[tick(4,2*MINUTE)],3*MINUTE)
    c.consume('ETHUSDC',[tick(3,2*MINUTE)],3*MINUTE)
    assert int(s.get_meta('stream_sequence'))>int(sequence)
    s.close()


def test_no_synthetic_minute_and_gap_resets_profile_coverage(tmp_path):
    s=DataStore(tmp_path/'c.sqlite','lighter'); c=Collector(s,{'ETHUSDC':'0'})
    start=clock_ms(date(2026,10,3),1)
    c.consume('ETHUSDC',[tick(1,start),tick(9,start+MINUTE),tick(20,start+4*MINUTE)],start+5*MINUTE)
    assert s.db.execute('SELECT COUNT(*) FROM minutes').fetchone()[0]==1
    c.gap('ETHUSDC',start+6*MINUTE,'history_unavailable')
    assert c.market.state('ETHUSDC') is None
    assert s.db.execute('SELECT COUNT(*) FROM profile_prices').fetchone()[0]==0
    c.consume('ETHUSDC',[tick(100,start+7*MINUTE),tick(110,clock_ms(date(2026,10,3),9))],clock_ms(date(2026,10,3),9)+1)
    assert not s.db.execute('SELECT * FROM profiles').fetchall()
    events=[json.loads(r['payload']) for r in s.pending()]
    assert any(e['quality']=='gap' for e in events)
    assert any(e['quality']=='partial' and e['event_type']=='coverage' for e in events)
    s.close()


def test_publication_ack_loss_redelivers_identical_payload(tmp_path):
    s=DataStore(tmp_path/'c.sqlite','binance'); c=Collector(s,{'ETHUSDC':'ETHUSDC'})
    c.consume('ETHUSDC',[tick(1,1)],2)
    calls=[]
    class Broker:
        async def publish(self,subject,payload,**kw):
            calls.append((subject,payload,kw))
            if len(calls)==1: raise OSError('ack lost')
    with pytest.raises(OSError): asyncio.run(flush_outbox(s,Broker()))
    assert len(s.pending())==1
    asyncio.run(flush_outbox(s,Broker()))
    assert calls[0]==calls[1] and not s.pending()
    s.close()


def test_receipts_idempotent_commit_conflict_and_retention(tmp_path):
    s=DataStore(tmp_path/'c.sqlite','binance'); c=Collector(s,{'ETHUSDC':'ETHUSDC'})
    c.consume('ETHUSDC',[tick(1,1)],2)
    e=json.loads(s.pending()[0]['payload'])
    receipts=ReceiptStore(tmp_path/'r.sqlite')
    receipts.apply('GG_EXEC_V1',1,e,3)
    receipts.apply('GG_EXEC_V1',1,e,4)
    assert receipts.status()['receipts']==1
    with pytest.raises(ValueError,match='conflicting'): receipts.apply('GG_EXEC_V1',2,{**e,'payload':{'different':True}},5)
    receipts.check_retention('GG_EXEC_V1',10,6)
    assert receipts.status()['warnings']==1
    receipts.db.close(); s.close()


def test_collector_import_closure_has_no_evaluator_or_execution():
    root=Path(__file__).resolve().parents[1]
    code='import market_data.run,sys,json; print(json.dumps(sorted(sys.modules)))'
    modules=json.loads(subprocess.check_output([sys.executable,'-B','-c',code],cwd=root,text=True))
    assert not any(m.startswith(('models.','backtest_engine','pandas','numpy')) or m=='live_engine.order_manager' for m in modules)


def test_lighter_conflicting_snapshot_duplicate_rejected():
    r=dict(market_id=0,trade_id=1,timestamp=1,price='100',size='1',type='trade',is_maker_ask=True)
    with pytest.raises(ValueError,match='Conflicting'): LighterPublic._normalize_rows('ETHUSDC',[r,{**r,'price':'101'}])


def test_repair_is_batched_not_full_history_materialization():
    from market_data.run import repair_batch
    rows=iter(range(1200))
    assert len(repair_batch(rows))==500
    assert len(repair_batch(rows))==500
    assert len(repair_batch(rows))==200
    assert repair_batch(rows)==[]


def test_binance_worker_run_startup_regression(tmp_path,monkeypatch):
    # This exercises the CLI run branch that an import-only smoke test missed.
    from models.trend_following_preny_profile_15m import paper_worker as worker
    import time
    class API:
        def validate_markets(self,symbols): return {}
        def server_time(self): return int(time.time()*1000)
        def stream(self,symbols):
            raise KeyboardInterrupt()
            yield None
    monkeypatch.setattr(worker,'BinancePublic',API)
    monkeypatch.setattr(sys,'argv',['paper_worker','--database',str(tmp_path/'legacy.sqlite'),'--action','run'])
    worker.main()
