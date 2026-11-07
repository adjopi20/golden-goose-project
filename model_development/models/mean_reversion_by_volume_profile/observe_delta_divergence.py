"""v23: inside-VA NY open, two entry directions, CVD vs 15m delta divergence."""
import argparse
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

import observe_open_poc_touch as touch_model
from observe_push_orderflow import FlowReader

base = touch_model.base
DIRECTIONS = ('first_edge', 'open_poc_bias')
POLICIES = ('base_all', 'cvd_matched', 'cvd_divergence', 'cvd_no_divergence',
            'delta15_matched', 'delta15_divergence', 'delta15_no_divergence')
WIDTH = 15*60000


def features(arrays, entry_index, opening, edge):
    ts, prices, qty, ids, maker = arrays
    i = entry_index
    out = dict(cvd_observable=i > 0, cvd_signal=False,
        delta15_observable=False, delta15_signal=False,
        delta15_status='less_than_three_completed_post_open_intervals')
    if i:
        buy = float(qty[:i][~maker[:i]].sum())
        sell = float(qty[:i][maker[:i]].sum())
        cvd = buy-sell
        outward = 1 if edge == 'vah' else -1
        out.update(cvd=float(cvd), cvd_buy_volume=buy, cvd_sell_volume=sell,
            cvd_signal=bool(outward*cvd < 0),
            cvd_last_observed_id=int(ids[i-1]), cvd_last_observed_ny=base.iso(ts[i-1]))
    cutoff = int(ts[i])
    completed = (cutoff-opening)//WIDTH
    if completed < 3:
        return out
    first = opening+(completed-3)*WIDTH
    values, closes = [], []
    for k in range(3):
        left, right = first+k*WIDTH, first+(k+1)*WIDTH
        a, b = int(np.searchsorted(ts, left)), int(np.searchsorted(ts, right))
        assert b <= i  # All feature prints precede the entry print in aggregate-ID order.
        if a == b:
            return dict(out, delta15_status='missing_interval_prints')
        values.append(float(qty[a:b][~maker[a:b]].sum()-qty[a:b][maker[a:b]].sum()))
        closes.append(float(prices[b-1]))
    outward = 1 if edge == 'vah' else -1
    pressure = outward*np.array(values)
    # Declining pressure may remain positive or cross zero; no extra sign threshold.
    flag = pressure[0] > pressure[1] > pressure[2] and outward*(closes[2]-closes[0]) > 0
    out.update(delta15_observable=True, delta15_status='valid', delta15_signal=bool(flag),
        delta15_start_ny=base.iso(first), delta15_end_ny=base.iso(first+3*WIDTH),
        delta15_price_progress=float(outward*(closes[2]-closes[0])),
        delta15_last_pressure_positive=bool(pressure[2] > 0))
    for k in range(3):
        out['delta15_'+str(k)] = values[k]
        out['delta15_close_'+str(k)] = closes[k]
    return out


def candidate(prices, source, direction):
    if direction == 'open_poc_bias':
        return touch_model.candidate(prices, source['poc'], source['val'], source['vah'])
    hit = base.first_touch(prices[1:], float(prices[0]), source['val'], source['vah'])
    if hit is None:
        return None
    return hit['index']+1, hit['side'], hit['edge'], hit['boundary']


def daily(source, arrays, cfg):
    common = {k:source[k] for k in touch_model.COMMON if k in source}
    common['opening_eligible'] = bool(source['valid_session'] and source['val'] < source['opening_price'] < source['vah'])
    if not source['valid_session']:
        return [dict(common, direction=d, status='source_'+source['status']) for d in DIRECTIONS]
    if not common['opening_eligible']:
        return [dict(common, direction=d, status='open_not_inside_va') for d in DIRECTIONS]
    ts, prices, qty, ids, maker = arrays
    _, opening, end = base.bounds(date.fromisoformat(source['session_date']))
    assert base.quality(*arrays[:4], opening, end, cfg) == 'valid'
    assert prices[0] == source['opening_price'] and maker.dtype == np.bool_
    rows = []
    for direction in DIRECTIONS:
        row = dict(common, direction=direction)
        found = candidate(prices, source, direction)
        if found is None:
            rows.append(dict(row, status='open_at_poc' if direction == 'open_poc_bias' and prices[0] == source['poc'] else 'no_selected_edge_touch'))
            continue
        i, side, edge, level = found
        row.update(touch_edge=edge, touch_boundary=float(level),
            touch_timestamp_ny=base.iso(ts[i]), touch_agg_trade_id=int(ids[i]),
            entry_timestamp_ny=base.iso(ts[i]), entry_agg_trade_id=int(ids[i]))
        result = base.outcome(prices[i:], source['poc'], -side, 'endpoint')
        j = result.pop('exit_index', None)
        row.update(result)
        if j is None:
            rows.append(row)
            continue
        entry, risk = row['entry'], row['risk_price']
        exit_price = float(prices[i+j])
        gross = side*(exit_price-entry)/risk
        cost = .0001*(entry+exit_price)/risk
        row.update(barrier_gross_r=float(row['gross_r']), gross_r=float(gross), exit_price=exit_price,
            cost_r_per_bps=float(cost), net_r=float(gross-cfg['fee_bps_per_side']*cost),
            exit_timestamp_ny=base.iso(ts[i+j]), exit_agg_trade_id=int(ids[i+j]),
            target_distance_pct=float(100*risk/entry), hold_minutes=float((ts[i+j]-ts[i])/60000))
        if direction == 'open_poc_bias':
            assert row['entry_agg_trade_id'] == int(source['entry_agg_trade_id'])
            assert row['exit_agg_trade_id'] == int(source['exit_agg_trade_id']) and row['status'] == source['status']
        row.update(features(arrays, i, opening, edge))
        rows.append(row)
    return rows


