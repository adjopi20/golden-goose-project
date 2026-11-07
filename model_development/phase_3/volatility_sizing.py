"""Causal entry-only volatility sizing on fixed execution ledgers, not a new replay engine."""
import argparse
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from model_development.phase_3.ledger_io import config_load, sources, ledger_load, output_begin, write_csv, write_json, receipt

VERSION = 1
TZ = 'America/New_York'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def daily_variance(minutes, symbol):
    """Minute-open stamps -> completed 15m closes -> prior NY09-to-NY09 RV.

    Missing whole 15m bars invalidate the day. Missing individual minutes are
    counted, not invented. DST days are normalized to a 24h variance equivalent.
    """
    m = minutes[['timestamp_ms', 'close', 'symbol']].copy()
    if m.empty or set(m.symbol) != {symbol}:
        raise ValueError(f'Empty/wrong-symbol candle cache: {symbol}')
    if m.timestamp_ms.isna().any() or (m.timestamp_ms % 60000 != 0).any():
        raise ValueError('Expected minute-open timestamps in UTC milliseconds')
    if not np.isfinite(m.close).all() or (m.close <= 0).any():
        raise ValueError('Nonpositive/nonfinite close')
    # Duplicate overlap must agree; do not silently choose one conflicting feed.
    dup = m[m.duplicated('timestamp_ms', keep=False)]
    if not dup.empty and (dup.groupby('timestamp_ms').close.nunique() > 1).any():
        raise ValueError('Conflicting overlapping candle caches')
    m = m.drop_duplicates('timestamp_ms').sort_values('timestamp_ms')
    idx = pd.to_datetime(m.timestamp_ms.to_numpy(), unit='ms', utc=True) + pd.Timedelta(minutes=1)
    close = pd.Series(m.close.to_numpy(), index=idx)
    bars = close.resample('15min', closed='right', label='right').last()
    counts = close.resample('15min', closed='right', label='right').count()
    rv = np.log(bars).diff().pow(2)
    lo = (bars.index.min().tz_convert(TZ) - pd.Timedelta(days=1)).date()
    hi = (bars.index.max().tz_convert(TZ) + pd.Timedelta(days=1)).date()
    boundaries = pd.date_range(str(lo), str(hi), freq='D', tz=TZ) + pd.DateOffset(hours=9)
    rows = []
    for left, right in zip(boundaries[:-1], boundaries[1:]):
        expected = int((right.tz_convert('UTC') - left.tz_convert('UTC')).total_seconds() / 900)
        segment = rv[(rv.index > left) & (rv.index <= right)]
        ct = counts[(counts.index > left) & (counts.index <= right)]
        valid = len(segment) == expected and segment.notna().all()
        rows.append(dict(session_day=str(right.date()), snapshot_ms=int(right.timestamp()*1000),
                         expected_bars=expected, valid_returns=int(segment.notna().sum()),
                         observed_minutes=int(ct.sum()), complete=bool(valid),
                         variance_24h=float(segment.sum()*96/expected) if valid else None))
    return pd.DataFrame(rows)


def forecasts(daily, half_life, warmup):
    """No backward fill: a missing day resets the estimator and warmup."""
    lam = 2**(-1/half_life)
    state, count = None, 0
    rows = []
    for r in daily.to_dict('records'):
        value = r['variance_24h']
        if not r['complete'] or not np.isfinite(value):
            state, count = None, 0
        else:
            state = float(value) if state is None else lam*state+(1-lam)*value
            count += 1
        sigma = math.sqrt(state) if count >= warmup and state is not None and state > 0 else None
        rows.append(dict(session_day=r['session_day'], snapshot_ms=int(r['snapshot_ms']),
                         sigma_24h=sigma, consecutive_valid_days=count,
                         complete=bool(r['complete'])))
    return {r['session_day']: r for r in rows}


