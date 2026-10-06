import ast
import asyncio
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from live_engine.strategy_store import StrategyStore
from market_data.collector import Collector
from market_data.store import DataStore
from models.trend_following_preny_profile_15m import shadow_consumer as sc
from models.trend_following_preny_profile_15m.runtime import observer as ob
from models.trend_following_preny_profile_15m.runtime.evaluator import ACCOUNT_IDS, NotReady
from trading_core.contracts import make_event
from trading_core.contracts import content_hash
from trading_core.session import clock_ms

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026,10,5)
START = clock_ms(DAY,9)


def store(path,venue='binance'):
    return StrategyStore(path,venue=venue,config_hash='cfg',calibration_hash='cal',code_version='test')


def event(sequence=1, **kwargs):
    return make_event(venue='binance',product='futures',instrument_id='ETHUSDC',
        source='test',event_type='coverage',event_timestamp_ms=START,
        available_at_ms=START+1,received_timestamp_ms=START+1,stream_sequence=sequence,
        feature_version='test',quality='complete',payload=kwargs or {'ok':True})


def test_atomic_checkpoint_redelivery_conflict_restart(tmp_path):
    path = tmp_path/'strategy.sqlite'
    s = store(path)
    e = event()
    def fail(row):
        s.set_meta('partial_write',1)
        raise RuntimeError('crash')
    with pytest.raises(RuntimeError): s.apply(1,e,START+2,fail)
    assert s.get_meta('partial_write') is None and s.get_meta('broker_sequence') is None
    assert s.apply(1,e,START+2,lambda row:s.set_meta('applied',1))
    s.close()
    s = store(path)
    assert not s.apply(2,e,START+3,lambda row:pytest.fail('duplicate applied'))
    assert s.get_meta('broker_sequence')=='2'
    with pytest.raises(ValueError,match='Conflicting'):
        s.apply(3,{**e,'payload':{'different':True}},START+4,lambda row:None)
    with pytest.raises(ValueError,match='Wrong strategy'):
        s.apply(3,{**event(2),'venue':'lighter'},START+4,lambda row:None)
    with pytest.raises(ValueError,match='retention gap'): s.check_retention(10)
    assert not s.db.execute("SELECT name FROM sqlite_master WHERE name IN ('accounts','journal','intents')").fetchall()
    s.close()
    with pytest.raises(ValueError,match='identity changed'):
        StrategyStore(path,venue='binance',config_hash='different',calibration_hash='cal',code_version='test')


def test_bootstrap_atomic_readonly_and_anchor(tmp_path):
    source = DataStore(tmp_path/'collector.sqlite','binance')
    c = Collector(source,{'ETHUSDC':'ETHUSDC'})
    ticks = [dict(symbol='ETHUSDC',agg_trade_id=i+1,timestamp_ms=START+i*60_000,
                  price=100.,quantity=1.,buy=True) for i in range(3)]
    c.consume('ETHUSDC',ticks,START+180_000)
    anchor = source.get_meta('stream_sequence')
    s = store(tmp_path/'strategy.sqlite')
    assert s.bootstrap(source.path,symbols=['ETHUSDC'],broker_sequence=5,now_ms=START+180_000)
    assert s.get_meta('bootstrap_source_sequence')==anchor
    assert s.db.execute('SELECT COUNT(*) FROM minutes').fetchone()[0]==1
    s.apply(6,event(1),START+180_001,lambda row:pytest.fail('snapshot event reapplied'))
    assert not s.bootstrap(source.path,symbols=['ETHUSDC'],broker_sequence=999,now_ms=START+180_000)
    assert source.get_meta('stream_sequence')==anchor
    s.close(); source.close()


