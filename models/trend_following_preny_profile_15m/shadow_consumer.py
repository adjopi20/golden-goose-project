"""Phase 3 independent strategy subscriber. No orders, accounts or fill simulation."""
import argparse
import asyncio
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import re
import time

from live_engine.strategy_store import StrategyStore
from market_data.broker import connect, flush_outbox
from market_data.shadow import fetch_batch
from trading_core.contracts import canonical_json, content_hash
from trading_core.session import NY, clock_ms
from .runtime.evaluator import ACCOUNT_IDS, NotReady
from .runtime.intention import as_intention
from .runtime.observer import SessionObserver
from .runtime.preparation import paper_activation_signal

MODEL = 'trend_following_preny_profile_15m'


def load_config(path, calibration_path=None):
    cfg = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (cfg.get('schema_version'), cfg.get('mode'), cfg.get('venue')) not in (
            (1, 'paper', 'binance'), (1, 'paper', 'lighter')):
        raise ValueError('Expected frozen venue-native paper configuration')
    accounts = cfg['accounts']
    if len(accounts) != 3 or {a['symbol']:a['id'] for a in accounts} != ACCOUNT_IDS:
        raise ValueError('Expected exactly the three frozen allocations')
    calibration = json.loads(Path(calibration_path).read_text()) if calibration_path else None
    if calibration is not None and calibration.get('venue') != cfg['venue']:
        raise ValueError('C1 calibration must be venue native')
    return cfg, calibration