def cached_daily(root, spec, cache_dir):
    paths = [(root/p).resolve() for p in spec['candle_caches']]
    records = [dict(path=str(p), sha256=sha256(p)) for p in paths]
    signature = dict(version=VERSION, symbol=spec['name'], sources=records,
                     timezone=TZ, snapshot='09:00', interval_minutes=15)
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    folder = cache_dir/key
    marker, data_path = folder/'complete.json', folder/'daily_variance.csv'
    if marker.is_file():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved['signature'] != signature or sha256(data_path) != saved['data_sha256']:
            raise ValueError('Derived volatility cache validation failed')
        frame = pd.read_csv(data_path)
        print(f"{spec['name']}: reuse daily volatility cache", flush=True)
    else:
        print(f"{spec['name']}: read cached candles (no raw aggTrades)", flush=True)
        frames = [pd.read_parquet(p, columns=['timestamp_ms','close','symbol']) for p in paths]
        frame = daily_variance(pd.concat(frames, ignore_index=True), spec['name'])
        folder.mkdir(parents=True, exist_ok=True)
        frame.to_csv(data_path, index=False)
        write_json(marker, dict(signature=signature, data_sha256=sha256(data_path)))
    return frame, dict(**signature, derived_cache=str(data_path), derived_sha256=sha256(data_path))


def exposure(distance, risk, leverage):
    return np.minimum(risk/distance, leverage)


def matched_risk(distance, target_mean, cap, leverage):
    low, high = 0., cap
    for _ in range(60):
        mid = (low+high)/2
        if np.mean(exposure(distance, mid, leverage)*distance) < target_mean:
            low = mid
        else:
            high = mid
    return (low+high)/2


def walk_forward_sizes(data, features, risk, leverage, min_days, min_trades):
    """Expanding monthly calibration; only exited trades may enter training.

    Target = median training baseline predicted volatility (no PnL optimization).
    Fixed matched control matches training average planned risk after caps.
    Missing features fall back to baseline for ALL policies; never drop trades.
    """
    rows = data['rows']
    first = pd.Timestamp(data['start']).date()
    months = sorted({r['session_day'][:7] for r in rows})
    output, folds = [], []
    for month in months:
        start = pd.Timestamp(month+'-01', tz=TZ)
        if (start.date()-first).days < min_days:
            continue
        start_ms = int(start.timestamp()*1000)
        train = [i for i,r in enumerate(rows) if r['exit_timestamp_ms'] < start_ms
                 and features[i]['sigma_24h'] is not None]
        if len(train) < min_trades:
            continue
        distance = data['distance'][train]
        sigma = np.array([features[i]['sigma_24h'] for i in train])
        base = exposure(distance, risk, leverage)
        target = float(np.median(base*sigma))
        challenger = np.minimum(base, target/sigma)
        train_risk = float(np.mean(challenger*distance))
        fixed = matched_risk(distance, train_risk, risk, leverage)
        folds.append(dict(test_month=month, calibration_cutoff_ms=start_ms,
                          train_trades=len(train), target_daily_vol=target,
                          fixed_matched_risk=fixed, train_challenger_mean_risk=train_risk,
                          train_baseline_mean_risk=float(np.mean(base*distance)),
                          latest_train_exit_ms=max(rows[i]['exit_timestamp_ms'] for i in train)))
        for i,r in enumerate(rows):
            if r['session_day'][:7] != month:
                continue
            d = data['distance'][i]
            base = min(risk/d, leverage)
            s = features[i]['sigma_24h']
            weights = dict(fixed_base=base, volatility_target=min(base,target/s) if s else base,
                           fixed_train_matched=min(fixed/d,leverage) if s else base)
            output.append(dict(index=i,fold=month,weights=weights,sigma=s,
                               fallback=s is None,target=target))
    return output, folds


def metrics(returns, pnl, r, risks, equity):
    wealth = np.r_[equity, equity*np.cumprod(np.maximum(0.,1+returns))]
    dd = 1-wealth/np.maximum.accumulate(wealth)
    gains, losses = pnl[pnl>0].sum(), -pnl[pnl<0].sum()
    sd = float(np.std(returns, ddof=1)) if len(returns)>1 else 0.
    top = float(np.sort(pnl[pnl>0])[-3:].sum())
    return dict(trades=len(r), net_wr=float(np.mean(r>0)), expectancy_net_r=float(np.mean(r)),
                mean_account_return_per_calendar_day=float(np.mean(returns)),
                mean_planned_stop_risk=float(np.mean(risks)),
                final_equity=float(wealth[-1]),return_pct=float(wealth[-1]/equity-1),
                closed_trade_max_dd=float(dd.max()),cash_pf=float(gains/losses) if losses else None,
                daily_sharpe=float(np.mean(returns)/sd*math.sqrt(365)) if sd else None,
                top3_positive_pnl_share=top/float(gains) if gains else None,
                insolvent=bool(wealth[-1]==0))


