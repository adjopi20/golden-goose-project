"""Read a selected completed research account; never replay or edit its source."""
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

IDENTITY=('model_id','instrument','configuration_id','submodel_id','portfolio_id')
DAY=86400000


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def identify(row,meta):
    return tuple(str(row.get(k) if row.get(k) is not None else meta.get(k))
                 if row.get(k) is not None or meta.get(k) is not None else None for k in IDENTITY)


def declarations(meta):
    result=[]
    for job in meta.get('jobs',[]):
        child=job.get('metadata')
        if child:
            for account in child.get('accounts',[]):
                result.append((identify(account,child),child,account))
        elif 'portfolio_id' in job:
            result.append((identify(job,meta),meta,job))
    return result


def close(a,b):return math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-8)


def load_run(folder,selectors):
    folder=folder.resolve();paths=[folder/'run.json',folder/'trades.parquet']
    fingerprints={p.name:digest(p) for p in paths}
    meta=json.loads(paths[0].read_text(encoding='utf-8-sig'))
    if meta.get('status')!='complete':raise ValueError('Source run must be complete')
    if meta.get('phase')!='backtest' and not str(meta.get('protocol','')).startswith('global_observe'):
        raise ValueError('Unsupported source run schema; use explicit legacy tools for older ledgers')
    file=pq.ParquetFile(paths[1])
    names=set(file.schema_arrow.names);file.close()
    required={'record_type','trade_id','entry_timestamp_ms','exit_timestamp_ms','entry_price','stop',
              'quantity','initial_risk','gross_pnl','fees','net_pnl','gross_R','net_R',
              'feature_as_of_ms','feature_available_at_ms','decision_timestamp_ms','entry_eligible_timestamp_ms'}
    if not required<=names:raise ValueError('Source master lacks required accounting fields')
    optional=set(IDENTITY)|{'entry_agg_trade_id','exit_agg_trade_id','equity_before_entry','equity_after_exit',
                            'exit_reason','stop_trigger_timestamp_ms','tp1_filled','remaining_exit_quantity'}
    table=pq.read_table(paths[1],columns=sorted(required|(optional&names)))
    rows=table.to_pylist()
    if any(r['record_type'] not in ('closed_trade','position_block') for r in rows):
        raise ValueError('Unknown source record type')
    closed=sum(r['record_type']=='closed_trade' for r in rows)
    expected=meta.get('closed_trades',meta.get('counts',{}).get('closed_trades'))
    if expected is None and meta.get('jobs'):
        values=[j.get('metadata',j).get('closed_trades',j.get('counts',{}).get('closed_trades')) for j in meta['jobs']]
        if all(v is not None for v in values):expected=sum(values)
    if expected is not None and expected!=closed:raise ValueError('Source master/metadata trade counts disagree')
    declared=declarations(meta)
    candidates={identify(r,meta) for r in rows}
    candidates.update(i for i,_,_ in declared)
    if not candidates:candidates.add(identify({},meta))
    def selected(identity):
        return all(not selectors.get(k) or identity[IDENTITY.index(k)]==selectors[k] for k in IDENTITY)
    matches=[i for i in candidates if selected(i)]
    if len(matches)!=1:
        choices=[dict(zip(IDENTITY,i)) for i in sorted(candidates,key=str)][:8]
        raise ValueError(f'Select exactly one model/asset/config/submodel/account; candidates={choices}')
    identity=matches[0]
    chosen=[r for r in rows if identify(r,meta)==identity]
    if any(any(r.get(k) is not None for k in ('gross_pnl','net_pnl','fees'))
           for r in chosen if r['record_type']=='position_block'):
        raise ValueError('Position blocks must not contain monetary outcomes')
    trades=[r for r in chosen if r['record_type']=='closed_trade']
    child,account=meta,None
    for i,submeta,a in declared:
        if i==identity:child,account=submeta,a;break
    if meta.get('protocol','').startswith('global_observe') and meta.get('jobs'):
        matching=[j for j in meta['jobs'] if j.get('instrument')==identity[1] and j.get('configuration_id')==identity[2]]
        if len(matching)==1:child={**meta,**matching[0]}
    trades.sort(key=lambda r:(r['exit_timestamp_ms'],r.get('exit_agg_trade_id') or 0,r['trade_id']))
    seen=set();previous=(-1,-1)
    for r in trades:
        if not isinstance(r['trade_id'],str) or r['trade_id'] in seen:raise ValueError('Duplicate/invalid closed trade identity')
        seen.add(r['trade_id'])
        for k in ('entry_timestamp_ms','exit_timestamp_ms'):
            if type(r[k]) is not int or r[k]<0:raise ValueError('Invalid trade timestamps')
        times=[r[k] for k in ('feature_as_of_ms','feature_available_at_ms','decision_timestamp_ms',
                             'entry_eligible_timestamp_ms','entry_timestamp_ms')]
        if any(type(t) is not int or t<0 for t in times) or times!=sorted(times):
            raise ValueError('Source feature/decision/fill timing is inconsistent')
        entry_order=(r['entry_timestamp_ms'],r.get('entry_agg_trade_id') if r.get('entry_agg_trade_id') is not None else -1)
        if entry_order<=previous or r['exit_timestamp_ms']<r['entry_timestamp_ms']:
            raise ValueError('Overlapping or reversed positions require a supported capital adapter')
        previous=(r['exit_timestamp_ms'],r.get('exit_agg_trade_id') if r.get('exit_agg_trade_id') is not None else -1)
        for k in ('entry_price','stop','quantity','initial_risk','gross_pnl','fees','net_pnl','gross_R','net_R'):
            if not isinstance(r[k],(int,float)) or not math.isfinite(r[k]):raise ValueError('Missing/nonfinite accounting')
        if min(r['entry_price'],r['stop'],r['quantity'],r['initial_risk'])<=0 or r['fees']<0:
            raise ValueError('Invalid risk/geometry/fee')
        if (not close(r['quantity']*abs(r['entry_price']-r['stop']),r['initial_risk'])
                or not close(r['gross_pnl']-r['fees'],r['net_pnl'])
                or not close(r['net_pnl']/r['initial_risk'],r['net_R'])
                or not close(r['gross_pnl']/r['initial_risk'],r['gross_R'])):
            raise ValueError('Source risk/gross/net/fee reconciliation failed')
    if account is not None and account.get('closed_trades')!=len(trades):raise ValueError('Selected account count mismatch')
    window=child.get('window',{})
    if 'replay_window_ms' in child:
        bounds=child['replay_window_ms']
        if not isinstance(bounds,(list,tuple)) or len(bounds)!=2:
            raise ValueError('Invalid explicit replay coverage window')
        lower,upper=bounds
    elif window.get('effective_start_utc'):
        import pandas as pd
        lower=int(pd.Timestamp(window['effective_start_utc']).timestamp()*1000)
        upper=int(pd.Timestamp(window['effective_end_exclusive_utc']).timestamp()*1000)
    elif 'start_ms' in child and 'end_ms' in child:
        lower,upper=child['start_ms'],child['end_ms']
    elif 'cache' in child and 'source_start_ms' in child['cache']:
        lower,upper=child['cache']['source_start_ms'],child['cache']['source_end_ms']+1
    elif 'source_min_ms' in child and 'source_max_ms' in child:
        lower,upper=child['source_min_ms'],child['source_max_ms']+1
    else:raise ValueError('Source lacks an explicit coverage window; cannot invent resampling days from trade extrema')
    if type(lower) is not int or type(upper) is not int or not 0<=lower<upper:raise ValueError('Invalid source coverage bounds')
    if any(not lower<=r['entry_timestamp_ms']<=r['exit_timestamp_ms']<upper for r in trades):
        raise ValueError('Source bounds do not cover all selected outcomes; no silent crossing-trade exclusion')
    origin=lower//DAY*DAY;days=(upper-origin+DAY-1)//DAY
    offsets=np.array([(r['exit_timestamp_ms']-origin)//DAY for r in trades],dtype=int)
    if child.get('phase',meta.get('phase'))=='backtest':
        initial=child.get('config',{}).get('initial_equity')
        if not isinstance(initial,(int,float)) or not math.isfinite(initial) or initial<=0:
            raise ValueError('Backtest source lacks valid initial equity')
        equity=float(initial)
        for r in trades:
            before,after=r.get('equity_before_entry'),r.get('equity_after_exit')
            if (not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (before,after))
                    or before<=0 or not close(before,equity) or not close(after,before+r['net_pnl'])):
                raise ValueError('Observed account equity is incomplete or inconsistent')
            equity=after
        if account is not None and not close(account['metrics']['final_equity'],equity):raise ValueError('Source final equity mismatch')
    else:initial=None
    source_summary=dict(closed_trades=len(trades),excluded_other_account_trades=closed-len(trades),
        position_blocks_excluded=sum(r['record_type']=='position_block' for r in chosen),
        gross_pnl=sum(r['gross_pnl'] for r in trades),fees=sum(r['fees'] for r in trades),net_pnl=sum(r['net_pnl'] for r in trades),
        gross_total_R=sum(r['gross_R'] for r in trades),net_total_R=sum(r['net_R'] for r in trades),
        gross_expectancy_R=float(np.mean([r['gross_R'] for r in trades])) if trades else None,
        net_expectancy_R=float(np.mean([r['net_R'] for r in trades])) if trades else None)
    counts=child.get('counts',{}).get('omitted_open_positions',{})
    selected_open=account.get('open_positions_omitted') if account is not None else counts.get(identity[3])
    context=dict(selected_open_positions_omitted=selected_open,
        censoring=child.get('censoring',child.get('open_trade_policy',meta.get('open_trade_policy'))),
        cost_configuration={k:child.get(k,child.get('config',{}).get(k)) for k in ('fee_bps','entry_fee_bps','exit_fee_bps')})
    return dict(folder=folder,hashes=fingerprints,metadata=meta,account_metadata=child,identity=dict(zip(IDENTITY,identity)),
        trades=trades,initial_equity=initial,offset=offsets,days=int(days),calendar_origin_ms=origin,
        source_bounds_ms=[lower,upper],source_summary=source_summary,source_context=context)


def verify_source(data):
    if any(digest(data['folder']/name)!=expected for name,expected in data['hashes'].items()):
        raise ValueError('Selected source run changed during evaluation')
