"""Calendar resampling and fixed-ledger stress; public runner: evaluate.py.

Includes the sizing-study validation used only by this stress calculation.
"""
import argparse
import csv
import hashlib
import json
import math
import re
from datetime import date, timedelta
from pathlib import Path
import numpy as np
from model_development.phase_3.ledger_io import config_load, sources, ledger_load, output_begin, write_csv, write_json, receipt, equal

def scenarios(cfg):
    known = {'name','fee_multiplier','extra_roundtrip_bps','stop_extra_r','winner_gross_multiplier','drop_top_winners'}
    result = cfg.get('scenarios', [{'name':'baseline'}])
    names = set()
    for s in result:
        if set(s)-known or s['name'] in names:
            raise ValueError('Unknown scenario field or duplicate name')
        names.add(s['name'])
        if not re.fullmatch(r'[A-Za-z0-9_-]+',s['name']):
            raise ValueError('Invalid scenario name')
        for k in known-{'name','drop_top_winners'}:
            if k in s and (not math.isfinite(s[k]) or s[k]<0):
                raise ValueError('Invalid stress magnitude')
        if s.get('fee_multiplier',1)<1 or not 0<=s.get('winner_gross_multiplier',1)<=1:
            raise ValueError('Stress may not improve fees or winners')
        n=s.get('drop_top_winners',0)
        if not isinstance(n,int) or n<0:
            raise ValueError('drop_top_winners must be a nonnegative integer')
    return result



def stressed_r(data, s):
    base, fees = data['r'], data['fee_r']
    gross = base+fees
    modified = gross.copy()
    winners = base>0
    modified[winners] *= s.get('winner_gross_multiplier',1.)
    count = s.get('drop_top_winners',0)
    if count:
        ids = np.flatnonzero(winners)
        ids = ids[np.argsort(base[ids])[-count:]]
        modified[ids] = 0.  # A diagnostic missed winner: original fees still paid.
    return (modified-fees*s.get('fee_multiplier',1.)
            -s.get('extra_roundtrip_bps',0.)/10000/data['distance']
            -s.get('stop_extra_r',0.)*data['stop_remaining'])



def block_indices(days, block, count, rng):
    begins = rng.integers(0,days,(count,(days+block-1)//block))
    return ((begins[:,:,None]+np.arange(block))%days).reshape(count,-1)[:,:days]



def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))



def load_study(folder):
    folder = Path(folder)
    complete = json.loads((folder/'complete.json').read_text())
    manifest = json.loads((folder/'manifest.json').read_text())
    if manifest['tool'] != 'indicator_sizing':
        raise ValueError('Expected an indicator_sizing study')
    trades, summary, folds = (read_csv(folder/name) for name in
                             ('sized_trades.csv','summary.csv','calibration_folds.csv'))
    if len(trades) != complete['trade_rows'] or len(summary) != complete['summary_rows']:
        raise ValueError('Incomplete sizing output')
    for fold in folds:
        if int(fold['latest_train_exit_ms']) >= int(fold['calibration_cutoff_ms']):
            raise ValueError('Noncausal sizing calibration')
    return dict(manifest=manifest,trades=trades,summary=summary,folds=folds,
                hashes={name:hashlib.sha256((folder/name).read_bytes()).hexdigest()
                        for name in ('manifest.json','complete.json','sized_trades.csv','summary.csv','calibration_folds.csv')},
                path=str(folder.resolve()))



