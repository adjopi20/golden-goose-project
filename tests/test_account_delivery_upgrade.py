import asyncio
from dataclasses import replace
import sqlite3
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_engine.account_delivery_upgrade import SOURCE_RELEASE, SOURCE_RELEASES, upgrade, quiesce
from live_engine.account_owner import AccountOwner
from live_engine.account_store import AccountStore
from live_engine.account_worker import STREAMS
from infra.ops.upgrade_phase4_account_health import state_fingerprint
from test_account_phase4 import config, intention, event, trade


class Broker:
    def __init__(self,store):
        from nats.js.api import AckPolicy, ConsumerConfig
        self.consumers={};self.updates=[];self.resumed=[];self.paused=[]
        for channel in STREAMS:
            store.anchor(channel,0);store.set_meta('durable:'+channel,1)
            prefix={'GG_INTENT_V1':'intent','GG_MARKET_V1':'md','GG_EXEC_V1':'exec'}[channel]
            name=f'phase4_{store.get_meta("account_group")}_{channel}'
            self.consumers[channel]=SimpleNamespace(name=name,
                config=ConsumerConfig(durable_name=name,ack_policy=AckPolicy.EXPLICIT,
                    filter_subject=f'{prefix}.v1.binance.futures.>',max_ack_pending=100,ack_wait=60),
                num_ack_pending=0,num_waiting=0,
                ack_floor=SimpleNamespace(stream_seq=0,consumer_seq=0),
                delivered=SimpleNamespace(stream_seq=0,consumer_seq=0))
    async def stream_info(self,channel):return SimpleNamespace(state=SimpleNamespace(first_seq=1))
    async def consumer_info(self,channel,durable):return self.consumers[channel]
    async def add_consumer(self,channel,config):
        self.updates.append(channel);self.consumers[channel].config=config
        return self.consumers[channel]
    async def resume_consumer(self,channel,durable):self.resumed.append(channel)
    async def pause_consumer(self,channel,durable,until):self.paused.append(channel)


@pytest.mark.parametrize('stage',['flat','pending','open','partial'])
@pytest.mark.parametrize('source',sorted(SOURCE_RELEASES))
def test_upgrade_preserves_state_and_reopens(tmp_path,stage,source):
    cfg=config();path=tmp_path/'account.sqlite'
    s=AccountStore(path,cfg,source);o=AccountOwner(s,cfg);js=Broker(s)
    if stage!='flat':o.intention(intention(),1000)
    ticks=[trade(1,1001,100.),trade(2,1500,111.),trade(3,1501,111.)]
    if stage=='open':o.event(event(ticks[:1]),1001)
    if stage=='partial':o.event(event(ticks),1501)
    before=state_fingerprint(s.db)
    assert asyncio.run(upgrade(s,js,'a'*40))['status']=='ready'
    assert js.updates==[]
    result=asyncio.run(upgrade(s,js,'a'*40,tmp_path/'backup.sqlite'))
    assert result['status']=='upgraded' and state_fingerprint(s.db)==before
    assert js.updates==list(STREAMS) and js.resumed==list(STREAMS)
    assert s.get_meta('code_version')=='a'*40
    s.close();s=AccountStore(path,cfg,'a'*40);s.close()
    with sqlite3.connect(tmp_path/'backup.sqlite') as db:
        assert dict(db.execute('SELECT key,value FROM metadata'))['code_version']==source


@pytest.mark.parametrize('fault',['pending_ack','pending_pull','wrong_filter','ack_hole','cursor_hole','blocked','unknown_release'])
def test_upgrade_refuses_before_backup_or_broker_mutation(tmp_path,fault):
    s=AccountStore(tmp_path/'a.sqlite',config(),SOURCE_RELEASE);AccountOwner(s,config());js=Broker(s)
    c=js.consumers['GG_EXEC_V1']
    if fault=='pending_ack':c.num_ack_pending=1
    if fault=='pending_pull':c.num_waiting=1
    if fault=='wrong_filter':c.config=replace(c.config,filter_subject='exec.v1.lighter.futures.>')
    if fault=='ack_hole':c.delivered.consumer_seq=1
    if fault=='cursor_hole':c.delivered.consumer_seq=c.ack_floor.consumer_seq=1;c.delivered.stream_seq=1
    if fault=='blocked':s.set_meta('blocked:ETHUSDC',1000)
    if fault=='unknown_release':s.set_meta('code_version','unknown')
    before=state_fingerprint(s.db)
    with pytest.raises(ValueError):asyncio.run(upgrade(s,js,'a'*40,tmp_path/'backup.sqlite'))
    assert state_fingerprint(s.db)==before and js.updates==[] and not (tmp_path/'backup.sqlite').exists()
    s.close()


