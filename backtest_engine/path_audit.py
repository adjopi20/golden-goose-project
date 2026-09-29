"""Reusable, stop-bounded raw path observation. Not a portfolio backtester."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def read_rows(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, allow_nan=False), encoding="utf-8")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_signal(s):
    if s['direction'] not in ('long', 'short'):
        raise ValueError('Invalid direction')
    sign = 1 if s['direction'] == 'long' else -1
    if not np.isfinite(s['entry']) or s['entry'] <= 0:
        raise ValueError('Invalid entry')
    if s['force_exit_timestamp_ms'] <= s['entry_timestamp_ms']:
        raise ValueError('Invalid horizon')
    if s['feature_as_of_ms'] > s['entry_timestamp_ms']:
        raise ValueError('Future feature snapshot')
    if 'baseline' not in s['stops']:
        raise ValueError('Missing baseline stop')
    for stop in s['stops'].values():
        if not np.isfinite(stop) or stop <= 0 or sign*(s['entry']-stop) <= 0:
            raise ValueError('Stop is not on risk side')


def audit_arrays(s, timestamp, price, ids, targets, fee_bps):
    """Touch metrics end at trigger; fills use next print, never post-stop MFE."""
    validate_signal(s)
    timestamp, price, ids = map(np.asarray, (timestamp, price, ids))
    if len(price) < 2 or not (len(price) == len(timestamp) == len(ids)):
        raise ValueError('Incomplete path')
    if (np.diff(timestamp) < 0).any() or (np.diff(ids) <= 0).any():
        raise ValueError('Unordered or duplicate ticks')
    if not np.isfinite(price).all() or (price <= 0).any():
        raise ValueError('Bad prices')
    if int(ids[0]) != s['entry_fill_agg_trade_id'] or int(timestamp[0]) != s['entry_timestamp_ms'] or abs(price[0]-s['entry']) > 1e-8:
        raise ValueError('Cached entry differs from actual fill')
    targets = sorted(set(float(t) for t in targets))
    if not targets or any(not np.isfinite(t) or t <= 0 for t in targets) or not 0 <= fee_bps < 10000:
        raise ValueError('Invalid targets/fees')
    cutoff = int(np.searchsorted(timestamp, s['force_exit_timestamp_ms']))
    if cutoff+1 >= len(price):
        raise ValueError('Missing next-print force exit')
    sign = 1 if s['direction'] == 'long' else -1
    change = sign*(price-s['entry'])
    result = []
    for label, stop in s['stops'].items():
        risk = sign*(s['entry']-stop)
        crossed = np.flatnonzero(sign*(price[:cutoff]-stop) <= 0)
        end = int(crossed[0]) if len(crossed) else cutoff
        reason = 'initial_stop' if len(crossed) else 'next_day_0929'
        # No milestone at or after the time cutoff. Stop crossing cannot be positive R.
        eligible = change[:end]/risk
        path = change[:end+1]/risk
        mfe = max(0., float(eligible.max())) if len(eligible) else 0.
        base = {k:s[k] for k in ('sample_id','session_day','route','direction')}
        base.update(s.get('context', {}))
        base.update(stop_variant=label, stop=stop, entry=s['entry'], risk_pct=risk/s['entry'],
                    mfe_r=mfe, mfe_pct=mfe*risk/s['entry'], mae_r=min(0.,float(path.min())),
                    terminal_reason=reason, terminal_trigger_ms=int(timestamp[end]),
                    terminal_fill_ms=int(timestamp[end+1]), terminal_fill_price=float(price[end+1]),
                    terminal_gross_r=float(change[end+1]/risk),
                    terminal_net_r=float((change[end+1]-(s['entry']+price[end+1])*fee_bps/10000)/risk),
                    nominal_roundtrip_fee_r=2*s['entry']*fee_bps/10000/risk)
        for target in targets:
            hits = np.flatnonzero(eligible >= target)
            hit = int(hits[0]) if len(hits) else None
            trigger = hit if hit is not None else end
            fill = trigger+1
            later_targets = [t for t in targets if t > target]
            next_hits = np.flatnonzero(eligible >= later_targets[0]) if later_targets else []
            segment_end = int(next_hits[0]) if len(next_hits) else end
            segment = path[hit:segment_end+1] if hit is not None else np.array([])
            pullback = float((np.maximum.accumulate(segment)-segment).max()) if len(segment) else None
            result.append({**base, 'target_r':target, 'hit_before_stop_or_cutoff':hit is not None,
                'hit_timestamp_ms':int(timestamp[hit]) if hit is not None else None,
                'minutes_to_hit':float((timestamp[hit]-timestamp[0])/60000) if hit is not None else None,
                'max_pullback_r_until_next_target_or_terminal':pullback,
                'returned_to_initial_stop_after_hit':hit is not None and reason == 'initial_stop',
                'exit_reason':'target' if hit is not None else reason,
                'exit_fill_timestamp_ms':int(timestamp[fill]), 'exit_fill_price':float(price[fill]),
                'net_r':float((change[fill]-(s['entry']+price[fill])*fee_bps/10000)/risk)})
    return result


def cached_path(raw, signal, cache_dir):
    # Raw schema reader is shared with the existing replay, not reimplemented here.
    from models.orb.scripts.pre_ny_submodels.audit_pre_ny_early_immediate_tail_geometry_cp002 import _iter_ticks
    raw = Path(raw).resolve()
    stat = raw.stat()
    identity = dict(schema=1, raw=str(raw), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    entry_ms=signal['entry_timestamp_ms'], entry_id=signal['entry_fill_agg_trade_id'],
                    cutoff=signal['force_exit_timestamp_ms'])
    key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    cache_dir.mkdir(parents=True,exist_ok=True)
    path, meta = cache_dir/(key+'.parquet'), cache_dir/(key+'.json')
    if path.exists() and meta.exists():
        saved = json.loads(meta.read_text())
        if saved['identity'] != identity or digest(path) != saved['sha256']:
            raise ValueError('Corrupt path cache; use a new cache directory')
        return path, True
    tmp = cache_dir/(key+'.partial')
    schema = pa.schema([('timestamp_ms',pa.int64()),('price',pa.float64()),('agg_trade_id',pa.int64())])
    buffer=[]; found=False; cutoff_seen=False; complete=False; count=0; last=None
    with pq.ParquetWriter(tmp,schema,compression='zstd') as writer:
        for tick in _iter_ticks(pq.ParquetFile(raw), SimpleNamespace(**signal)):
            t, p, i = tick['timestamp_ms'], tick['price'], tick['agg_trade_id']
            if not found:
                if i != signal['entry_fill_agg_trade_id']:
                    continue
                if t != signal['entry_timestamp_ms'] or abs(p-signal['entry'])>1e-8:
                    raise ValueError('Raw entry mismatch')
                found=True
            if last and (t<last[0] or i<=last[1]):
                raise ValueError('Raw path ordering failure')
            last=(t,i)
            buffer.append(dict(timestamp_ms=t,price=p,agg_trade_id=i)); count+=1
            if len(buffer)>=100000:
                writer.write_table(pa.Table.from_pylist(buffer,schema=schema)); buffer=[]
            if cutoff_seen:
                complete=True
                break
            cutoff_seen=t>=signal['force_exit_timestamp_ms']
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer,schema=schema))
    if not complete:
        raise ValueError(f"Incomplete raw horizon for {signal['sample_id']}; no fabricated exit")
    tmp.replace(path)
    write_json(meta,dict(identity=identity,sha256=digest(path),ticks=count))
    return path, False


def summarize(rows):
    groups=defaultdict(list)
    for r in rows:
        for dimension in ('all','macro_direction','volatility_state','alignment','historical_period'):
            for route in ('all',r['route']):
                groups[(route,r['stop_variant'],r['target_r'],dimension,r.get(dimension,'all'))].append(r)
    output=[]
    for key, rs in groups.items():
        values=[r['net_r'] for r in rs]; losses=-sum(min(x,0) for x in values)
        output.append(dict(zip(('route','stop_variant','target_r','dimension','state'),key))|
            dict(n=len(rs),wins=sum(v>0 for v in values),wr=sum(v>0 for v in values)/len(rs),
                 target_hit_rate=sum(r['hit_before_stop_or_cutoff'] for r in rs)/len(rs),
                 expectancy=sum(values)/len(rs),pf=sum(max(v,0) for v in values)/losses if losses else None,
                 median_mfe_r=float(np.median([r['mfe_r'] for r in rs])),
                 median_mfe_pct=float(np.median([r['mfe_pct'] for r in rs]))))
    return output


def paired_comparison(rows):
    baseline={(r['sample_id'],r['target_r']):r for r in rows if r['stop_variant']=='baseline'}
    return [dict(sample_id=r['sample_id'],route=r['route'],target_r=r['target_r'],
        stop_variant=r['stop_variant'],stop_changed=r['stop']!=baseline[r['sample_id'],r['target_r']]['stop'],
        baseline_net_r=baseline[r['sample_id'],r['target_r']]['net_r'],challenger_net_r=r['net_r'],
        baseline_winner_to_loser=baseline[r['sample_id'],r['target_r']]['net_r']>0 and r['net_r']<=0,
        baseline_loser_to_winner=baseline[r['sample_id'],r['target_r']]['net_r']<=0 and r['net_r']>0)
        for r in rows if r['stop_variant']!='baseline']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args(); cfg=json.loads(a.experiment.read_text())
    signals=read_rows(cfg['signals'])
    if not signals or len({s['sample_id'] for s in signals})!=len(signals):
        raise ValueError('Empty/duplicate signal universe')
    for s in signals: validate_signal(s)
    a.output_dir.mkdir(parents=True,exist_ok=False)
    all_rows=[]
    for idx,s in enumerate(signals,1):
        path,reused=cached_path(cfg['input'],s,Path(cfg['path_cache_dir']))
        table=pq.read_table(path)
        rows=audit_arrays(s,*(table[c].to_numpy() for c in ('timestamp_ms','price','agg_trade_id')),
                          cfg['targets_r'],cfg['fee_bps'])
        all_rows.extend(rows)
        print(json.dumps(dict(event='path_audited',sample_id=s['sample_id'],completed=idx,total=len(signals),cache_reused=reused)),flush=True)
    with (a.output_dir/'trade_target_audit.jsonl').open('w') as f:
        for r in all_rows: f.write(json.dumps(r,allow_nan=False)+'\n')
    write_json(a.output_dir/'regime_summary.json',summarize(all_rows))
    write_json(a.output_dir/'matched_stop_comparison.json',paired_comparison(all_rows))
    write_json(a.output_dir/'manifest.json',dict(experiment=cfg,signals_sha256=digest(cfg['signals']),
        engine_sha256=digest(__file__),trades=len(signals),status='complete',
        warning='Historical descriptive audit, not untouched OOS, portfolio equity, or order-book simulation.'))


if __name__=='__main__': main()