def test_sparse_minute_still_emits_ordered_quarter_boundary(tmp_path):
    source = DataStore(tmp_path/'collector.sqlite','binance')
    c = Collector(source,{'ETHUSDC':'ETHUSDC'})
    ticks=[dict(symbol='ETHUSDC',agg_trade_id=i+1,timestamp_ms=t,price=100.,quantity=1.,buy=True)
           for i,t in enumerate([START-60_000,START+60_000,START+13*60_000,START+16*60_000])]
    c.consume('ETHUSDC',ticks,START+16*60_000+1)
    events=[json.loads(row['payload']) for row in source.pending()]
    boundary=next(i for i,e in enumerate(events) if e['event_type']=='evaluation_boundary'
                  and e['payload']['as_of_ms']==START+15*60_000)
    assert events[boundary-1]['event_type']=='completed_bar'
    assert events[boundary-1]['payload']['open_timestamp_ms']==START+13*60_000
    assert not source.db.execute('SELECT 1 FROM minutes WHERE timestamp_ms=?',(START+14*60_000,)).fetchone()
    source.close()


def test_collector_database_replacement_fails_closed(tmp_path):
    a=DataStore(tmp_path/'first.sqlite','binance'); Collector(a,ACCOUNT_IDS)
    b=DataStore(tmp_path/'replacement.sqlite','binance'); Collector(b,ACCOUNT_IDS)
    s=store(tmp_path/'strategy.sqlite')
    s.bootstrap(a.path,symbols=ACCOUNT_IDS,broker_sequence=0,now_ms=START)
    with pytest.raises(ValueError,match='database replaced'):
        s.bootstrap(b.path,symbols=ACCOUNT_IDS,broker_sequence=0,now_ms=START)
    with pytest.raises(ValueError,match='epoch changed'):
        s.apply(1,{**event(),'source_epoch':b.get_meta('source_epoch')},START+2,lambda row:None)
    a.close(); b.close(); s.close()


def test_profile_precedes_0900_boundary(tmp_path):
    source=DataStore(tmp_path/'collector.sqlite','binance'); c=Collector(source,{'ETHUSDC':'ETHUSDC'})
    ticks=[dict(symbol='ETHUSDC',agg_trade_id=i+1,timestamp_ms=t,price=100.,quantity=1.,buy=True)
           for i,t in enumerate([clock_ms(DAY,1)-60_000,clock_ms(DAY,1),START])]
    c.consume('ETHUSDC',ticks,START+1)
    events=[json.loads(row['payload']) for row in source.pending()]
    b=next(i for i,e in enumerate(events) if e['event_type']=='evaluation_boundary')
    assert events[b-1]['event_type']=='profile'
    source.close()


def legacy_class():
    namespace = dict(vars(ob))
    exec((ROOT/'tests/fixtures/phase3_legacy_worker_v1.py').read_text(),namespace)
    return namespace['Worker'],namespace