class ShadowStrategy:
    def __init__(self, store, cfg, calibration=None, now_ms=None, publish_intentions=False):
        self.store, self.cfg, self.calibration = store, cfg, calibration
        self.now_ms = now_ms or (lambda: int(time.time()*1000))
        self.publish_intentions = publish_intentions
        mode = str(int(publish_intentions))
        if store.get_meta('publish_intentions',mode) != mode:
            raise ValueError('Publication mode changed; use a fresh strategy release database')
        with store.transaction():
            store.set_meta('publish_intentions',mode)
            if publish_intentions: store.enable_publication()
        self.accounts = {a['symbol']:a for a in cfg['accounts']}
        self.native = ({s:s for s in self.accounts} if cfg['venue']=='binance' else
                       {'ETHUSDC':'0', 'BNBUSDC':'25', 'HYPEUSDT':'24'})
        self.symbols = {native:s for s,native in self.native.items()}
        self.reconstructing = False
        self.observer = SessionObserver(store, calibration=calibration, now_ms=self.now_ms,
            strict_coverage=cfg['venue']=='lighter',
            min_native_reference_sessions=10 if cfg['venue']=='lighter' else 0,
            activate=self.propose)

    def propose(self, evaluated, ready_ms):
        # Publication is opt-in; Phase 3 remains store-only.
        if self.reconstructing: return dict(proposal_state='BOOTSTRAP_ONLY')
        signal = paper_activation_signal(evaluated['signal'], ready_ms)
        if not evaluated['selected']: return dict(proposal_state='SELECTOR_REJECTED')
        signal['entry_deadline_timestamp_ms'] = min(signal['entry_deadline_timestamp_ms'],
                                                  signal['feature_as_of_ms']+300_000)
        symbol = signal['symbol']
        a = self.accounts[symbol]
        intention = as_intention({**evaluated, 'signal':signal}, venue=self.cfg['venue'],
            product='futures', instrument_id=self.native[symbol], environment='server-paper',
            account_id=a['id'], allocation_id=a['id'], contract_version=a['contract'],
            config_hash=self.store.get_meta('config_hash'), feature_version='phase1-frozen-v1',
            calibration_id=self.store.get_meta('calibration_hash'),
            decision_snapshot_ref=content_hash(evaluated['snapshot']))
        with self.store.transaction():
            self.store.db.execute('INSERT INTO proposals VALUES (?,?)',
                                  (intention['intention_id'], canonical_json(intention)))
            if self.publish_intentions: self.store.queue_intention(intention)
        return dict(proposal_state='QUEUED_PAPER' if self.publish_intentions else 'SHADOW_ONLY',
                    intention_id=intention['intention_id'])

    def boundary(self, symbol, asof, state):
        day = datetime.fromtimestamp(asof/1000,NY).date()
        if self.store.get_meta('gap_at:'+symbol) and state['coverage_start_ms'] > clock_ms(day,1):
            result = dict(state='NOT_READY',session_day=str(day),as_of_ms=asof,candidate=False,
                          reason='Shared feed reset; full native profile continuity required')
            self.store.db.execute('INSERT OR IGNORE INTO evaluations VALUES (?,?,?)',
                                  (symbol,asof,canonical_json(result)))
            return
        result = self.observer.boundary(symbol, asof, state)
        if result is not None:
            result['provenance'] = {k:self.store.get_meta(k) for k in (
                'venue','environment','code_version','config_hash','calibration_hash')}
            result['mode'] = 'strategy_shadow'
            self.store.db.execute('UPDATE evaluations SET payload=? WHERE symbol=? AND asof=?',
                                  (canonical_json(result),symbol,asof))

    def reconstruct_today(self):
        """Rebuild the session lock, never retroactively propose an entry."""
        if self.store.get_meta('session_reconstructed'):
            return
        day = datetime.fromtimestamp(self.now_ms()/1000, NY).date()
        self.reconstructing = True
        try:
            with self.store.transaction():
                for symbol in self.accounts:
                    raw = self.store.get_meta('bootstrap_feed:'+symbol)
                    if not raw: continue
                    feed = json.loads(raw)
                    confirmed = min(self.now_ms(),feed['cursor']['timestamp_ms'])//900_000*900_000
                    for asof in range(clock_ms(day,9),min(clock_ms(day,12),confirmed)+1,900_000):
                        self.boundary(symbol,asof,feed)
                self.store.set_meta('session_reconstructed', 1)
        finally:
            self.reconstructing = False

    def event(self, event):
        symbol = self.symbols.get(event['instrument_id'])
        if symbol is None: raise ValueError('Unconfigured native instrument')
        if event['revision'] != 0: raise ValueError('Revision requires explicit causal recovery review')
        p, kind = event['payload'], event['event_type']
        if kind == 'completed_bar':
            if event['quality'] != 'complete' or p['source'] != 'aggregate_trades':
                raise ValueError('Invalid completed native minute')
            if p['close_timestamp_ms']-p['open_timestamp_ms'] != 60_000:
                raise ValueError('Expected minute aggregation')
            bar = {k:p[k] for k in ('timestamp_ms','open','high','low','close','volume','delta')}
            if (bar['timestamp_ms']!=p['open_timestamp_ms'] or bar['timestamp_ms']%60_000
                or not all(isinstance(bar[k],(int,float)) and not isinstance(bar[k],bool) and math.isfinite(bar[k])
                           for k in ('open','high','low','close','volume','delta'))
                or not 0 < bar['low'] <= min(bar['open'],bar['close']) <= max(bar['open'],bar['close']) <= bar['high']
                or bar['volume'] < abs(bar['delta'])-1e-9):
                raise ValueError('Invalid native minute geometry/volume')
            payload = canonical_json(bar)
            old = self.store.db.execute('SELECT payload FROM minutes WHERE symbol=? AND timestamp_ms=?',
                                        (symbol,p['open_timestamp_ms'])).fetchone()
            if old and json.loads(old[0]) != bar: raise ValueError('Conflicting minute history')
            self.store.db.execute('INSERT OR IGNORE INTO minutes VALUES (?,?,?,?)',
                                  (symbol,p['open_timestamp_ms'],payload,'aggregate_trades'))
        elif kind == 'profile':
            old = self.store.db.execute('SELECT payload FROM profiles WHERE symbol=? AND session=?',
                                        (symbol,p['session_day'])).fetchone()
            if old and json.loads(old[0]) != p: raise ValueError('Conflicting frozen profile')
            self.store.db.execute('INSERT OR IGNORE INTO profiles VALUES (?,?,?)',
                                  (symbol,p['session_day'],canonical_json(p)))
        elif kind == 'evaluation_boundary':
            self.boundary(symbol,p['as_of_ms'],p)
            row = self.store.db.execute('SELECT payload FROM evaluations WHERE symbol=? AND asof=?',
                                        (symbol,p['as_of_ms'])).fetchone()
            if row:
                result=json.loads(row[0])
                result['input_delivery']={k:event[k] for k in ('event_id','available_at_ms',
                    'received_timestamp_ms','stream_sequence','feature_version')}
                self.store.db.execute('UPDATE evaluations SET payload=? WHERE symbol=? AND asof=?',
                                      (canonical_json(result),symbol,p['as_of_ms']))
        elif kind == 'coverage' and event['quality'] in ('gap','invalid'):
            self.store.set_meta('gap_at:'+symbol,event['event_timestamp_ms'])
        # Coverage is also durable evidence; readiness uses boundary's updated
        # coverage watermark. No synthetic bar or cross-venue fallback is added.


