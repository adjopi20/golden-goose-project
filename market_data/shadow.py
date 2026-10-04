"""Independent durable observation consumer. No model evaluation or orders."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import time

from trading_core.contracts import canonical_json,validate_event
from .broker import connect,STREAMS


class ReceiptStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS receipts(event_id TEXT PRIMARY KEY,stream TEXT NOT NULL,
            sequence INTEGER NOT NULL,event_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS checkpoints(stream TEXT PRIMARY KEY,sequence INTEGER NOT NULL,last_receipt_ms INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS warnings(id INTEGER PRIMARY KEY,stream TEXT,reason TEXT,time_ms INTEGER);
          CREATE INDEX IF NOT EXISTS receipts_retention ON receipts(stream,received_ms);
        ''')

    def apply(self, stream, sequence, event, now_ms):
        payload = canonical_json(validate_event(event))
        with self.db:
            checkpoint = self.db.execute('SELECT sequence FROM checkpoints WHERE stream=?',(stream,)).fetchone()
            if checkpoint and sequence > checkpoint[0]+1:
                self.db.execute('INSERT INTO warnings(stream,reason,time_ms) VALUES (?,?,?)',
                    (stream,'delivery_sequence_gap: replay interval not verified',now_ms))
            prior = self.db.execute('SELECT payload FROM receipts WHERE event_id=?',(event['event_id'],)).fetchone()
            if prior and prior[0] != payload:
                raise ValueError('Same event ID, conflicting payload; consumer must not silently overwrite')
            self.db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?,?)',
                (event['event_id'],stream,sequence,event['event_timestamp_ms'],now_ms,payload))
            self.db.execute('INSERT INTO checkpoints VALUES (?,?,?) ON CONFLICT(stream) DO UPDATE SET '
                'sequence=MAX(sequence,excluded.sequence),last_receipt_ms=excluded.last_receipt_ms',(stream,sequence,now_ms))

    def check_retention(self, stream, first_sequence, now_ms):
        prior = self.db.execute('SELECT sequence FROM checkpoints WHERE stream=?',(stream,)).fetchone()
        if prior and first_sequence > prior[0]+1:
            with self.db:
                self.db.execute('INSERT INTO warnings(stream,reason,time_ms) VALUES (?,?,?)',
                    (stream,'retention_gap: cannot claim continuous replay',now_ms))

    def prune(self, now_ms):
        with self.db:
            self.db.execute('DELETE FROM receipts WHERE stream=? AND received_ms<?',('GG_EXEC_V1',now_ms-2*3600_000))
            self.db.execute('DELETE FROM receipts WHERE stream=? AND received_ms<?',('GG_MARKET_V1',now_ms-72*3600_000))

    def status(self):
        return dict(real_orders=False,checkpoints=[dict(zip(('stream','sequence','last_receipt_ms'),r)) for r in self.db.execute('SELECT * FROM checkpoints')],
                    receipts=self.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],
                    warnings=self.db.execute('SELECT COUNT(*) FROM warnings').fetchone()[0])


async def fetch_batch(sub):
    """An empty pull is normal; both asyncio and NATS timeouts inherit this type."""
    try:
        return await sub.fetch(100,timeout=1)
    except TimeoutError:
        return []


async def consume(url, database, consumer_prefix):
    from nats.js.api import ConsumerConfig,AckPolicy,DeliverPolicy
    store = ReceiptStore(database)
    nc = await connect(url)
    try:
        js = nc.jetstream()
        subscriptions = []
        for spec in STREAMS:
            info = await js.stream_info(spec['name'])
            store.check_retention(spec['name'],info.state.first_seq,int(time.time()*1000))
            durable = consumer_prefix+'_'+spec['name']
            subscriptions.append((spec['name'],await js.pull_subscribe(spec['subjects'][0],durable,
                stream=spec['name'],config=ConsumerConfig(durable_name=durable,ack_policy=AckPolicy.EXPLICIT,
                deliver_policy=DeliverPolicy.ALL,ack_wait=30,max_ack_pending=200))))
        async def pump(stream, sub):
            while True:
                messages = await fetch_batch(sub)
                for msg in messages:
                    store.apply(stream,msg.metadata.sequence.stream,json.loads(msg.data),int(time.time()*1000))
                    await msg.ack()  # only after local transaction commits
        async def maintenance():
            while True:
                await asyncio.sleep(60)
                store.prune(int(time.time()*1000))
                print(json.dumps(store.status()),flush=True)
        await asyncio.gather(*(pump(stream,sub) for stream,sub in subscriptions),maintenance())
    finally:
        await nc.close()
        store.db.close()


if __name__=='__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='nats://nats:4222')
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--consumer-prefix',default='phase2_shadow')
    p.add_argument('--status',action='store_true')
    args = p.parse_args()
    if args.status:
        store = ReceiptStore(args.database)
        print(json.dumps(store.status(),indent=2))
        store.db.close()
    else:
        asyncio.run(consume(args.url,args.database,args.consumer_prefix))
