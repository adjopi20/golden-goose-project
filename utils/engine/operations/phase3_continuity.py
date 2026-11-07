"""Read-only shadow/collector history audit; no evaluator, repair, or account writes.

Run in a strategy container, where the collector is already mounted read-only.
The two-minute cutoff excludes the currently closing minute and transport lag.
Legacy paper warmup is deliberately not treated as the source of this history.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def snapshot(path, cutoff_ms):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        meta = dict(db.execute('SELECT key,value FROM metadata'))
        rows = {}
        for symbol, stamp, raw, source in db.execute(
                'SELECT symbol,timestamp_ms,payload,source FROM minutes '
                'WHERE timestamp_ms<=? ORDER BY symbol,timestamp_ms', (cutoff_ms,)):
            canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(',', ':'), allow_nan=False)
            rows.setdefault(symbol, {})[stamp] = (canonical, source)
        return meta, rows


def audit(strategy_path, collector_path, cutoff_ms):
    sm, strategy = snapshot(strategy_path, cutoff_ms)
    cm, collector = snapshot(collector_path, cutoff_ms)
    identity_ok = (sm.get('owner') == 'preny_strategy_shadow'
        and cm.get('owner') == 'market_data'
        and sm.get('venue') == cm.get('venue')
        and sm.get('environment') == cm.get('environment') == 'server-paper'
        and bool(sm.get('source_epoch')) and sm.get('source_epoch') == cm.get('source_epoch'))
    results = {}
    for symbol in sorted(set(strategy) | set(collector)):
        a, b = strategy.get(symbol, {}), collector.get(symbol, {})
        # Collector retention may discard rows that the strategy still holds.
        # Compare the entire shared time range, not just the current session.
        start = min(b, default=0)
        aa = {t:v for t,v in a.items() if t >= start}
        bb = {t:v for t,v in b.items() if t >= start}
        common = sorted(aa.keys() & bb.keys())
        changed = [t for t in common if aa[t] != bb[t]]
        source_only = sorted(bb.keys()-aa.keys())
        shadow_only = sorted(aa.keys()-bb.keys())
        digest = hashlib.sha256()
        for t in common:
            digest.update(f'{t}:{aa[t][0]}:{aa[t][1]}\n'.encode())
        results[symbol] = dict(strategy_rows=len(a), collector_rows=len(b),
            strategy_first_ms=min(a, default=None), collector_first_ms=min(b, default=None),
            compared_rows=len(common), mismatched_rows=len(changed),
            mismatch_examples=changed[:5], collector_only_rows=len(source_only),
            collector_only_examples=source_only[:5], shadow_only_rows=len(shadow_only),
            shadow_only_examples=shadow_only[:5], common_history_sha256=digest.hexdigest())
    passed = identity_ok and bool(results) and all(r['compared_rows'] > 0
        and r['mismatched_rows'] == r['collector_only_rows'] == r['shadow_only_rows'] == 0
        for r in results.values())
    return dict(status='passed' if passed else 'review_required', venue=sm.get('venue'),
        identity_ok=identity_ok, cutoff_ms=cutoff_ms, symbols=results, real_orders=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--strategy', type=Path, default=Path('/data/strategy.sqlite'))
    p.add_argument('--collector', type=Path, required=True)
    p.add_argument('--cutoff-ms', type=int)
    args = p.parse_args()
    cutoff = args.cutoff_ms if args.cutoff_ms is not None else int(time.time()*1000)-120_000
    result = audit(args.strategy, args.collector, cutoff)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
