import asyncio
from datetime import date
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest

from live_engine import account_worker as aw
from live_engine.account_store import AccountStore
from live_engine.account_owner import AccountOwner
from live_engine.strategy_store import StrategyStore
from models.trend_following_preny_profile_15m import shadow_consumer as sc
from trading_core.contracts import content_hash
from trading_core.session import clock_ms
from test_account_phase4 import config, intention, event, trade

ROOT=Path(__file__).resolve().parents[1]


def test_strategy_publication_optin_atomic_and_bootstrap_suppressed(tmp_path):
    cfg,_=sc.load_config(ROOT/'models/trend_following_preny_profile_15m/deploy/binance.paper.yaml')
    s=StrategyStore(tmp_path/'s.sqlite',venue='binance',config_hash=content_hash(cfg),
        calibration_hash='cal',code_version='test')
    strategy=sc.ShadowStrategy(s,cfg,publish_intentions=True)
    start=clock_ms(date(2026,10,6),10)
    signal=dict(sample_id='sample',symbol='ETHUSDC',strategy='trend_following_preny_profile_15m_v1',
        route=sc.MODEL,session_day='2026-10-06',direction='long',feature_as_of_ms=start,
        signal_timestamp_ms=start,entry_eligible_timestamp_ms=start,
        entry_deadline_timestamp_ms=start+300000,force_exit_timestamp_ms=start+80000000,
        stop=90.,entry_reference=100.,exit_policy='initial_stop_or_time')
    r=dict(signal=signal,selected=True,risk_fraction=.005,snapshot={'known':True})
    strategy.reconstructing=True
    assert strategy.propose(r,start+1)['proposal_state']=='BOOTSTRAP_ONLY'
    assert not s.pending()
    strategy.reconstructing=False
    with pytest.raises(RuntimeError):
        with s.transaction():strategy.propose(r,start+1);raise RuntimeError('power lost')
    assert not s.pending() and not s.db.execute('SELECT * FROM proposals').fetchall()
    assert strategy.propose(r,start+1)['proposal_state']=='QUEUED_PAPER'
    assert len(s.pending())==1 and json.loads(s.pending()[0]['payload'])['source_sample_id']=='sample'
    with pytest.raises(ValueError,match='Publication mode changed'):sc.ShadowStrategy(s,cfg,publish_intentions=False)
    s.close()


@pytest.mark.parametrize('registered',[False,True])
def test_account_broker_three_cursors_commit_before_ack_and_restart(tmp_path,monkeypatch,registered):
    from nats.js.errors import NotFoundError
    from nats.js.api import DeliverPolicy
    path=tmp_path/'account.sqlite';cp=tmp_path/'config.json';cfg=config()
    cp.write_text(json.dumps(cfg))
    if registered:
        s=AccountStore(path,cfg,'test');AccountOwner(s,cfg)
        for channel in aw.STREAMS:
            s.anchor(channel,0);s.set_meta('durable:'+channel,1)
        s.close()
    class EndTest(Exception):pass
    p=intention();acks=[];seen=[]
    class Message:
        data=json.dumps(p).encode()
        metadata=SimpleNamespace(sequence=SimpleNamespace(stream=1))
        async def ack(self):
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT status FROM requests').fetchone()[0]=='pending'
                assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==1
                assert db.execute("SELECT sequence FROM checkpoints WHERE channel='GG_INTENT_V1'").fetchone()[0]==1
            acks.append('committed');raise EndTest()
    class JS:
        async def stream_info(self,name):return SimpleNamespace(state=SimpleNamespace(first_seq=1,last_seq=0))
        async def consumer_info(self,name,durable):
            if not registered:raise NotFoundError()
            return SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=0))
        async def pull_subscribe(self,subject,durable,stream,config):
            assert 'binance' in subject and 'phase4_' in durable
            if registered:assert config is None
            else:assert config.deliver_policy==DeliverPolicy.BY_START_SEQUENCE and config.opt_start_seq==1
            seen.append(stream);return stream
    class NC:
        closed=False
        def jetstream(self):return JS()
        async def close(self):self.closed=True
    nc=NC()
    async def connect(url):return nc
    async def fetch(sub):
        if sub=='GG_INTENT_V1':return [Message()]
        await asyncio.sleep(10);return []
    monkeypatch.setattr(aw,'connect',connect);monkeypatch.setattr(aw,'fetch_batch',fetch)
    monkeypatch.setattr(aw.time,'time',lambda:1.)
    args=SimpleNamespace(database=path,config=cp,code_version='test',url='test')
    with pytest.raises(EndTest):asyncio.run(aw.consume(args))
    assert seen==list(aw.STREAMS) and acks==['committed'] and nc.closed


def test_stream_init_adds_only_intentions_and_never_reconfigures_market(monkeypatch):
    from nats.js.errors import NotFoundError
    names=[];created=[]
    class JS:
        async def stream_info(self,name):names.append(name);raise NotFoundError()
        async def add_stream(self,config):created.append(config)
    class NC:
        def jetstream(self):return JS()
        async def close(self):pass
    async def connect(url):return NC()
    monkeypatch.setattr(aw,'connect',connect)
    asyncio.run(aw.initialize_intentions('test'))
    assert names==['GG_INTENT_V1'] and created[0].subjects==['intent.v1.>']
    assert created[0].max_bytes==32*1024*1024