def test_observer_is_exact_extraction_and_session_lock_survives_restart(tmp_path,monkeypatch):
    Legacy, namespace=legacy_class()
    profile=dict(session_day=str(DAY),profile_window='pre_ny',poc=100.,vah=110.,val=90.,
                 profile_start_timestamp_ms=clock_ms(DAY,1),profile_end_timestamp_ms=START)
    bars=[dict(open_timestamp_ms=START+i*900_000,close_timestamp_ms=START+(i+1)*900_000,
        open=110.+i,close=111.+i,high=112.+i,low=109.+i,buy_volume=10.,sell_volume=2.) for i in range(2)]
    def context(frame,day,asof):
        return dict(bars=[b for b in bars if b['close_timestamp_ms']<=asof],
            references=dict(through_session_day='2026-10-04',slots={str(i):dict(prior_sessions=20,
                volume=20.,buy_volume=5.,sell_volume=5.,body=.5) for i in range(12)}),
            atr_before={START+900_000:2.}, signal_atr=None,
            trend=dict(status='ready',end_ms=asof,price_vs_ema200_pct=.6,adx14=31.))
    monkeypatch.setattr(ob,'prepare_context',context); namespace['prepare_context']=context
    paths=[tmp_path/'new.sqlite',tmp_path/'old.sqlite']
    for path in paths:
        s=store(path)
        s.db.execute('INSERT INTO profiles VALUES (?,?,?)',('ETHUSDC',str(DAY),json.dumps(profile)))
        s.db.execute('INSERT INTO minutes VALUES (?,?,?,?)',('ETHUSDC',START,json.dumps({'timestamp_ms':START}),'aggregate_trades'))
        worker = ob.SessionObserver(s,now_ms=lambda:START+1800_000) if path==paths[0] else Legacy(s,None,now_ms=lambda:START+1800_000)
        for asof in [START,START+900_000,START+1800_000]:
            worker.boundary('ETHUSDC',asof,dict(coverage_start_ms=clock_ms(DAY,1)))
        s.close()
    import sqlite3
    with sqlite3.connect(paths[0]) as a,sqlite3.connect(paths[1]) as b:
        assert a.execute('SELECT * FROM evaluations ORDER BY asof').fetchall()==b.execute('SELECT * FROM evaluations ORDER BY asof').fetchall()
    s=store(paths[0]); worker=ob.SessionObserver(s)
    result=worker.boundary('ETHUSDC',START+2700_000,dict(coverage_start_ms=clock_ms(DAY,1)))
    assert result['state']=='SESSION_LOCKED'; s.close()


def test_expiry_never_stores_proposal(tmp_path):
    cfg,_=sc.load_config(ROOT/'models/trend_following_preny_profile_15m/deploy/binance.paper.yaml')
    s=store(tmp_path/'strategy.sqlite'); strategy=sc.ShadowStrategy(s,cfg)
    signal=dict(symbol='ETHUSDC',feature_as_of_ms=START,entry_eligible_timestamp_ms=START,
                entry_deadline_timestamp_ms=START+900_000)
    with pytest.raises(NotReady,match='deadline'):
        strategy.propose(dict(signal=signal,selected=True),START+300_001)
    assert s.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0]==0
    strategy.reconstructing=True
    assert strategy.propose(dict(signal=signal,selected=True),START+300_001)['proposal_state']=='BOOTSTRAP_ONLY'
    s.close()


def test_selected_proposal_is_store_only_and_has_actual_readiness_deadline(tmp_path):
    cfg,_=sc.load_config(ROOT/'models/trend_following_preny_profile_15m/deploy/binance.paper.yaml')
    s=store(tmp_path/'strategy.sqlite'); strategy=sc.ShadowStrategy(s,cfg)
    asof=START+1800_000
    signal=dict(symbol='ETHUSDC',strategy='frozen-v1',route=sc.MODEL,session_day=str(DAY),direction='long',
        feature_as_of_ms=asof,signal_timestamp_ms=asof,entry_eligible_timestamp_ms=asof,
        entry_deadline_timestamp_ms=clock_ms(DAY,12),force_exit_timestamp_ms=START+86400_000-60_000,
        stop=100.,entry_reference=112.,exit_policy='initial_stop_or_time')
    with s.transaction():
        result=strategy.propose(dict(signal=signal,selected=True,risk_fraction=.005,snapshot={'known':True}),asof+10_000)
    proposal=json.loads(s.db.execute('SELECT payload FROM proposals').fetchone()[0])
    assert result['proposal_state']=='SHADOW_ONLY'
    assert proposal['eligible_timestamp_ms']==asof+10_000
    assert proposal['expires_timestamp_ms']==asof+300_000
    assert proposal['risk_fraction']==.005 and proposal['initial_stop']==100.
    assert not s.db.execute("SELECT name FROM sqlite_master WHERE name IN ('outbox','accounts','journal')").fetchall()
    s.close()


