"""v28: LTC-only fixed-entry RR sweep; POC target, stop = reward distance / RR."""
import argparse
from datetime import date
import json
from pathlib import Path
import numpy as np
import pandas as pd

import observe_simple_base_filters as old
from observe_half_pullback import WindowReader
from observe_profile_end_stop import scan

base = old.base
RATIOS = (1., 1.2, 1.5, 1.8, 2.)
MODELS = ('A_first_touch', 'distance', 'location', 'intersection', 'union')
EVENT_FIELDS = ('entry', 'poc', 'side', 'entry_timestamp_ny', 'exit_agg_trade_id', 'exit_price', 'status', 'gross_r', 'net_r', 'risk_price')


def verified(path):
    audit = json.loads((path/'audit.json').read_text())
    assert audit['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(path/name) == digest
    return json.loads((path/'config_snapshot.json').read_text())


def geometry(entry, poc, side, rr):
    reward = side*(poc-entry)
    if reward <= 0 or not np.isfinite(rr) or rr <= 0:
        raise ValueError('Positive reward distance and RR required')
    risk = reward/rr
    return reward, risk, entry-side*risk


def replay(event, arrays, cfg):
    ts, prices, _, ids = arrays
    i = int(np.searchsorted(ids, int(event['entry_agg_trade_id'])))
    assert i < len(ids) and int(ids[i]) == int(event['entry_agg_trade_id'])
    assert prices[i] == event['entry'] and base.iso(ts[i]) == event['entry_timestamp_ny']
    old_exit = int(np.searchsorted(ids, int(event['exit_agg_trade_id'])))
    rows = []
    for rr in RATIOS:
        entry, poc, side = event['entry'], event['poc'], int(event['side'])
        reward, risk, stop = geometry(entry, poc, side, rr)
        assert stop > 0
        status, j, barrier = scan(prices[i:], side, poc, stop, risk)
        assert i+j <= old_exit
        exit_price = float(prices[i+j])
        gross = side*(exit_price-entry)/risk
        fee = .0001*cfg['fee_bps_per_side']*(entry+exit_price)/risk
        row = dict(session_date=event['session_date'], entry_agg_trade_id=int(ids[i]), rr=rr,
            entry=entry, poc=poc, tp=poc, side=side, sl=float(stop), risk_price=float(risk),
            risk_bps=float(10000*risk/entry), reward_price=float(reward), status=status,
            barrier_gross_r=float(barrier), gross_r=float(gross), net_r=float(gross-fee),
            cost_r_per_bps=float(fee/cfg['fee_bps_per_side']), exit_price=exit_price,
            entry_timestamp_ny=base.iso(ts[i]), exit_timestamp_ny=base.iso(ts[i+j]),
            exit_agg_trade_id=int(ids[i+j]), target_distance_pct=float(100*reward/entry),
            hold_minutes=float((ts[i+j]-ts[i])/60000))
        if rr == 1:
            assert status == event['status'] and int(ids[i+j]) == int(event['exit_agg_trade_id'])
            np.testing.assert_allclose([gross, gross-fee], [event['gross_r'], event['net_r']], atol=1e-10)
        rows.append(row)
    return rows


def confidence_intervals(trades, schedule):
    rows = []
    for number, ((model, rr), group) in enumerate(trades.groupby(['model', 'rr'], sort=True)):
        rng = np.random.default_rng(20261025+2000+number)
        sums, counts = np.zeros(4000), np.zeros(4000)
        for f in schedule.itertuples():
            part = group.loc[group.fold.eq(f.fold)]
            dates = pd.date_range(f.test_start, pd.Timestamp(f.test_end_exclusive)-pd.Timedelta(days=1))
            daily = pd.DataFrame(dict(net=part.net_r.to_numpy(), count=np.ones(len(part))),
                index=pd.to_datetime(part.session_date)).reindex(dates, fill_value=0).to_numpy()
            n = len(dates)
            starts = rng.integers(n, size=(4000, int(np.ceil(n/7)), 1))
            picks = ((starts+np.arange(7))%n).reshape(4000, -1)[:, :n]
            draws = daily[picks].sum(axis=1)
            sums += draws[:, 0]
            counts += draws[:, 1]
        means = sums[counts > 0]/counts[counts > 0]
        rows.append(dict(model=model, rr=rr, repeats=4000, calendar_block_days=7,
            net_mean_ci025=float(np.quantile(means, .025)), net_mean_ci975=float(np.quantile(means, .975))))
    return pd.DataFrame(rows)


def report(trades, schedule, cfg, out):
    summary, folds, years, stability = [], [], [], []
    valid_n = int(schedule.valid_sessions.sum())
    population = pd.DataFrame(index=range(valid_n))
    for (model, rr), part in trades.groupby(['model', 'rr'], sort=True):
        part = part.sort_values('session_date')
        summary.append(dict(symbol='LTCUSDC', model=model, rr=rr, **old.metrics(part, population, cfg)))
        for f in schedule.itertuples():
            selected = part.loc[part.fold.eq(f.fold)]
            folds.append(dict(symbol='LTCUSDC', model=model, rr=rr, fold=int(f.fold), phase='test',
                train_start=f.train_start, test_start=f.test_start, test_end_exclusive=f.test_end_exclusive,
                **old.metrics(selected, pd.DataFrame(index=range(int(f.valid_sessions))), cfg)))
        for year, selected in part.groupby(part.session_date.str[:4]):
            years.append(dict(symbol='LTCUSDC', model=model, rr=rr, year=year,
                **old.metrics(selected, selected, cfg)))
    summary = pd.DataFrame(summary)
    folds = pd.DataFrame(folds)
    for (model, rr), group in folds.groupby(['model', 'rr']):
        stability.append(dict(model=model, rr=rr, test_folds=len(group),
            nonempty_test_folds=int(group.trades.gt(0).sum()), positive_net_test_folds=int(group.net_mean_r.gt(0).sum())))
    summary.to_csv(out/'summary.csv', index=False)
    folds.to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(years).to_csv(out/'yearly_metrics.csv', index=False)
    pd.DataFrame(stability).to_csv(out/'stability.csv', index=False)
    confidence_intervals(trades, schedule).to_csv(out/'absolute_confidence.csv', index=False)
    trades.to_parquet(out/'trades.parquet', index=False)
    return summary


def run(root, checkpoint, a_source, out):
    cp = verified(checkpoint)
    a_snapshot = verified(a_source)
    assert cp['config'] == a_snapshot['config']
    cfg = cp['config']
    schedule = pd.read_csv(checkpoint/'fold_metrics.csv')
    schedule = schedule.loc[schedule.symbol.eq('LTCUSDC') & schedule.policy.eq('base_all')]
    assert schedule.phase.eq('test').all() and not schedule.fold.duplicated().any()
    checkpoint_data = pd.read_parquet(checkpoint/'test_predictions.parquet')
    checkpoint_data = checkpoint_data.loc[checkpoint_data.symbol.eq('LTCUSDC')]
    a_data = pd.read_parquet(a_source/'sessions.parquet')
    a_data = a_data.loc[a_data.symbol.eq('LTCUSDC') & a_data.direction.eq('first_edge') & a_data.status.isin(base.ACTIVE)].copy()
    a_parts = []
    for f in schedule.itertuples():
        part = a_data.loc[a_data.session_date.ge(f.test_start) & a_data.session_date.lt(f.test_end_exclusive)].copy()
        a_parts.append(part.assign(fold=int(f.fold), model='A_first_touch'))
    members = [pd.concat(a_parts, ignore_index=True)]
    for model in MODELS[1:]:
        members.append(checkpoint_data.loc[checkpoint_data[model+'_selected']].assign(model=model))
    parents = pd.concat(members, ignore_index=True)
    assert not parents.duplicated(['model', 'session_date']).any()
    for _, group in parents.groupby(['session_date', 'entry_agg_trade_id']):
        for column in EVENT_FIELDS:
            assert group[column].nunique() == 1, column
    v21_snapshot = json.loads((Path(a_snapshot['source_run'])/'config_snapshot.json').read_text())
    raw_snapshot = json.loads((Path(v21_snapshot['source_run'])/'config_snapshot.json').read_text())
    raw_info = raw_snapshot['sources']['LTCUSDC']
    raw_path = root/cfg['assets']['LTCUSDC']
    stat = raw_path.stat()
    assert stat.st_size == raw_info['bytes'] and stat.st_mtime_ns == raw_info['mtime_ns']
    out.mkdir(parents=True, exist_ok=False)
    reader = WindowReader(raw_path)
    records, unique_events = [], 0
    try:
        with (out/'daily.jsonl').open('w', encoding='utf-8') as log:
            for number, (day, group) in enumerate(parents.groupby('session_date', sort=True), 1):
                _, start, end = base.bounds(date.fromisoformat(day))
                arrays = reader.window(start, end)
                assert base.quality(*arrays, start, end, cfg) == 'valid'
                for _, event_group in group.groupby('entry_agg_trade_id'):
                    event = event_group.iloc[0].to_dict()
                    unique_events += 1
                    outcomes = replay(event, arrays, cfg)
                    for p in event_group.itertuples():
                        for outcome in outcomes:
                            row = dict(outcome, symbol='LTCUSDC', model=p.model, fold=int(p.fold))
                            records.append(row)
                            log.write(json.dumps(row, allow_nan=False)+'\n')
                if number % 100 == 0:
                    log.flush()
                    print(number, day, flush=True)
        groups_read = reader.groups_read
    finally:
        reader.close()
    trades = pd.DataFrame(records)
    assert not trades.duplicated(['model', 'rr', 'session_date']).any()
    parents[[c for c in parents.columns if c not in ('direction',)]].to_parquet(out/'source_membership.parquet', index=False)
    summary = report(trades, schedule, cfg, out)
    for r in summary.loc[summary.rr.eq(1)].itertuples():
        if r.model == 'A_first_touch':
            original = pd.read_csv(a_source/'summary.csv')
            row = original.loc[original.symbol.eq('LTCUSDC') & original.direction.eq('first_edge') & original.policy.eq('base_all') & original['sample'].eq('walk_forward_test')].iloc[0]
        else:
            original = pd.read_csv(checkpoint/'summary.csv')
            row = original.loc[original.symbol.eq('LTCUSDC') & original.policy.eq(r.model)].iloc[0]
        for column in ('trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r'):
            np.testing.assert_allclose(getattr(r, column), row[column], atol=1e-9)
    manifest = dict(rule_version='rr_checkpoint_v28', config=cfg, assets=['LTCUSDC'], ratios=RATIOS,
        checkpoint_run=str(checkpoint), a_source_run=str(a_source), checkpoint_preserved=True,
        membership_rule='A first-edge inside-VA baseline separate; four checkpoint simple-base memberships frozen',
        source_audits={str(path/'audit.json'):base.sha(path/'audit.json') for path in (checkpoint, a_source)},
        raw_source=dict(path=str(raw_path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns),
        code_sha256=base.sha(__file__), profiles_built=0, unique_entry_events=unique_events, row_groups_read=groups_read,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (old, base, __import__('observe_half_pullback'), __import__('observe_profile_end_stop'),
             __import__('observe_second_push'), __import__('observe_open_poc_touch'), __import__('observe_va_orderflow'),
             __import__('backtest_poc_rr'), __import__('observe'), __import__('observe_four_profiles'))})
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (out/'CHECKPOINT.md').write_text('# Preserved checkpoint v27\n\n'+str(checkpoint)+'\n\nSHA256 audit.json: '+base.sha(checkpoint/'audit.json')+
        '\n\nLTC intersection RR=1: 69 trades; 62.31884058% gross WR; +0.303230054 gross R; +0.256290055 net R; +17.684013810 net total R.\n', encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', source_rr1_baselines_reconciled=5, outputs=outputs), indent=2), encoding='utf-8')
    print(summary[['model', 'rr', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r', 'net_mean_position_pct']].to_string(index=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--checkpoint-run', type=Path, required=True)
    p.add_argument('--a-source-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.root.resolve(), args.checkpoint_run.resolve(), args.a_source_run.resolve(), args.output.resolve())