def select(active, policy):
    if policy == 'base_all':
        return active
    prefix = 'cvd' if policy.startswith('cvd') else 'delta15'
    available = active[prefix+'_observable'].eq(True)
    if policy.endswith('matched'):
        return active.loc[available]
    flag = active[prefix+'_signal'].eq(True)
    return active.loc[available & (flag if policy.endswith('_divergence') and '_no_' not in policy else ~flag)]


def stats(part, population, cfg):
    result = base.stats(part, len(population), cfg)
    result['opening_eligible_sessions'] = int(population.opening_eligible.sum())
    result['gross_wins'] = int(part.gross_r.gt(0).sum())
    result['net_wins'] = int(part.net_r.gt(0).sum())
    return result


def report(data, cfg, out):
    summaries, folds, coverage = [], [], []
    for (symbol, direction), group in data.groupby(['symbol', 'direction'], sort=True):
        valid = group.loc[group.valid_session]
        all_active = valid.loc[valid.status.isin(base.ACTIVE)]
        tested = []
        for fold, (start, boundary, finish) in enumerate(base.fold_schedule(valid.session_date.min(), valid.session_date.max()), 1):
            train = valid.loc[valid.session_date.ge(str(start.date())) & valid.session_date.lt(str(boundary.date()))]
            test = valid.loc[valid.session_date.ge(str(boundary.date())) & valid.session_date.lt(str(finish.date()))]
            tested.append(test)
            for phase, population in (('train', train), ('test', test)):
                active = population.loc[population.status.isin(base.ACTIVE)]
                purged = 0
                if phase == 'train' and len(active):
                    cut = pd.Timestamp(boundary.date()).tz_localize(base.NY).tz_convert('UTC')
                    keep = pd.to_datetime(active.exit_timestamp_ny, utc=True, format='ISO8601').lt(cut)
                    purged = int((~keep).sum())
                    active = active.loc[keep]
                for policy in POLICIES:
                    selected = select(active, policy)
                    folds.append(dict(symbol=symbol, direction=direction, policy=policy, fold=fold,
                        phase=phase, train_start=str(start.date()), test_start=str(boundary.date()),
                        test_end_exclusive=str(finish.date()), purged_train_outcomes=purged,
                        limited_sample=len(selected) < 20, **stats(selected, population, cfg)))
        test_population = pd.concat(tested, ignore_index=True) if tested else valid.iloc[:0]
        assert not test_population.session_date.duplicated().any()
        for sample, population in (('all_available', valid), ('walk_forward_test', test_population)):
            active = population.loc[population.status.isin(base.ACTIVE)]
            for policy in POLICIES:
                selected = select(active, policy)
                summaries.append(dict(symbol=symbol, direction=direction, policy=policy, sample=sample,
                    **stats(selected, population, cfg)))
            coverage.append(dict(symbol=symbol, direction=direction, sample=sample,
                valid_sessions=len(population), opening_eligible_sessions=int(population.opening_eligible.sum()),
                base_trades=len(active), cvd_observable=int(active.cvd_observable.eq(True).sum()),
                delta15_observable=int(active.delta15_observable.eq(True).sum())))
    summary = pd.DataFrame(summaries)
    summary.to_csv(out/'summary.csv', index=False)
    pd.DataFrame(folds).to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(coverage).to_csv(out/'feature_coverage.csv', index=False)
    data.to_parquet(out/'sessions.parquet', index=False)
    data.groupby(['symbol', 'direction', 'status']).size().rename('sessions').reset_index().to_csv(out/'quality.csv', index=False)
    lines = ['# Delta divergence at frozen VA touch — v23', '',
        'NY open strictly inside previous 09:30–09:30 NY frozen value area. '
        'Two entry rules: first_edge takes first VAL long / VAH short; open_poc_bias selects VAL '
        'if NY open below POC or VAH if above POC. Exactly at POC has no open_poc_bias entry. '
        'One candidate per direction/period; failing a signal never searches for a later touch. '
        'TP POC, symmetric SL 1:1 based on observed entry print; time exit before next NY open. '
        'v22 profile-extreme stop discarded. Four exact assets; no candle-body entry condition.', '',
        'CVD: aggressive buy base quantity minus aggressive sell base quantity, '
        'accumulated from NY open through the print BEFORE entry. VAH divergence: CVD<0; VAL: CVD>0. '
        'This is opposite net flow from the opening anchor, not a swing-high/lower-high detector. '
        'Delta15: last three COMPLETED 15-minute intervals anchored at NY 09:30, all after NY open. '
        'For VAH require delta0>delta1>delta2 and last interval close>first interval close; '
        'VAL reverses both inequalities. Delta may remain positive or cross zero. '
        'No requirement for each individual price step to be monotonic. '
        'Partial interval and entry print excluded; at least 45 minutes of post-open context needed. '
        'Insufficient history is unavailable, not no-divergence. No pre-NY lookback or future pivot confirmation.', '',
        'Each signal has feature-matched baseline and no-divergence complement. '
        'Compare delta15 to delta15_matched because early touches are unavailable. '
        'Nothing fitted; same 8-month train / 2-month test / step 2 calendar. '
        'Previously studied history, not new holdout. Fee 4bps each side; '
        '0.5% equity risk and 5x entry-notional cap as before. Trade-print execution proxy '
        'omits spread, funding, latency and book impact. Profiles reused; none rebuilt.', '',
        '| Asset | Entry rule | Policy | Test entries | TP | SL | Time exit | Gross WR | Gross mean R | Net mean R |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in summary.loc[summary['sample'].eq('walk_forward_test')].to_dict('records'):
        def f(key, suffix=''):
            return f"{r[key]:.3f}{suffix}" if pd.notna(r.get(key)) else '—'
        lines.append(f"| {r['symbol']} | {r['direction']} | {r['policy']} | {int(r['trades'])} | "
            +' | '.join(f(k) for k in ('tp_first', 'sl_first', 'time_exit'))+' | '
            +' | '.join((f('gross_wr_pct', '%'), f('gross_mean_r'), f('net_mean_r')))+' |')
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--source-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    source, out = args.source_run.resolve(), args.output.resolve()
    snapshot = json.loads((source/'config_snapshot.json').read_text())
    audit = json.loads((source/'audit.json').read_text())
    assert audit['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(source/name) == digest, name
    assert base.sha(touch_model.__file__) == snapshot['code_sha256']
    cfg = snapshot['config']
    raw_manifest = json.loads((Path(snapshot['source_run'])/'config_snapshot.json').read_text())
    assert base.sha(Path(snapshot['source_run'])/'audit.json') == snapshot['source_audit_sha256']
    for symbol, r in raw_manifest['sources'].items():
        stat = (args.root/cfg['assets'][symbol]).stat()
        assert stat.st_size == r['bytes'] and stat.st_mtime_ns == r['mtime_ns']
    original = pd.read_csv(source/'sessions.csv', float_precision='round_trip')
    out.mkdir(parents=True, exist_ok=False)
    all_rows = []
    for symbol, group in original.groupby('symbol', sort=True):
        reader = FlowReader(args.root/cfg['assets'][symbol])
        try:
            with (out/(symbol+'_daily.jsonl')).open('w', encoding='utf-8') as log:
                for n, source_row in enumerate(group.sort_values('session_date').to_dict('records'), 1):
                    clean = {k:v for k,v in source_row.items() if pd.notna(v)}
                    arrays = None
                    if clean['valid_session'] and clean['val'] < clean['opening_price'] < clean['vah']:
                        _, start, end = base.bounds(date.fromisoformat(clean['session_date']))
                        arrays = reader.window(start, end)
                    rows = daily(clean, arrays, cfg)
                    all_rows.extend(rows)
                    for row in rows:
                        log.write(json.dumps(row, allow_nan=False)+'\n')
                    if n % 200 == 0:
                        log.flush()
                        print(symbol, n, clean['session_date'], flush=True)
            print(symbol, 'finished; row groups', reader.groups_read, flush=True)
        finally:
            reader.close()
    data = pd.DataFrame(all_rows)
    assert not data.duplicated(['symbol', 'direction', 'session_date']).any()
    summary = report(data, cfg, out)
    modules = (touch_model, base, __import__('observe_push_orderflow'), __import__('observe_half_pullback'),
        __import__('observe_second_push'), __import__('backtest_poc_rr'), __import__('observe'), __import__('observe_four_profiles'))
    manifest = dict(rule_version='delta_divergence_v23', config=cfg,
        source_run=str(source), source_audit_sha256=base.sha(source/'audit.json'),
        code_sha256=base.sha(__file__), profiles_built=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in modules})
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', sessions=len(data),
        profiles_built=0, outputs=outputs), indent=2), encoding='utf-8')
    print(summary.loc[summary['sample'].eq('walk_forward_test'),
        ['symbol', 'direction', 'policy', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r']].to_string(index=False), flush=True)


if __name__ == '__main__':
    main()
