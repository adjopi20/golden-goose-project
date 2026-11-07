"""Bounded JetStream streams and publish-after-commit outbox delivery."""
import time

STREAMS = (
    dict(name='GG_MARKET_V1',subjects=['md.v1.>'],max_bytes=256*1024*1024,max_age=72*3600),
    dict(name='GG_EXEC_V1',subjects=['exec.v1.>'],max_bytes=256*1024*1024,max_age=2*3600),
)


async def connect(url):
    import nats
    # No credentials, no public broker port; private Compose network only in Phase 2.
    return await nats.connect(url,connect_timeout=3,max_reconnect_attempts=-1,reconnect_time_wait=1)


async def initialize(js):
    from nats.js.api import StreamConfig, StorageType, RetentionPolicy, DiscardPolicy
    from nats.js.errors import NotFoundError
    for spec in STREAMS:
        config = StreamConfig(**spec,storage=StorageType.FILE,retention=RetentionPolicy.LIMITS,
                              discard=DiscardPolicy.OLD,num_replicas=1,duplicate_window=120)
        try:
            existing = await js.stream_info(spec['name'])
        except NotFoundError:
            await js.add_stream(config=config)
        else:
            for key in ('subjects','max_bytes','max_age','storage','retention','discard','num_replicas'):
                if getattr(existing.config,key) != getattr(config,key):
                    raise ValueError(f'Broker stream configuration changed: {spec["name"]}.{key}')


async def flush_outbox(store, js, limit=200):
    sent = 0
    for row in store.pending(limit):
        # Broker acknowledgment is required; Core NATS publish alone is insufficient.
        await js.publish(row['subject'],row['payload'].encode(),headers={'Nats-Msg-Id':row['event_id']},timeout=3)
        store.published(row['sequence'])
        sent += 1
    if sent:
        with store.transaction():
            store.set_meta('last_publish_ms',int(time.time()*1000))
    return sent


async def bootstrap(url):
    nc = await connect(url)
    try:
        await initialize(nc.jetstream())
        print('JetStream Phase 2 stream configuration verified.',flush=True)
    finally:
        await nc.close()


if __name__=='__main__':
    import argparse
    import asyncio
    p = argparse.ArgumentParser()
    p.add_argument('--url',required=True)
    asyncio.run(bootstrap(p.parse_args().url))
