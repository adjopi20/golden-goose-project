"""Explicit ETH-only bridge from the frozen strategy's proposals to a live owner.

Reads the existing strategy DB read-only, never its paper position/fill ledger.
Execution is OFF unless both config and command-line acknowledgement enable it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

from live_engine.adapters.binance_futures_execution import ACKNOWLEDGEMENT, FuturesOrders, VenueError
from live_engine.adapters.binance_futures_readonly import PreflightError
from live_engine.futures_execution import FuturesExecutor
from live_engine.futures_preflight import number
from live_engine.futures_store import FuturesStore
from trading_core.contracts import canonical_json, content_hash, make_intention, validate_intention


def load_policy(path):
    c = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    fixed = dict(schema_version=1, symbol='ETHUSDC', allocation_id='tf-preny-eth-pilot-v1',
                 initial_equity='200', contract='TF-PRENY-ETH-001', source_config_hash='80eddfd8c661c80eb3d03a81532ec70abf20e3637b9116c043b054fb78c047ee',
                 source_code_version='e2917f054494682b4e5ba703631089263d03156e',
                 stop_trigger='CONTRACT_PRICE', tp1_r='1', tp1_fraction='0.1',
                 max_notional_multiple='5', poll_seconds=2)
    if set(c) != set(fixed) | {'execution_enabled', 'accept_new_entries'} or any(c[k] != v for k, v in fixed.items()):
        raise ValueError('Unreviewed pilot policy change')
    if any(type(c[k]) is not bool for k in ('execution_enabled', 'accept_new_entries')):
        raise ValueError('Execution switches must be JSON booleans')
    return c


def live_proposal(p, evaluation, config, source_meta):
    p = validate_intention(p)
    if (p['environment'], p['venue'], p['product'], p['instrument_id'], p['account_id'], p['allocation_id'],
        p['contract_version'], p['config_hash'], p['model_id'], p['model_version']) != (
            'server-paper', 'binance', 'futures', 'ETHUSDC', 'tf-preny-eth-v1', 'tf-preny-eth-v1',
            'TF-PRENY-ETH-001', config['source_config_hash'], 'trend_following_preny_profile_15m',
            'trend_following_preny_profile_15m_v1'):
        raise ValueError('Proposal not from explicitly bound frozen ETH producer')
    snapshot = evaluation.get('snapshot')
    if evaluation.get('selected') is not True or evaluation.get('state') != 'ENTRY_READY' or not isinstance(snapshot, dict):
        raise ValueError('Missing selected causal evaluation')
    if content_hash(snapshot) != p['decision_snapshot_ref'] or snapshot.get('feature_as_of_ms') != p['feature_as_of_ms']:
        raise ValueError('Causal decision snapshot mismatch')
    if p['calibration_id'] != source_meta['calibration_hash'] or p['feature_version'] != 'phase1-frozen-v1':
        raise ValueError('Calibration/feature identity mismatch')
    trend = snapshot.get('trend_15m') or {}
    if trend.get('status') != 'ready' or trend.get('end_ms') != p['feature_as_of_ms']:
        raise ValueError('EMA200 not ready at decision time')
    gap = number(trend['price_vs_ema200_pct'], signed=True)
    expected = .005 if gap * (1 if p['direction'] == 'long' else -1) >= number('.5') else .0025
    if p['risk_fraction'] != expected or snapshot.get('risk_fraction') != expected:
        raise ValueError('Sizing tier differs from causal EMA200 snapshot')
    if number(snapshot['profile']['poc']) != number(p['initial_stop']):
        raise ValueError('Stop differs from frozen profile POC')
    fields = {k: v for k, v in p.items() if k not in ('schema_version', 'intention_id')}
    fields.update(environment='server-live', account_id=config['allocation_id'], allocation_id=config['allocation_id'])
    return make_intention(**fields)


def import_proposals(source_path, executor, now_ms):
    s, c = executor.s, executor.c
    uri = Path(source_path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        meta = dict(db.execute('SELECT key,value FROM metadata'))
        if tuple(meta.get(k) for k in ('owner', 'schema', 'environment', 'venue', 'config_hash', 'code_version')) != (
                'preny_strategy_shadow', '3', 'server-paper', 'binance', c['source_config_hash'], c['source_code_version']):
            raise ValueError('Source strategy identity changed; review required')
        age = now_ms - int(meta.get('last_poll_ms', 0))
        if not 0 <= age <= 180000: raise ValueError('Source strategy heartbeat stale/future')
        identity = content_hash({k: meta.get(k) for k in ('owner', 'schema', 'environment', 'venue', 'config_hash', 'code_version', 'calibration_hash', 'source_epoch')})
        if s.meta('source_identity', identity) != identity: raise ValueError('Source database replaced/changed')
        maximum = db.execute('SELECT COALESCE(MAX(rowid),0) FROM proposals').fetchone()[0]
        cursor = s.meta('source_cursor')
        if cursor is None:
            with s.transaction():
                s.set_meta('source_identity', identity)
                s.set_meta('source_cursor', maximum)
                s.audit('source_bootstrap_no_backfill', dict(cursor=maximum))
            return 0
        if int(cursor) > maximum: raise ValueError('Source proposal history rewound')
        count = 0
        for row in db.execute('SELECT rowid,payload FROM proposals WHERE rowid>? ORDER BY rowid LIMIT 100', (int(cursor),)):
            p = json.loads(row['payload'])
            with s.transaction():
                if p.get('instrument_id') == 'ETHUSDC':
                    evaluation = db.execute('SELECT payload FROM evaluations WHERE symbol=? AND asof=?',
                                            ('ETHUSDC', p['feature_as_of_ms'])).fetchone()
                    if not evaluation: raise ValueError('Missing causal source evaluation')
                    converted = live_proposal(p, json.loads(evaluation[0]), c, meta)
                    executor.offer(converted, now_ms)
                    count += 1
                s.set_meta('source_cursor', row['rowid'])
        return count


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--database', type=Path)
    p.add_argument('--source-strategy-db', type=Path)
    p.add_argument('--private-key-file', type=Path)
    p.add_argument('--passphrase-file', type=Path)
    p.add_argument('--action', choices=('validate', 'run', 'status', 'backup', 'health'), default='validate')
    p.add_argument('--backup-to', type=Path)
    p.add_argument('--acknowledge-real-money', default='')
    args = p.parse_args()
    cfg = load_policy(args.config)
    if args.action == 'validate':
        print(canonical_json(dict(policy_valid=True, execution_enabled=cfg['execution_enabled'],
                                  accept_new_entries=cfg['accept_new_entries'], orders_submitted=0)))
        return
    if not args.database: p.error('--database required')
    if args.action in ('status', 'health'):
        with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro', uri=True) as db:
            meta = dict(db.execute('SELECT key,value FROM metadata'))
            if args.action == 'health':
                age = int(time.time()*1000) - int(meta.get('worker_heartbeat_ms', 0))
                pending = db.execute("SELECT 1 FROM orders o JOIN sessions s ON s.session=o.session_id "
                                     "WHERE s.state NOT IN ('CLOSED','SKIPPED','DISARMED') AND "
                                     "(o.state IN ('SENDING','UNKNOWN') OR o.cancel_pending=1 OR o.error IS NOT NULL) LIMIT 1").fetchone()
                raise SystemExit(0 if 0 <= age < 60000 and not meta.get('worker_error') and not meta.get('entries_blocked') and not pending else 1)
            print(canonical_json(dict(metadata=meta,
                                      order_intention_count=db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],
                                      native_acknowledged_orders=db.execute('SELECT COUNT(*) FROM orders WHERE response IS NOT NULL').fetchone()[0],
                                      actual_fill_count=db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],
                                      diagnostic_event_count=db.execute('SELECT COUNT(*) FROM audit').fetchone()[0])))
        return
    if args.action == 'backup':
        if not args.backup_to or args.backup_to.resolve() == args.database.resolve():
            p.error('backup needs a NEW different --backup-to path')
        args.backup_to.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.backup_to, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro', uri=True) as source:
            with sqlite3.connect(args.backup_to) as destination:
                source.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup verification failed; do not use this artifact')
        print(canonical_json(dict(backup_complete=True)))
        return
    if cfg['execution_enabled'] and args.acknowledge_real_money != ACKNOWLEDGEMENT:
        p.error('Execution requested without explicit real-money acknowledgement')
    store = FuturesStore(args.database, cfg)
    client = None
    try:
        store.acquire_writer()
        if not args.source_strategy_db: p.error('--source-strategy-db required')
        client = FuturesOrders(execution_enabled=cfg['execution_enabled'], acknowledgement=args.acknowledge_real_money,
                               api_key=os.environ.get('BINANCE_API_KEY'), private_key_file=args.private_key_file,
                               passphrase_file=args.passphrase_file)
        executor = FuturesExecutor(store, client, cfg)
        with store.transaction(): store.set_meta('worker_armed', int(client.armed))
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        last_sync = 0
        while not stopped.is_set():
            now_ms, source_ok, errors = int(time.time()*1000), True, []
            try:
                import_proposals(args.source_strategy_db, executor, now_ms)
            except (ValueError, KeyError, TypeError, sqlite3.Error, OSError) as e:
                source_ok = False; errors.append('source_not_ready:'+str(e) if isinstance(e, ValueError) else 'source_not_ready:'+type(e).__name__)
            try:
                if client.armed and time.monotonic()-last_sync >= 60:
                    client.sync_time(); last_sync = time.monotonic()
                executor.cycle(now_ms, new_entries_ok=source_ok)
            except (VenueError, PreflightError, ValueError, KeyError, TypeError, sqlite3.Error, OSError) as e:
                errors.append('execution_reconcile_error:'+str(e) if isinstance(e, (ValueError, VenueError)) else 'execution_reconcile_error:'+type(e).__name__)
            with store.transaction():
                store.set_meta('worker_heartbeat_ms', now_ms)
                store.set_meta('worker_error', '; '.join(errors))
            if errors or now_ms % 60000 < cfg['poll_seconds']*1000:
                print(canonical_json(dict(**store.status(), execution_enabled=client.armed, errors=errors)), flush=True)
            stopped.wait(cfg['poll_seconds'])
    finally:
        if client is not None: client.close()
        store.close()


if __name__ == '__main__': main()
