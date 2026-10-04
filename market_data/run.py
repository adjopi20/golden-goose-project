"""Independent Binance/Lighter public collectors; never evaluates or places orders."""
import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import signal
import time

import requests
from websockets.exceptions import ConnectionClosed

from live_engine.adapters.binance import BinancePublic, UnrecoverableGap
from live_engine.adapters.lighter_public import LighterPublic, INTERNAL_SYMBOLS
from .broker import connect, initialize, flush_outbox
from .collector import Collector
from .store import DataStore, Backpressure


def read_config(path):
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if cfg.get('schema_version') != 1 or cfg.get('environment') != 'server-paper' or cfg.get('product') != 'futures':
        raise ValueError('Expected Phase 2 server-paper futures config')
    if cfg.get('venue') not in ('binance','lighter') or not cfg.get('symbols') or len(set(cfg['symbols'])) != len(cfg['symbols']):
        raise ValueError('Invalid collector subscription registry')
    if cfg['venue']=='lighter' and set(cfg['symbols']) != set(INTERNAL_SYMBOLS):
        raise ValueError('Current Lighter public adapter supports ETH/BNB/HYPE only')
    return cfg


def next_item(iterator):
    try:
        return False,next(iterator)
    except StopIteration:
        return True,None


def repair_batch(iterator):
    rows=[]
    for _ in range(500):
        try:
            rows.append(next(iterator))
        except StopIteration:
            break
    return rows


async def run(cfg, database, action):
    store = DataStore(database,cfg['venue'],max_outbox_bytes=cfg['max_outbox_bytes'])
    try:
        if action=='status':
            print(json.dumps(store.status(),indent=2))
            return
        store.acquire_writer()
        api = BinancePublic() if cfg['venue']=='binance' else LighterPublic()
        registry = await asyncio.to_thread(api.validate_markets,cfg['symbols']) if cfg['venue']=='binance' else await asyncio.to_thread(api.validate_markets)
        native = {s:s if cfg['venue']=='binance' else str(registry[s]['market_id']) for s in cfg['symbols']}
        collector = Collector(store,native)
        print(json.dumps(dict(venue=cfg['venue'],markets=registry,mode='collector_shadow',real_orders=False)),flush=True)
        if action=='warmup':
            if cfg['venue']!='binance':
                raise ValueError('Lighter delta warmup cannot use Binance or price-only historical candles')
            end = (await asyncio.to_thread(api.server_time))//60_000*60_000
            for symbol in cfg['symbols']:
                rows = await asyncio.to_thread(lambda:list(api.minute_history(symbol,end-25*86_400_000,end)))
                collector.market.seed_minutes(symbol,rows)
            print('Warmup complete; collection must cover the full native requested profile.',flush=True)
            return
        nc = await connect(cfg['nats_url'])
        try:
            js = nc.jetstream()
            await initialize(js)
            # A restart with a full queue must recover publications BEFORE adding
            # registry events or reopening a feed; otherwise capacity would deadlock.
            while store.pending(1):
                await flush_outbox(store,js)
            collector.received_ms = int(time.time()*1000)
            with store.transaction():
                for symbol, metadata in registry.items():
                    collector.emit(symbol,'instrument',collector.received_ms,metadata,quality='partial')
            stopping = False
            def stop(*args):
                nonlocal stopping
                stopping = True
            for sig in (signal.SIGINT,signal.SIGTERM):
                signal.signal(sig,stop)
            buffers = defaultdict(list)
            last_flush = last_prune = time.monotonic()

            async def publish():
                try:
                    await flush_outbox(store,js)
                except Exception as exc:
                    print(json.dumps(dict(state='BROKER_UNAVAILABLE',error=type(exc).__name__,pending=store.status()['pending_events'])),flush=True)
                    return False
                return True

            async def flush():
                nonlocal last_flush
                for symbol in cfg['symbols']:
                    while buffers[symbol]:
                        batch = buffers[symbol][:500]
                        try:
                            collector.consume(symbol,batch,int(time.time()*1000))
                        except Backpressure:
                            if not await publish():
                                await asyncio.sleep(1)
                            # Preserve buffer; do not advance upstream checkpoint while blocked.
                            if stopping:
                                raise Backpressure('Shutdown with uncommitted buffer; restart will repair')
                            continue
                        except ValueError as exc:
                            # Contain malformed market input; other instruments still flush.
                            collector.gap(symbol,int(time.time()*1000),'invalid_input:'+str(exc))
                            buffers[symbol].clear()
                            print(json.dumps(dict(state='MARKET_INVALID',symbol=symbol,action='reset_coverage')),flush=True)
                            break
                        del buffers[symbol][:len(batch)]
                await publish()
                with store.transaction():
                    store.set_meta('last_loop_ms',int(time.time()*1000))
                last_flush = time.monotonic()

            delay = 1
            while not stopping:
                try:
                    if cfg['venue']=='binance':
                        iterator = api.stream(cfg['symbols'])
                    else:
                        cursors = {s:(collector.market.state(s) or {}).get('cursor',{}).get('agg_trade_id') for s in cfg['symbols']}
                        iterator = api.stream(cursors,heartbeats=True)
                    while not stopping:
                        ended, item = await asyncio.to_thread(next_item,iterator)
                        if ended:
                            raise OSError('Public feed ended')
                        if cfg['venue']=='binance' and item is not None:
                            symbol = item['symbol']
                            prev = buffers[symbol][-1] if buffers[symbol] else (collector.market.state(symbol) or {}).get('cursor')
                            try:
                                if prev:
                                    repaired_iterator=iter(api.repair(symbol,prev,item))
                                    while True:
                                        repaired=await asyncio.to_thread(repair_batch,repaired_iterator)
                                        if not repaired:
                                            break
                                        buffers[symbol].extend(repaired)
                                        await flush()
                            except UnrecoverableGap as exc:
                                await flush()
                                collector.gap(symbol,int(time.time()*1000),str(exc))
                            buffers[symbol].append(item)
                        elif cfg['venue']=='lighter' and item is not None:
                            symbol, rows, reset = item
                            if symbol is not None:
                                if reset:
                                    await flush()
                                    collector.gap(symbol,int(time.time()*1000),'venue_history_cannot_bridge_reconnect')
                                for tick in rows:
                                    buffers[symbol].append(tick)
                                    if len(buffers[symbol]) >= 500:
                                        await flush()
                        if time.monotonic()-last_flush >= 1 or sum(map(len,buffers.values())) >= 500:
                            await flush()
                            delay = 1
                        if time.monotonic()-last_prune >= 3600:
                            collector.prune(int(time.time()*1000))
                            last_prune = time.monotonic()
                    iterator.close()
                except (ConnectionClosed,TimeoutError,OSError,requests.RequestException,ValueError) as exc:
                    await flush()
                    print(json.dumps(dict(state='FEED_DISCONNECTED',venue=cfg['venue'],error=type(exc).__name__,retry_seconds=delay)),flush=True)
                    await asyncio.sleep(delay)
                    delay = min(delay*2,30)
            await flush()
        finally:
            await nc.close()
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--action',choices=('run','warmup','status'),default='run')
    args = p.parse_args()
    asyncio.run(run(read_config(args.config),args.database,args.action))


if __name__=='__main__':
    main()
