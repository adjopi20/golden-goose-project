"""Opt-in integration tests against a disposable LOCAL NATS on port 15222."""
import asyncio
import json
import os
import uuid

import pytest

from live_engine.account_store import AccountStore
from live_engine.account_owner import AccountOwner
from market_data.shadow import fetch_batch
from test_account_phase4 import config, intention, event, trade, records


@pytest.mark.skipif(os.environ.get('GG_LOCAL_NATS_TEST')!='1',reason='Requires disposable local NATS')
@pytest.mark.parametrize('committed_before_restart',[False,True])
def test_single_inflight_restart_and_lost_ack_match_uninterrupted(tmp_path,committed_before_restart):
    import nats
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    async def run():
        stream='AUDIT_'+uuid.uuid4().hex
        subject='audit.'+uuid.uuid4().hex
        cfg=config();path=tmp_path/'restart.sqlite'
        store=AccountStore(path,cfg,'test');owner=AccountOwner(store,cfg)
        store.anchor('exec',0);owner.intention(intention(),1000)
        control=AccountStore(tmp_path/'control.sqlite',cfg,'test');baseline=AccountOwner(control,cfg)
        control.anchor('exec',0);baseline.intention(intention(),1000)
        events=[event([trade(i,ts,price)]) for i,ts,price in [
            (1,1001,100.),(2,1500,110.),(3,1501,111.),(4,2000,89.),(5,2001,88.)]]
        nc=await nats.connect('nats://127.0.0.1:15222',max_reconnect_attempts=0)
        try:
            js=nc.jetstream()
            await js.add_stream(name=stream,subjects=[subject])
            for payload in events:await js.publish(subject,json.dumps(payload).encode())
            sub=await js.pull_subscribe(subject,'worker',stream=stream,
                config=ConsumerConfig(durable_name='worker',ack_policy=AckPolicy.EXPLICIT,
                    deliver_policy=DeliverPolicy.ALL,ack_wait=1,max_ack_pending=1))
            first=(await sub.fetch(1,timeout=.5))[0]
            if committed_before_restart:
                store.apply('exec',1,events[0]['event_id'],events[0],1001,owner.event)
            # Neither case acknowledged the first message; later messages must remain blocked.
            store.close();await nc.close()
            store=AccountStore(path,cfg,'test');owner=AccountOwner(store,cfg)
            nc=await nats.connect('nats://127.0.0.1:15222',max_reconnect_attempts=0)
            js=nc.jetstream();sub=await js.pull_subscribe_bind('worker',stream=stream)
            with pytest.raises(TimeoutError):await sub.fetch(1,timeout=.15)
            assert store.checkpoint('exec')==(1 if committed_before_restart else 0)
            await asyncio.sleep(1)
            redelivered=(await sub.fetch(1,timeout=.5))[0]
            assert first.metadata.sequence.stream==redelivered.metadata.sequence.stream==1
            changed=store.apply('exec',1,events[0]['event_id'],events[0],1001,owner.event)
            assert changed is not committed_before_restart
            await redelivered.ack_sync()
            for sequence,payload in enumerate(events[1:],2):
                messages=await fetch_batch(sub,batch=1)
                assert len(messages)==1
                message=messages[0]
                assert message.metadata.sequence.stream==sequence
                store.apply('exec',sequence,payload['event_id'],payload,payload['event_timestamp_ms'],owner.event)
                await message.ack_sync()
            for sequence,payload in enumerate(events,1):
                control.apply('exec',sequence,payload['event_id'],payload,payload['event_timestamp_ms'],baseline.event)
            assert records(store,'fill')==records(control,'fill')
            assert records(store,'trade')==records(control,'trade')
            assert store.cash('eth')==control.cash('eth')
            assert store.checkpoint('exec')==5
            assert not store.db.execute('SELECT * FROM reservations').fetchall()
            assert store.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==5
            assert (await js.consumer_info(stream,'worker')).num_ack_pending==0
        finally:
            await nc.jetstream().delete_stream(stream)
            await nc.close();store.close();control.close()
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get('GG_LOCAL_NATS_TEST')!='1',reason='Requires disposable local NATS')
def test_real_broker_pause_upgrade_resume_preserves_open_position(tmp_path):
    import nats
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
    from live_engine.account_delivery_upgrade import SOURCE_RELEASE, upgrade, quiesce
    from live_engine.account_worker import STREAMS
    from infra.ops.upgrade_phase4_account_health import state_fingerprint

    async def run():
        cfg=config();path=tmp_path/'account.sqlite'
        s=AccountStore(path,cfg,SOURCE_RELEASE);o=AccountOwner(s,cfg)
        nc=await nats.connect('nats://127.0.0.1:15222',max_reconnect_attempts=0)
        js=nc.jetstream();created=[]
        try:
            for channel,prefix in zip(STREAMS,('intent','md','exec')):
                await js.add_stream(name=channel,subjects=[prefix+'.v1.>']);created.append(channel)
                s.anchor(channel,0);s.set_meta('durable:'+channel,1)
                name=f'phase4_{cfg["account_group"]}_{channel}'
                await js.add_consumer(channel,config=ConsumerConfig(durable_name=name,
                    ack_policy=AckPolicy.EXPLICIT,deliver_policy=DeliverPolicy.ALL,
                    filter_subject=prefix+'.v1.binance.futures.>',max_ack_pending=100,ack_wait=60))
            o.intention(intention(),1000)
            subject='exec.v1.binance.futures.ETHUSDC.trade'
            e=event([trade(1,1001,100.)]);await js.publish(subject,json.dumps(e).encode())
            sub=await js.pull_subscribe_bind(f'phase4_{cfg["account_group"]}_GG_EXEC_V1',stream='GG_EXEC_V1')
            message=(await sub.fetch(1,timeout=.5))[0]
            s.apply('GG_EXEC_V1',1,e['event_id'],e,1001,o.event);await message.ack_sync()
            before=state_fingerprint(s.db)
            await quiesce(js,cfg['account_group'])
            e2=event([trade(2,1500,111.)]);await js.publish(subject,json.dumps(e2).encode())
            with pytest.raises(TimeoutError):await sub.fetch(1,timeout=.15)
            await asyncio.sleep(.1)  # Expire the timed-out pull before the stopped-worker audit.
            result=await upgrade(s,js,'a'*40,tmp_path/'backup.sqlite')
            assert result['status']=='upgraded' and state_fingerprint(s.db)==before
            assert json.loads(s.db.execute('SELECT state FROM accounts').fetchone()[0])['position']
            message=(await sub.fetch(1,timeout=.5))[0]
            assert message.metadata.sequence.stream==2
            s.apply('GG_EXEC_V1',2,e2['event_id'],e2,1500,o.event);await message.ack_sync()
            for channel in STREAMS:
                c=await js.consumer_info(channel,f'phase4_{cfg["account_group"]}_{channel}')
                assert c.config.max_ack_pending==1
            s.close();s=AccountStore(path,cfg,'a'*40)
        finally:
            for channel in created:await js.delete_stream(channel)
            await nc.close();s.close()
    asyncio.run(run())