def test_gap_blocks_only_affected_market_until_native_coverage_recovers(tmp_path):
    cfg,_=sc.load_config(ROOT/'models/trend_following_preny_profile_15m/deploy/binance.paper.yaml')
    s=store(tmp_path/'strategy.sqlite'); strategy=sc.ShadowStrategy(s,cfg)
    strategy.event({**event(), 'quality':'gap'})
    strategy.boundary('ETHUSDC',START+900_000,dict(coverage_start_ms=START+1))
    result=json.loads(s.db.execute('SELECT payload FROM evaluations').fetchone()[0])
    assert result['state']=='NOT_READY' and 'continuity' in result['reason']
    assert s.get_meta('gap_at:BNBUSDC') is None
    s.close()


def test_shadow_import_boundary():
    code='import models.trend_following_preny_profile_15m.shadow_consumer,sys,json; print(json.dumps(sorted(sys.modules)))'
    modules=json.loads(subprocess.check_output([sys.executable,'-B','-c',code],cwd=ROOT,text=True))
    assert not any(m.startswith(('backtest_engine','risk_research')) or
        m in ('live_engine.order_manager','models.trend_following_preny_profile_15m.strategy',
              'models.trend_following_preny_profile_15m.paper_worker') for m in modules)


@pytest.mark.parametrize('registered',[False,True])
def test_consumer_cli_subscription_restart_and_ack_after_commit(tmp_path,monkeypatch,registered):
    from nats.js.errors import NotFoundError
    from nats.js.api import DeliverPolicy
    config=ROOT/'models/trend_following_preny_profile_15m/deploy/binance.paper.yaml'
    source=DataStore(tmp_path/'collector.sqlite','binance'); Collector(source,ACCOUNT_IDS); source.close()
    import sqlite3
    with sqlite3.connect(tmp_path/'collector.sqlite') as db:
        epoch=db.execute("SELECT value FROM metadata WHERE key='source_epoch'").fetchone()[0]
    database=tmp_path/'strategy.sqlite'
    if registered:
        cfg,cal=sc.load_config(config)
        s=StrategyStore(database,venue='binance',config_hash=content_hash(cfg),calibration_hash=content_hash(cal),code_version='test')
        s.bootstrap(tmp_path/'collector.sqlite',symbols=ACCOUNT_IDS,broker_sequence=0,now_ms=START)
        s.set_meta('durable_registered',1); s.close()
    class EndTest(Exception): pass
    class Message:
        data=json.dumps({**event(),'source_epoch':epoch}).encode()
        metadata=SimpleNamespace(sequence=SimpleNamespace(stream=1))
        async def ack(self):
            import sqlite3
            with sqlite3.connect(database) as db:
                assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==1
                assert db.execute("SELECT value FROM metadata WHERE key='broker_sequence'").fetchone()[0]=='1'
    class Subscription:
        calls=0
        async def fetch(self,batch,timeout):
            self.calls+=1
            if self.calls==1: return [Message()]
            raise EndTest()
    class JS:
        async def stream_info(self,name): return SimpleNamespace(state=SimpleNamespace(first_seq=1,last_seq=0))
        async def consumer_info(self,name,durable):
            if not registered: raise NotFoundError()
            return SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=0))
        async def pull_subscribe(self,subject,durable,stream,config):
            assert subject=='md.v1.binance.futures.>' and 'binance' in durable
            if registered: assert config is None
            else:
                assert config.deliver_policy==DeliverPolicy.BY_START_SEQUENCE
                assert config.opt_start_seq==1
            return Subscription()
    class NC:
        closed=False
        def jetstream(self): return JS()
        async def close(self): self.closed=True
    nc=NC()
    async def connect(url): return nc
    monkeypatch.setattr(sc,'connect',connect)
    args=SimpleNamespace(config=config,c1_calibration=None,database=database,
        bootstrap_database=tmp_path/'collector.sqlite',code_version='test',url='test')
    with pytest.raises(EndTest): asyncio.run(sc.consume(args))
    assert nc.closed
