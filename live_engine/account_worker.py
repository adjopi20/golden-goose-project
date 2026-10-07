"""Independent paper account service. Broker only; no model or exchange imports."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import time
from decimal import Decimal
import math

from live_engine.account_store import AccountStore
from live_engine.account_owner import AccountOwner
from market_data.broker import connect
from market_data.shadow import fetch_batch
from trading_core.contracts import canonical_json
from trading_core.paper_execution import Config, validate_config

INTENT_STREAM = dict(name='GG_INTENT_V1',subjects=['intent.v1.>'],
    max_bytes=32*1024*1024,max_age=72*3600)
STREAMS = ('GG_INTENT_V1','GG_MARKET_V1','GG_EXEC_V1')


def load_config(path):
    cfg=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (cfg.get('schema_version'),cfg.get('mode'),cfg.get('environment')) != (1,'paper-shadow','server-paper'):
        raise ValueError('Only independent server-paper shadow accounts are supported')
    if cfg.get('venue') not in ('binance','lighter') or not re.fullmatch(r'[a-zA-Z0-9_-]+',cfg.get('account_group','')):
        raise ValueError('Invalid venue/account group')
    if not cfg.get('strategy_config_hash') or not cfg.get('allocations'): raise ValueError('Missing allocation registry')
    ids=set()
    for a in cfg['allocations']:
        if a['id'] in ids: raise ValueError('Duplicate allocation')
        ids.add(a['id'])
        for key in ('id','symbol','instrument_id','currency','contract','model_id','model_version'):
            if not isinstance(a.get(key),str) or not a[key].strip(): raise ValueError('Invalid allocation '+key)
        execution=Config(**a['execution']); validate_config(execution)
        if not a['allowed_risks'] or execution.risk_fraction not in a['allowed_risks']:
            raise ValueError('Invalid risk registry')
        if any(type(r) not in (int,float) or not 0<r<1 for r in a['allowed_risks']):
            raise ValueError('Invalid risk tier')
    return cfg


async def initialize_intentions(url):
    from nats.js.api import StreamConfig, StorageType, RetentionPolicy, DiscardPolicy
    from nats.js.errors import NotFoundError
    nc=await connect(url)
    try:
        js=nc.jetstream()
        config=StreamConfig(**INTENT_STREAM,storage=StorageType.FILE,retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,num_replicas=1,duplicate_window=120)
        try: existing=await js.stream_info(INTENT_STREAM['name'])
        except NotFoundError: await js.add_stream(config=config)
        else:
            for key in ('subjects','max_bytes','max_age','storage','retention','discard','num_replicas'):
                if getattr(existing.config,key) != getattr(config,key): raise ValueError('Intention stream configuration changed')
    finally: await nc.close()


async def consume(args):
    from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy
    from nats.js.errors import NotFoundError
    cfg=load_config(args.config)
    store=AccountStore(args.database,cfg,args.code_version)
    nc=None; tasks=[]
    try:
        store.acquire_writer()
        owner=AccountOwner(store,cfg)
        nc=await connect(args.url); js=nc.jetstream(); subscriptions=[]
        for channel in STREAMS:
            info=await js.stream_info(channel)
            with store.transaction(): store.anchor(channel,info.state.last_seq)
            store.check_retention(channel,info.state.first_seq)
            durable=f'phase4_{cfg["account_group"]}_{channel}'
            try: existing=await js.consumer_info(channel,durable)
            except NotFoundError: existing=None
            registered=store.get_meta('durable:'+channel)
            if existing and not registered: raise ValueError('Existing account durable but missing ledger registration')
            if registered and not existing: raise ValueError('Account durable lost; explicit recovery review required')
            if existing and existing.ack_floor.stream_seq > store.checkpoint(channel):
                raise ValueError('Account DB behind broker acknowledgment')
            prefix={'GG_INTENT_V1':'intent','GG_MARKET_V1':'md','GG_EXEC_V1':'exec'}[channel]
            subject=f'{prefix}.v1.{cfg["venue"]}.futures.>'
            cc=None if existing else ConsumerConfig(durable_name=durable,ack_policy=AckPolicy.EXPLICIT,
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE,opt_start_seq=store.checkpoint(channel)+1,
                ack_wait=60,max_ack_pending=100)
            sub=await js.pull_subscribe(subject,durable,stream=channel,config=cc)
            with store.transaction(): store.set_meta('durable:'+channel,1)
            subscriptions.append((channel,sub))

        async def pump(channel, sub):
            while True:
                info=await js.stream_info(channel)
                store.check_retention(channel,info.state.first_seq)
                messages=await fetch_batch(sub)
                for msg in messages:
                    payload=json.loads(msg.data)
                    identity=payload['intention_id'] if channel=='GG_INTENT_V1' else payload['event_id']
                    handler=owner.intention if channel=='GG_INTENT_V1' else owner.event
                    store.apply(channel,msg.metadata.sequence.stream,identity,payload,int(time.time()*1000),handler)
                    await msg.ack()  # after fill + ledger + reservation + cursor commit
                with store.transaction(): store.set_meta('last_poll:'+channel,int(time.time()*1000))

        async def timer():
            last_log=0
            while True:
                now=int(time.time()*1000); owner.clock(now)
                if now-last_log>=60_000:
                    with store.transaction(): store.prune(now)
                    print(canonical_json(owner.status()),flush=True);last_log=now
                await asyncio.sleep(1)
        tasks=[asyncio.create_task(pump(channel,sub)) for channel,sub in subscriptions]
        tasks.append(asyncio.create_task(timer()))
        await asyncio.gather(*tasks)
    finally:
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        if nc is not None: await nc.close()
        store.close()


def status(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        db.row_factory=sqlite3.Row; db.execute('BEGIN')
        checks=[];books={};cash={}
        for r in db.execute('SELECT journal_key,allocation,book,amount FROM postings'):
            books[r['journal_key']]=books.get(r['journal_key'],Decimal(0))+Decimal(r['amount'])
            if r['book']=='cash':cash[r['allocation']]=cash.get(r['allocation'],Decimal(0))+Decimal(r['amount'])
        if any(v!=0 for v in books.values()):checks.append('unbalanced_accounting_postings')
        for r in db.execute('SELECT * FROM accounts'):
            state=json.loads(r['state']);definition=json.loads(r['config'])
            pnl=sum(json.loads(t[0])['pnl'] for t in db.execute(
                "SELECT payload FROM journal WHERE account=? AND kind='trade'",(r['id'],)))
            expected=definition['execution']['initial_equity']+pnl
            if not math.isclose(state['equity'],expected,rel_tol=1e-10,abs_tol=1e-8):
                checks.append('closed_equity_mismatch:'+r['id'])
            if not state['position'] and not math.isclose(float(cash.get(r['id'],0)),state['equity'],rel_tol=1e-10,abs_tol=1e-8):
                checks.append('flat_cash_equity_mismatch:'+r['id'])
            active=bool(state['position'] or state['signals'])
            reserved=bool(db.execute('SELECT 1 FROM reservations WHERE allocation=?',(r['id'],)).fetchone())
            if active!=reserved:checks.append('reservation_state_mismatch:'+r['id'])
        return dict(metadata=dict(db.execute('SELECT key,value FROM metadata')),
            accounting_checks=dict(status='pass' if not checks else 'fail',issues=checks,
                cash_balances={k:str(v) for k,v in cash.items()}),
            journal_counts={k:n for k,n in db.execute('SELECT kind,COUNT(*) FROM journal GROUP BY kind')},
            input_blocks={k:v for k,v in db.execute("SELECT key,value FROM metadata WHERE key LIKE 'blocked:%'")},
            uncertain_closes=db.execute("SELECT COUNT(*) FROM requests WHERE status='closed_uncertain'").fetchone()[0],
            checkpoints=[dict(r) for r in db.execute('SELECT * FROM checkpoints')],
            accounts=[dict(id=r['id'],symbol=r['symbol'],state=json.loads(r['state']),
                cursor=json.loads(r['cursor']) if r['cursor'] else None) for r in db.execute('SELECT * FROM accounts')],
            requests=[dict(r) for r in db.execute('SELECT intention_id,allocation,status,reason,received_ms FROM requests')],
            reservations=[dict(r) for r in db.execute('SELECT * FROM reservations')],
            deadlines=[dict(r) for r in db.execute('SELECT * FROM deadlines')],real_orders=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='nats://nats:4222')
    p.add_argument('--database',type=Path)
    p.add_argument('--config',type=Path)
    p.add_argument('--code-version')
    p.add_argument('--status',action='store_true')
    p.add_argument('--init-stream',action='store_true')
    args=p.parse_args()
    if args.init_stream: asyncio.run(initialize_intentions(args.url));return
    if not args.database: p.error('--database is required')
    if args.status: print(json.dumps(status(args.database),indent=2));return
    if not args.config or not args.code_version: p.error('--config and --code-version are required')
    asyncio.run(consume(args))


if __name__=='__main__': main()