def simulate(data, sized, policy, initial):
    start = pd.Timestamp(sized[0]['fold']+'-01').date()
    end = pd.Timestamp(data['end']).date()
    daily = np.zeros((end-start).days+1)
    equity, out, rs, risks, pnls = initial, [], [], [], []
    for s in sized:
        i = s['index']; trade = data['rows'][i]
        f = s['weights'][policy]*data['distance'][i]
        ret = f*data['r'][i]
        before = equity
        equity *= max(0.,1+ret)
        pnl = equity-before
        day = pd.Timestamp(trade['session_day']).date()
        daily[(day-start).days] = ret
        out.append(dict(session_day=str(day), sample_id=trade['sample_id'], fold=s['fold'],
                        entry_timestamp_ms=trade['entry_timestamp_ms'],exit_timestamp_ms=trade['exit_timestamp_ms'],
                        sigma_24h=s['sigma'],feature_fallback=s['fallback'],target_daily_vol=s['target'],
                        notional_to_equity=s['weights'][policy],planned_risk_fraction=f,
                        quantity=before*s['weights'][policy]/trade['entry_price'],
                        net_r=float(data['r'][i]),fees=before*f*data['fee_r'][i],
                        equity_before=before,net_pnl=pnl,equity_after=equity))
        pnls.append(pnl);rs.append(data['r'][i]);risks.append(f)
    stat = metrics(daily,np.array(pnls),np.array(rs),np.array(risks),initial)
    stat.update(evaluation_start=str(start), evaluation_end=str(end),calendar_days=len(daily),
                feature_fallback_trades=sum(s['fallback'] for s in sized))
    monthly = []
    for month in sorted({x['fold'] for x in out}):
        subset = [x for x in out if x['fold']==month]
        gains = sum(max(x['net_pnl'],0) for x in subset)
        losses = -sum(min(x['net_pnl'],0) for x in subset)
        monthly.append(dict(month=month,trades=len(subset),net_wr=float(np.mean([x['net_r']>0 for x in subset])),
                            return_pct=subset[-1]['equity_after']/subset[0]['equity_before']-1 if subset[0]['equity_before']>0 else None,
                            net_pnl=sum(x['net_pnl'] for x in subset),cash_pf=gains/losses if losses else None,
                            mean_planned_stop_risk=float(np.mean([x['planned_risk_fraction'] for x in subset]))))
    return stat,out,monthly


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True)
    p.add_argument('--output-dir')
    p.add_argument('--check-inputs',action='store_true',help='Validate ledgers and candle schemas only; no study')
    a = p.parse_args()
    cfg,root = config_load(a.config)
    v = cfg['volatility_sizing']
    risk,leverage,initial = cfg['risk_cap'],cfg['max_leverage'],cfg['initial_equity']
    if not 0<risk<1 or min(leverage,initial)<=0 or not all(map(math.isfinite,[risk,leverage,initial])):
        p.error('Invalid risk cap/leverage/equity')
    halves = v['half_lives_days'];warmup=v['warmup_days']
    if not halves or len(set(halves))!=len(halves) or any(not isinstance(h,int) or h<1 for h in halves):
        p.error('Positive distinct integer half-lives required')
    if warmup<3*max(halves) or v['min_train_days']<1 or v['min_train_trades']<2:
        p.error('Require >=3 half-lives warmup and positive training requirements')
    selected = list(sources(cfg,root)); loaded=[]
    for name,path,spec in selected:
        d = ledger_load(path,spec)
        for c in spec['candle_caches']:
            import pyarrow.parquet as pq
            schema = pq.read_schema(root/c)
            if not {'timestamp_ms','close','symbol'}.issubset(schema.names):
                raise ValueError(f'Invalid candle schema: {c}')
        loaded.append((name,d,spec))
        print(f'{name}: validated {len(d["rows"])} trades',flush=True)
    if a.check_inputs:
        print('Input checks complete. No backtest or candle processing run.')
        return
    if not a.output_dir:p.error('--output-dir required unless --check-inputs')
    out=output_begin(a.output_dir)
    cache_dir=(root/v['cache_dir']).resolve()
    summaries=[];trades=[];months=[];folds=[];feature_rows=[];exclusions=[];provenance=[];memo={}
    for name,d,spec in loaded:
        asset=spec['name']
        if asset not in memo:
            daily,record=cached_daily(root,spec,cache_dir)
            memo[asset]=(daily,record)
        daily,record=memo[asset]
        provenance.append(dict(dataset=name,ledger_path=d['path'],ledger_sha256=d['sha256'],market_data=record))
        for h in halves:
            lookup=forecasts(daily,h,warmup)
            features=[]
            for row in d['rows']:
                f=lookup.get(row['session_day'])
                if f is None:
                    f=dict(session_day=row['session_day'],snapshot_ms=None,sigma_24h=None,consecutive_valid_days=0,complete=False)
                if f['snapshot_ms'] is not None and f['snapshot_ms']>row['entry_timestamp_ms']:
                    raise ValueError('Feature snapshot would use data after entry')
                # Enforce intended entry window, rather than attaching a future/same-day snapshot elsewhere.
                entry=pd.Timestamp(row['entry_timestamp_ms'],unit='ms',tz='UTC').tz_convert(TZ)
                if str(entry.date())!=row['session_day'] or not 9<=entry.hour<=12:
                    raise ValueError('This preset adapter expects NY morning entry sessions')
                features.append(f)
                feature_rows.append(dict(dataset=name,half_life_days=h,sample_id=row['sample_id'],**f))
            sized,calibration=walk_forward_sizes(d,features,risk,leverage,v['min_train_days'],v['min_train_trades'])
            if not sized:raise ValueError(f'Insufficient calibration history: {name}, half-life {h}')
            used={s['index'] for s in sized}
            exclusions.extend(dict(dataset=name,half_life_days=h,sample_id=r['sample_id'],session_day=r['session_day'],
                                   reason='initial_training_or_insufficient_past_feature_complete_trades')
                              for i,r in enumerate(d['rows']) if i not in used)
            folds.extend(dict(dataset=name,half_life_days=h,**f) for f in calibration)
            for policy in ('fixed_base','volatility_target','fixed_train_matched'):
                stat,ledger,monthly=simulate(d,sized,policy,initial)
                tag=dict(dataset=name,half_life_days=h,policy=policy)
                summaries.append(dict(**tag,**stat))
                trades.extend(dict(**tag,**r) for r in ledger)
                months.extend(dict(**tag,**r) for r in monthly)
            print(f'{name} / half-life {h} complete',flush=True)
    write_csv(out/'summary.csv',summaries)
    write_csv(out/'sized_trades.csv',trades)
    write_csv(out/'monthly.csv',months)
    write_csv(out/'calibration_folds.csv',folds)
    write_csv(out/'feature_snapshots.csv',feature_rows)
    if exclusions:write_csv(out/'training_exclusions.csv',exclusions)
    rec=receipt(cfg,provenance,'volatility_sizing',a)
    rec['method']='Entry-only capped inverse volatility; train-median target; monthly expanding calibration; no outcome optimization.'
    rec['limitations']+=' Sizing counterfactual assumes unchanged fills/linear costs. Retrospective walk-forward, not untouched OOS. Features reset after missing whole 15m bars; baseline fallback logged. 24h vol proxy, not guaranteed account volatility. Main half-life20; 10/40 sensitivity, not a search.'
    write_json(out/'manifest.json',rec)
    write_json(out/'complete.json',dict(datasets=len(loaded),summary_rows=len(summaries),trade_rows=len(trades)))
    print(f'Complete: {out}',flush=True)


if __name__=='__main__':main()