def select_sizing(data, name, spec, study, leverage):
    selector = spec['sizing_selection']
    policies = selector['policies']
    if not policies or len(set(policies)) != len(policies):
        raise ValueError('Missing/duplicate sizing policies')
    if set(policies)-{'fixed_base','indicator_sizing','fixed_train_matched'}:
        raise ValueError('Unknown sizing policy')
    m = study['manifest']
    source = next((r for r in m['sources'] if r.get('dataset') == name), None)
    if source is None or source['ledger_sha256'] != data['sha256']:
        raise ValueError('Execution ledger differs from sizing study')
    base = m['ledger_config']
    if not equal(base['max_leverage'], leverage):
        raise ValueError('Leverage differs from original sizing study')
    cap = base['risk_cap']
    low = m['config']['indicator_sizing']['low_multiplier']
    def match(r):
        return r['dataset']==name and r['family']==selector['family'] and equal(r['threshold'],selector['threshold'])
    summaries = [r for r in study['summary'] if match(r) and r['policy'] in policies]
    if len(summaries) != len(policies):
        raise ValueError('Missing/duplicate selected sizing summary')
    bounds={(r['evaluation_start'],r['evaluation_end']) for r in summaries}
    if len(bounds)!=1:
        raise ValueError('Different policy evaluation windows')
    start,end=bounds.pop()
    ids=[i for i,r in enumerate(data['rows']) if start<=r['session_day']<=end]
    if not ids:
        raise ValueError('No evaluated trades')
    d={**data,**{k:data[k][ids] for k in ('r','fee_r','distance','stop_remaining')}}
    d['rows']=[data['rows'][i] for i in ids]
    d.update(start=start,end=end,days=(date.fromisoformat(end)-date.fromisoformat(start)).days+1)
    d['offset']=np.array([(date.fromisoformat(r['session_day'])-date.fromisoformat(start)).days for r in d['rows']])
    calibration={r['test_month']:r for r in study['folds'] if match(r)}
    result=[]
    for policy in policies:
        rows=[r for r in study['trades'] if match(r) and r['policy']==policy]
        indexed={r['sample_id']:r for r in rows}
        if len(indexed)!=len(rows) or set(indexed)!={r['sample_id'] for r in d['rows']}:
            raise ValueError('Sizing must retain every execution trade in the evaluation window')
        risks=[]
        for i,t in enumerate(d['rows']):
            r=indexed[t['sample_id']]
            if (r['session_day']!=t['session_day'] or
                int(r['entry_timestamp_ms'])!=t['entry_timestamp_ms'] or
                int(r['exit_timestamp_ms'])!=t['exit_timestamp_ms'] or
                int(r['feature_as_of_ms'])>t['entry_timestamp_ms'] or
                not equal(r['net_r'],d['r'][i])):
                raise ValueError('Invalid sizing/trade causal join')
            risk=float(r['planned_risk_fraction'])
            if not np.isfinite(risk) or not 0<risk<=min(cap,leverage*d['distance'][i])+1e-12:
                raise ValueError('Invalid sizing risk')
            if policy=='fixed_base':
                requested=cap
            elif policy=='indicator_sizing':
                value=float(r['indicator_value']) if r['indicator_value'] else None
                fallback=r['feature_fallback']=='True'
                if fallback != (value is None):
                    raise ValueError('Invalid missing-feature fallback')
                multiplier=1. if fallback or value>=selector['threshold'] else low
                if not equal(r['requested_multiplier'],multiplier):
                    raise ValueError('Sizing multiplier inconsistent with indicator')
                requested=cap*multiplier
            else:
                requested=float(calibration[r['fold']]['fixed_matched_risk'])
            if not equal(risk,min(requested,leverage*d['distance'][i])):
                raise ValueError('Recorded risk inconsistent with policy')
            risks.append(risk)
        z=next(r for r in summaries if r['policy']==policy)
        risks=np.array(risks)
        equity=np.r_[base['initial_equity'],base['initial_equity']*np.cumprod(1+risks*d['r'])]
        dd=(1-equity/np.maximum.accumulate(equity)).max()
        if not equal(equity[-1],z['final_equity']) or not equal(dd,z['closed_trade_max_dd']):
            raise ValueError('Sizing summary cannot be reproduced')
        result.append((policy,risks/cap))
    return d,result