def test_partial_broker_failure_keeps_ledger_and_can_resume(tmp_path):
    s=AccountStore(tmp_path/'a.sqlite',config(),SOURCE_RELEASE);AccountOwner(s,config());js=Broker(s)
    original=js.add_consumer
    async def fail(channel,config):
        if channel=='GG_MARKET_V1':raise TimeoutError('network loss')
        return await original(channel,config)
    js.add_consumer=fail;before=state_fingerprint(s.db)
    with pytest.raises(TimeoutError):asyncio.run(upgrade(s,js,'a'*40,tmp_path/'backup1.sqlite'))
    assert state_fingerprint(s.db)==before and s.get_meta('code_version')==SOURCE_RELEASE
    js.add_consumer=original
    assert asyncio.run(upgrade(s,js,'a'*40,tmp_path/'backup2.sqlite'))['status']=='upgraded'
    s.close()


def test_upgrade_uses_persisted_readback_when_update_response_is_stale(tmp_path):
    s=AccountStore(tmp_path/'a.sqlite',config(),SOURCE_RELEASE);AccountOwner(s,config());js=Broker(s)
    original=js.add_consumer
    async def stale_response(channel,config):
        old=SimpleNamespace(config=replace(js.consumers[channel].config,pause_until=None))
        await original(channel,config)
        return old
    js.add_consumer=stale_response
    assert asyncio.run(upgrade(s,js,'a'*40,tmp_path/'backup.sqlite'))['status']=='upgraded'
    assert all(c.config.max_ack_pending==1 for c in js.consumers.values())
    s.close()


def test_upgrade_rejects_readback_even_if_update_response_claims_success(tmp_path):
    s=AccountStore(tmp_path/'a.sqlite',config(),SOURCE_RELEASE);AccountOwner(s,config());js=Broker(s)
    async def not_applied(channel,config):return SimpleNamespace(config=config)
    js.add_consumer=not_applied
    before=state_fingerprint(s.db)
    with pytest.raises(ValueError,match='read-back differs.*max_ack_pending'):
        asyncio.run(upgrade(s,js,'a'*40,tmp_path/'backup.sqlite'))
    assert state_fingerprint(s.db)==before and s.get_meta('code_version')==SOURCE_RELEASE
    assert js.resumed==[]
    s.close()


def test_quiesce_pauses_all_streams_before_accepting_drain(tmp_path):
    s=AccountStore(tmp_path/'a.sqlite',config(),SOURCE_RELEASE);AccountOwner(s,config());js=Broker(s)
    asyncio.run(quiesce(js,config()['account_group']))
    assert js.paused==list(STREAMS) and js.updates==[] and js.resumed==[]
    s.close()


@pytest.mark.parametrize('scenario,allowed',[
    ('fresh_affected_flat',True),('fresh_healthy',False),('fresh_open',False),
    ('healthy_blocked',False),('healthy_open',True),('fresh_outbox',False),
    ('fresh_published_history',True)])
def test_shell_preflight_refuses_unsafe_cutover_without_mutation(tmp_path,scenario,allowed):
    path=tmp_path/'account.sqlite';s=AccountStore(path,config(),SOURCE_RELEASE);o=AccountOwner(s,config())
    fresh=scenario.startswith('fresh')
    if scenario.endswith('open'):
        o.intention(intention(),1000);o.event(event([trade(1,1001,100.)]),1001)
    if scenario in ('fresh_affected_flat','fresh_open','healthy_blocked','fresh_outbox','fresh_published_history'):
        s.set_meta('blocked:ETHUSDC',2000)
    before=state_fingerprint(s.db)
    strategy=tmp_path/'strategy.sqlite'
    with sqlite3.connect(strategy) as db:
        db.execute('CREATE TABLE outbox(payload TEXT,published INTEGER NOT NULL)')
        if scenario=='fresh_outbox':db.execute("INSERT INTO outbox VALUES ('pending',0)")
        if scenario=='fresh_published_history':
            db.executemany('INSERT INTO outbox VALUES (?,1)',[(f'history-{i}',) for i in range(20)])
    script=(Path(__file__).resolve().parents[1]/'infra/ops/upgrade_phase4_delivery.sh').read_text()
    code=re.search(r"check_state\(\) \{.*?<<'PY'\n(.*?)\nPY",script,re.S)[1]
    result=subprocess.run([sys.executable,'-B','-c',code,str(path),str(strategy),
        '--fresh-epoch' if fresh else '', 'a'*40],capture_output=True,text=True,timeout=10)
    assert (result.returncode==0)==allowed,(result.stdout,result.stderr)
    assert state_fingerprint(s.db)==before
    s.close()