async def consume(args):
    from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy
    cfg, calibration = load_config(args.config,args.c1_calibration)
    store = StrategyStore(args.database,venue=cfg['venue'],config_hash=content_hash(cfg),
        calibration_hash=content_hash(calibration),code_version=args.code_version)
    nc = None
    try:
        store.acquire_writer()
        nc = await connect(args.url)
        js = nc.jetstream()
        publish = getattr(args,'publish_intentions',False)
        if publish: await js.stream_info('GG_INTENT_V1')
        info = await js.stream_info('GG_MARKET_V1')
        store.bootstrap(args.bootstrap_database, symbols=ACCOUNT_IDS,
            broker_sequence=info.state.last_seq, now_ms=int(time.time()*1000))
        strategy = ShadowStrategy(store,cfg,calibration,publish_intentions=publish)
        strategy.reconstruct_today()
        store.check_retention(info.state.first_seq)
        prefix = getattr(args,'durable_prefix','phase3')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',prefix): raise ValueError('Invalid durable prefix')
        if store.get_meta('durable_prefix',prefix) != prefix: raise ValueError('Durable prefix changed')
        with store.transaction(): store.set_meta('durable_prefix',prefix)
        durable = f'{prefix}_{MODEL}_{cfg["venue"]}'
        # A durable is coupled to this persistent DB; lost/reset DB is not
        # permission to attach to an older durable and silently skip its input.
        try:
            existing = await js.consumer_info('GG_MARKET_V1',durable)
        except Exception as exc:
            from nats.js.errors import NotFoundError
            if not isinstance(exc,NotFoundError): raise
            existing = None
        if existing and not store.get_meta('durable_registered'):
            raise ValueError('Existing durable but new database; use explicit reviewed reset')
        if not existing and store.get_meta('durable_registered'):
            raise ValueError('Strategy durable lost; explicit recovery review required')
        if existing and existing.ack_floor.stream_seq > int(store.get_meta('broker_sequence')):
            raise ValueError('Database is behind broker acknowledgments; recovery review required')
        config = None if existing else ConsumerConfig(durable_name=durable,ack_policy=AckPolicy.EXPLICIT,
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                opt_start_seq=int(store.get_meta('broker_sequence'))+1,
                ack_wait=120,max_ack_pending=100)
        sub = await js.pull_subscribe(f'md.v1.{cfg["venue"]}.futures.>',durable,
                                      stream='GG_MARKET_V1', config=config)
        with store.transaction(): store.set_meta('durable_registered',1)
        maintenance = time.monotonic()-60
        while True:
            # Check even during idle/reconnect; retention outside the persistent
            # cursor must fail closed, not pretend the remaining stream is whole.
            info = await js.stream_info('GG_MARKET_V1')
            store.check_retention(info.state.first_seq)
            messages = await fetch_batch(sub)
            for msg in messages:
                event = json.loads(msg.data)
                store.apply(msg.metadata.sequence.stream,event,int(time.time()*1000),strategy.event)
                await msg.ack()
            if publish: await flush_outbox(store,js)
            if time.monotonic()-maintenance >= 60:
                now_ms=int(time.time()*1000)
                with store.transaction():
                    store.set_meta('last_poll_ms',now_ms)
                    store.prune(now_ms)
                print(canonical_json(store.status()),flush=True)
                maintenance = time.monotonic()
    finally:
        if nc is not None: await nc.close()
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='nats://nats:4222')
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--bootstrap-database',type=Path)
    p.add_argument('--config',type=Path)
    p.add_argument('--c1-calibration',type=Path)
    p.add_argument('--code-version')
    p.add_argument('--publish-intentions',action='store_true')
    p.add_argument('--durable-prefix',default='phase3')
    p.add_argument('--status',action='store_true')
    p.add_argument('--healthcheck',action='store_true')
    args = p.parse_args()
    if args.status or args.healthcheck:
        with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True) as db:
            meta = dict(db.execute('SELECT key,value FROM metadata'))
            if args.healthcheck:
                age = int(time.time()*1000)-int(meta.get('last_poll_ms',0))
                raise SystemExit(0 if age <= 180_000 else 1)
            print(json.dumps(dict(metadata=meta,decisions=db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0],
                proposals=db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0],real_orders=False,
                latest={s:json.loads(payload) for s,payload in db.execute(
                    'SELECT symbol,payload FROM evaluations e WHERE asof=(SELECT MAX(asof) FROM evaluations WHERE symbol=e.symbol)')}),indent=2))
        return
    if not all((args.config,args.bootstrap_database,args.code_version)):
        p.error('run requires config, bootstrap-database and immutable code-version')
    asyncio.run(consume(args))


if __name__=='__main__': main()