def period_stats(data, per_trade, r, equity, begin, end):
    mask=np.array([begin<=t['session_day']<=end for t in data['rows']])
    values=per_trade[mask]
    eq=np.r_[equity,equity*np.cumprod(np.maximum(0.,1+values))]
    cash=np.diff(eq);loss=-cash[cash<0].sum()
    return dict(start=begin,end=end,trades=int(mask.sum()),
        net_wr=float(np.mean(r[mask]>0)) if mask.any() else None,
        expectancy_net_r=float(np.mean(r[mask])) if mask.any() else None,
        return_pct=float(eq[-1]/equity-1),
        max_dd=float((1-eq/np.maximum.accumulate(eq)).max()),
        cash_pf=float(cash[cash>0].sum()/loss) if loss>0 else None)



def stability_periods(data):
    start,end=date.fromisoformat(data['start']),date.fromisoformat(data['end'])
    middle=start+timedelta(days=((end-start).days+1)//2)
    result=[('early_half',str(start),str(middle-timedelta(days=1))),
            ('late_half',str(middle),str(end))]
    current=start.replace(day=1)
    while current<=end:
        nxt=(current.replace(day=28)+timedelta(days=4)).replace(day=1)
        result.append((str(current)[:7],str(max(start,current)),str(min(end,nxt-timedelta(days=1)))))
        current=nxt
    return result



def path_stats(returns, equity):
    # Insolvency is absorbing; do not allow a later return to resurrect capital.
    wealth=np.cumprod(np.maximum(0.,1+returns),axis=-1)
    peak=np.maximum(1.,np.maximum.accumulate(wealth,axis=-1))
    dd=np.max(1-wealth/peak,axis=-1)
    return equity*wealth[...,-1],dd



def resample_paths(daily, equity, *, block_days=7, simulations=5000, seed=20260927, chunk_size=128):
    """Existing circular-calendar blocks and absorbing-zero path calculator."""
    daily=np.asarray(daily,dtype=float)
    if any(type(v) is not int or v<1 for v in (block_days,simulations,chunk_size)) or type(seed) is not int or seed<0:
        raise ValueError('Resampling counts must be positive integers and seed nonnegative')
    if (daily.ndim!=1 or not len(daily) or not np.isfinite(daily).all()
            or min(block_days,simulations,chunk_size)<1 or not math.isfinite(equity) or equity<=0):
        raise ValueError('Invalid daily returns/equity/resampling parameters')
    rng=np.random.default_rng(seed);finals=[];drawdowns=[]
    for begin in range(0,simulations,chunk_size):
        count=min(chunk_size,simulations-begin)
        ix=block_indices(len(daily),block_days,count,rng)
        final,dd=path_stats(daily[ix],equity);finals.extend(final);drawdowns.extend(dd)
    return np.array(finals),np.array(drawdowns)



def shuffle_trade_paths(returns, equity, *, simulations=500, seed=20260927):
    """Permute every complete net trade return once per geometric equity path."""
    returns = np.asarray(returns, dtype=float)
    if (type(simulations) is not int or simulations < 1
            or type(seed) is not int or seed < 0):
        raise ValueError('Shuffle simulations must be a positive integer and seed nonnegative')
    if (returns.ndim != 1 or not len(returns) or not np.isfinite(returns).all()
            or not math.isfinite(equity) or equity <= 0):
        raise ValueError('Invalid trade returns or initial equity for shuffle')
    rng = np.random.default_rng(seed)
    paths = np.empty((simulations, len(returns) + 1), dtype=float)
    paths[:, 0] = equity
    factors = np.maximum(0., 1. + returns)
    with np.errstate(over='raise', invalid='raise'):
        for index in range(simulations):
            paths[index, 1:] = equity * np.cumprod(factors[rng.permutation(len(returns))])
    if not np.isfinite(paths).all():
        raise ValueError('Nonfinite shuffled equity path')
    return paths


def shuffled_performance_bands(paths):
    """Mean and P95 for each prefix of a trade-shuffled equity path."""
    wealth = paths[:, 1:] / paths[:, :1]
    drawdown = 1 - paths[:, 1:] / np.maximum.accumulate(paths, axis=1)[:, 1:]
    losing = paths[:, 1:] < paths[:, :-1]
    current = np.zeros(len(paths), dtype=int)
    longest = np.zeros_like(losing, dtype=int)
    for column in range(losing.shape[1]):
        current = np.where(losing[:, column], current + 1, 0)
        longest[:, column] = current
    return {name: np.column_stack((metric.mean(axis=0), np.quantile(metric, .95, axis=0))).tolist()
            for name, metric in (
                ("net_loss", np.maximum(0, 1 - wealth)),
                ("drawdown", np.maximum.accumulate(drawdown, axis=1)),
                ("loss_streak", np.maximum.accumulate(longest, axis=1)),
            )}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--output-dir')
    p.add_argument('--check-inputs',action='store_true',help='Validate sizing/ledger joins without simulations')
    p.add_argument('--simulations',type=int,default=5000);p.add_argument('--seed',type=int,default=20260927)
    p.add_argument('--chunk-size',type=int,default=128,help='Memory bound; do not change when comparing runs')
    a=p.parse_args()
    if a.simulations<1 or a.chunk_size<1:p.error('Positive simulation count/chunk size required')
    cfg,root=config_load(a.config);ss=scenarios(cfg)
    sizes=cfg['risk_fractions'];blocks=cfg.get('block_days',[7,14]);limits=cfg.get('dd_thresholds',[.1,.2])
    equity=float(cfg.get('initial_equity',1000));leverage=float(cfg.get('max_leverage',5))
    if not sizes or any(not math.isfinite(f) or not 0<f<1 for f in sizes):p.error('Risk fractions must be in (0,1)')
    if not blocks or any(not isinstance(b,int) or b<1 for b in blocks):p.error('Invalid block days')
    if any(not 0<x<1 for x in limits) or min(equity,leverage)<=0 or not all(map(math.isfinite,[equity,leverage])):p.error('Invalid limits/equity/leverage')
    study=None
    if cfg.get('sizing_study'):
        study=load_study(root/cfg['sizing_study'])
    loaded=[];provenance=[]
    for name,path,spec in sources(cfg,root):
        d=ledger_load(path,spec)
        if study:
            d,policies=select_sizing(d,name,spec,study,leverage)
        else:
            policies=[('fixed_fraction',np.ones(len(d['r'])))]
        loaded.append((name,d,policies))
        print(f'{name}: {len(d["r"])} evaluation trades; {len(policies)} policies validated',flush=True)
        n=len(d['r'])
        provenance.append(dict(dataset=name,path=str(path),sha256=d['sha256'],trades=n,start=d['start'],end=d['end']))
    if a.check_inputs:
        print('Checks complete. No stress simulations run.')
        return
    if not a.output_dir:p.error('--output-dir required unless --check-inputs')
    out=output_begin(a.output_dir);results=[];historical=[];curves=[];stability=[];paired=[]
    for name,d,policies in loaded:
        days=d['days'];n=len(d['r']);draws={}
        for s in ss:
            r=stressed_r(d,s)
            for risk in sizes:
                for policy,multipliers in policies:
                    tag=dict(dataset=name,policy=policy,scenario=s['name'],risk_fraction=risk)
                    actual_risks=np.minimum(risk*multipliers,leverage*d['distance'])
                    per_trade=actual_risks*r
                    # Resample complete (outcome x entry sizing) pairs, never separate them.
                    daily=np.zeros(days);daily[d['offset']]=per_trade
                    final,dd=path_stats(daily,equity)
                    eq=np.r_[equity,equity*np.cumprod(np.maximum(0,1+per_trade))]
                    cash=np.diff(eq);wins=cash[cash>0].sum();loss=-cash[cash<0].sum()
                    historical.append(dict(**tag,trades=n,start=d['start'],end=d['end'],
                        mean_planned_stop_risk=float(np.mean(actual_risks)),
                        net_wr=float(np.mean(r>0)),expectancy_net_r=float(np.mean(r)),final_equity=float(final),max_dd=float(dd),
                        cash_pf=float(wins/loss) if loss>0 else None,insolvent=bool(final==0)))
                    for day,value in zip([None]+[t['session_day'] for t in d['rows']],eq):
                        curves.append(dict(**tag,session_day=day,equity=float(value)))
                    if s['name']=='baseline':
                        for period,lo,hi in stability_periods(d):
                            stability.append(dict(**tag,period=period,**period_stats(d,per_trade,r,equity,lo,hi)))
                    for block in blocks:
                        # Same day indices for each exit, scenario and sizing with the same horizon.
                        f,draw=resample_paths(daily,equity,block_days=block,simulations=a.simulations,
                                              seed=a.seed,chunk_size=a.chunk_size)
                        draws[(s['name'],risk,block,policy)]=(f,draw)
                        row=dict(**tag,block_days=block,
                            horizon_days=days,simulations=a.simulations,equity_p05=float(np.quantile(f,.05)),
                            equity_p50=float(np.median(f)),equity_p95=float(np.quantile(f,.95)),
                            dd_p50=float(np.median(draw)),dd_p95=float(np.quantile(draw,.95)),dd_p99=float(np.quantile(draw,.99)),
                            frequency_finish_below_initial=float(np.mean(f<equity)),frequency_insolvent=float(np.mean(f==0)))
                        for limit in limits:row[f'frequency_dd_gt_{limit:g}']=float(np.mean(draw>limit))
                        results.append(row)
            print(f'{name} / {s["name"]} complete',flush=True)
        for (scenario,risk,block,policy),(f,dd) in draws.items():
            if policy!='indicator_sizing':continue
            for control in ('fixed_base','fixed_train_matched'):
                key=(scenario,risk,block,control)
                if key not in draws:continue
                cf,cd=draws[key]
                paired.append(dict(dataset=name,scenario=scenario,risk_fraction=risk,block_days=block,
                    challenger=policy,control=control,simulations=a.simulations,
                    frequency_higher_equity=float(np.mean(f>cf)),frequency_lower_dd=float(np.mean(dd<cd)),
                    frequency_both_better=float(np.mean((f>cf)&(dd<cd))),
                    equity_difference_p05=float(np.quantile(f-cf,.05)),equity_difference_p50=float(np.median(f-cf)),
                    equity_difference_p95=float(np.quantile(f-cf,.95)),dd_difference_p50=float(np.median(dd-cd))))
    write_csv(out/'stress_summary.csv',results);write_csv(out/'historical_sizing.csv',historical)
    write_csv(out/'historical_equity.csv',curves)
    if stability:write_csv(out/'period_stability.csv',stability)
    if paired:write_csv(out/'paired_sizing_comparison.csv',paired)
    rec=receipt(cfg,provenance,'stress',a)
    if study:
        rec['sizing_source']=dict(path=study['path'],sha256=study['hashes'])
        rec['limitations']+=' Sizing policies selected on inspected history. Recorded capped risk/outcome pairs resampled jointly; no refitting on bootstrap paths. Initial calibration excluded identically. No new OOS claim. HYPE fixed_base uses only the common evaluation window, not an EMA rule.'
    write_json(out/'manifest.json',rec)
    write_json(out/'complete.json',dict(datasets=len(provenance),combinations=len(results)))
    (out/'README.md').write_text('Stress results are conditional ledger scenarios, not new tick replays or probabilities of future survival.\n'
        'Each dataset is an independent account. No cross-asset portfolio. No automatic recommended size.\n'
        'Compare equal exit/sizing/horizon and block length. Fees/slippage/gap stresses do not modify entry timing.\n'
        'Read manifest.json and package README.md for definitions. complete.json is written last.\n',encoding='utf-8')



if __name__ == "__main__":
    main()
