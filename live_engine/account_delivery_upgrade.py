"""Guarded single-inflight upgrade. Stop the account worker before running."""
import argparse
import asyncio
from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

from infra.ops.upgrade_phase4_account_health import inspect, state_fingerprint
from live_engine.account_store import AccountStore
from live_engine.account_worker import STREAMS, load_config, status
from market_data.broker import connect

SOURCE_RELEASE = '15983d70cd0da5dbc903a8de56ff874043204754'
SOURCE_RELEASES = {SOURCE_RELEASE, '348b6290d1ca6f87abb6b39ad61ad395ad32a276',
    'a23fd747a14662d324b8019231661802156181ab'}


async def quiesce(js, group):
    """Pause new delivery while the old worker finishes its already fetched batch."""
    until=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    for channel in STREAMS:
        await js.pause_consumer(channel,f'phase4_{group}_{channel}',until)
    deadline=time.monotonic()+20
    while True:
        pending=[(await js.consumer_info(channel,f'phase4_{group}_{channel}')).num_ack_pending for channel in STREAMS]
        if not any(pending):return
        if time.monotonic()>=deadline:raise ValueError('Worker did not finish in-flight deliveries; review required')
        await asyncio.sleep(.5)


async def audit(store, js, target):
    from nats.js.api import AckPolicy
    inspect(store.db,target,compatible=SOURCE_RELEASES)
    result=status(store.path)
    if result['accounting_checks']['status']!='pass':
        raise ValueError('Accounting checks failed')
    if result['input_blocks'] or result['uncertain_closes']:
        raise ValueError('Uncertain ledger: separate reviewed recovery required')
    consumers=[]
    for channel in STREAMS:
        stream=await js.stream_info(channel)
        store.check_retention(channel,stream.state.first_seq)
        if store.get_meta('durable:'+channel)!='1':
            raise ValueError('Missing durable registration: '+channel)
        durable=f'phase4_{store.get_meta("account_group")}_{channel}'
        c=await js.consumer_info(channel,durable)
        prefix={'GG_INTENT_V1':'intent','GG_MARKET_V1':'md','GG_EXEC_V1':'exec'}[channel]
        expected=f'{prefix}.v1.{store.get_meta("venue")}.futures.>'
        if (c.config.filter_subject!=expected or c.config.ack_policy!=AckPolicy.EXPLICIT
                or c.config.max_ack_pending not in (1,100)):
            raise ValueError('Unreviewed consumer policy: '+channel)
        if c.num_ack_pending or c.num_waiting:
            raise ValueError('Outstanding delivery/pull; keep stopped and review: '+channel)
        if c.ack_floor.consumer_seq!=c.delivered.consumer_seq:
            raise ValueError('Consumer acknowledgment is incomplete: '+channel)
        cursor=store.checkpoint(channel)
        if c.ack_floor.stream_seq>cursor or (c.delivered.consumer_seq and c.delivered.stream_seq!=cursor):
            raise ValueError('Ledger and acknowledged delivery disagree: '+channel)
        consumers.append((channel,c))
    return consumers


async def upgrade(store, js, target, backup=None):
    if not re.fullmatch('[0-9a-f]{40}',target):raise ValueError('Exact target commit required')
    consumers=await audit(store,js,target)
    if backup is None:return {'status':'ready','venue':store.get_meta('venue')}
    if store.get_meta('code_version')==target and all(c.config.max_ack_pending==1 for _,c in consumers):
        for channel,c in consumers:await js.resume_consumer(channel,c.name)
        return {'status':'already_current','venue':store.get_meta('venue')}
    store.backup(backup)
    with sqlite3.connect(backup) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Backup failed integrity check')
    before=state_fingerprint(store.db)
    for channel,c in consumers:
        if c.config.max_ack_pending==1:continue
        desired=replace(c.config,max_ack_pending=1)
        expected=deepcopy(desired.as_dict())
        await js.add_consumer(channel,config=desired)
        # The update response can contain the old config; verify persisted state instead.
        actual=(await js.consumer_info(channel,c.name)).config.as_dict()
        changed=sorted(k for k in expected.keys()|actual.keys() if expected.get(k)!=actual.get(k))
        if changed:
            raise ValueError('Broker read-back differs on '+channel+': '+','.join(changed)+'; keep worker stopped')
    await audit(store,js,target)
    with store.transaction():
        store.set_meta('code_version',target)
        if state_fingerprint(store.db)!=before:raise ValueError('Trading state changed during upgrade')
    for channel,c in consumers:await js.resume_consumer(channel,c.name)
    return {'status':'upgraded','venue':store.get_meta('venue'),'backup':str(backup)}


async def main_async(args):
    if args.quiesce:
        if args.backup:raise ValueError('Quiesce and apply are separate steps')
        nc=await connect(args.url)
        try:
            await quiesce(nc.jetstream(),load_config(args.config)['account_group'])
            print(json.dumps({'status':'delivery_paused_and_drained'}))
        finally:await nc.close()
        return
    if not args.database.is_file():raise ValueError('Existing account database required')
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        source=dict(db.execute('SELECT key,value FROM metadata')).get('code_version')
    if source not in SOURCE_RELEASES | {args.target}:raise ValueError('Unreviewed source release')
    store=AccountStore(args.database,load_config(args.config),source)
    nc=None
    try:
        store.acquire_writer()
        nc=await connect(args.url)
        print(json.dumps(await upgrade(store,nc.jetstream(),args.target,args.backup)))
    finally:
        if nc is not None:await nc.close()
        store.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--target',required=True)
    p.add_argument('--url',default='nats://nats:4222')
    p.add_argument('--backup',type=Path,help='Apply only after creating this new backup; omit for inspection')
    p.add_argument('--quiesce',action='store_true',help='Pause delivery for ten minutes and wait for existing batch ACKs')
    args=p.parse_args()
    try:asyncio.run(main_async(args))
    except (ValueError,OSError) as exc:p.exit(2,f'Upgrade refused: {exc}\n')


if __name__=='__main__':main()
