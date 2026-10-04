"""Real JetStream scratch test: outbox ack loss, redelivery, independent consumers.

Creates/deletes only its uniquely named scratch stream; no real data subjects,
accounts or trading services are modified.
"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from .broker import connect,flush_outbox
from .collector import Collector
from .store import DataStore


async def test(url):
    from nats.js.api import StreamConfig,StorageType,ConsumerConfig,AckPolicy
    nc=await connect(url)
    name='GG_TEST_'+uuid.uuid4().hex[:12].upper()
    subject='test.phase2.'+name
    js=nc.jetstream()
    created=False
    try:
        await js.add_stream(config=StreamConfig(name=name,subjects=[subject],storage=StorageType.FILE,
            max_bytes=1024*1024,max_age=600,duplicate_window=120))
        created=True
        with tempfile.TemporaryDirectory() as folder:
            store=DataStore(Path(folder)/'collector.sqlite','binance',environment='local-dev')
            try:
                c=Collector(store,{'ETHUSDC':'ETHUSDC'})
                tick=dict(symbol='ETHUSDC',agg_trade_id=1,timestamp_ms=1,price=100.,quantity=1.,buy=True)
                c.consume('ETHUSDC',[tick],2)
                class Proxy:
                    def __init__(self): self.first=True
                    async def publish(self,ignored,payload,**kwargs):
                        ack=await js.publish(subject,payload,**kwargs)
                        if self.first:
                            self.first=False
                            raise OSError('simulated lost acknowledgment after broker persistence')
                        return ack
                proxy=Proxy()
                try:
                    await flush_outbox(store,proxy)
                except OSError:
                    pass
                assert len(store.pending())==1
                await flush_outbox(store,proxy)
                assert not store.pending()
                assert (await js.stream_info(name)).state.messages==1
                a=await js.pull_subscribe(subject,'a',stream=name,
                    config=ConsumerConfig(durable_name='a',ack_policy=AckPolicy.EXPLICIT,ack_wait=1))
                b=await js.pull_subscribe(subject,'b',stream=name,
                    config=ConsumerConfig(durable_name='b',ack_policy=AckPolicy.EXPLICIT,ack_wait=1))
                first=(await a.fetch(1,timeout=3))[0]
                second=(await b.fetch(1,timeout=3))[0]
                assert first.data==second.data
                await second.ack_sync(timeout=3)
                # Consumer A does not acknowledge; a reconnect/redelivery must not lose it.
                again=(await a.fetch(1,timeout=3))[0]
                assert again.data==first.data and again.metadata.num_delivered >= 2
                await again.ack_sync(timeout=3)
                print(json.dumps(dict(status='pass',ack_loss=True,deduplicated=True,
                    independent_consumers=True,redelivery=True,real_orders=False)),flush=True)
            finally:
                store.close()
    finally:
        if created:
            await js.delete_stream(name)
        await nc.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='nats://nats:4222')
    asyncio.run(test(p.parse_args().url))
