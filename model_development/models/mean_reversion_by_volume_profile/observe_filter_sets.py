"""v27: exact AND/OR of fixed >=1% distance and frozen v25 location selections."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_simple_base_filters as old
from validate_simple_base_filters import absolute_bootstrap

base = old.base
POLICIES = ('base_all', 'distance', 'location', 'intersection', 'union')


def masks(distance, location):
    assert distance.notna().all() and location.notna().all()
    assert distance.dtype == bool and location.dtype == bool
    return distance & location, distance | location


def verified_source(path):
    audit = json.loads((path/'audit.json').read_text())
    assert audit['status'] == 'passed'
    assert json.loads((path/'VALIDATION.json').read_text())['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(path/name) == digest, name
    return json.loads((path/'config_snapshot.json').read_text())


def run(location_run, distance_run, out):
    location_snapshot = verified_source(location_run)
    distance_snapshot = verified_source(distance_run)
    assert location_snapshot['source_audit_sha256'] == distance_snapshot['source_audit_sha256']
    assert location_snapshot['config'] == distance_snapshot['config']
    cfg = location_snapshot['config']
    a = pd.read_parquet(location_run/'test_predictions.parquet')
    b = pd.read_parquet(distance_run/'test_predictions.parquet')
    keys = ['symbol', 'session_date', 'entry_agg_trade_id']
    assert len(a) == len(b) and not a.duplicated(keys).any() and not b.duplicated(keys).any()
    columns = ['fold', 'entry', 'side', 'poc', 'sl', 'risk_price', 'exit_agg_trade_id',
        'exit_price', 'gross_r', 'net_r', 'status', 'entry_timestamp_ny', 'exit_timestamp_ny']
    a_index = a.set_index(keys).sort_index()
    b_index = b.set_index(keys).sort_index()
    pd.testing.assert_frame_equal(a_index[columns], b_index[columns])
    data = b.copy()
    locations = a_index.location_selected.reindex(pd.MultiIndex.from_frame(data[keys])).to_numpy(dtype=bool)
    data['location_selected'] = locations
    data['distance_selected'] = data.min10_selected
    assert data.distance_selected.eq(data.distance_pct_known >= 1.).all()
    data['intersection_selected'], data['union_selected'] = masks(data.distance_selected, data.location_selected)
    data['base_all_selected'] = True
    source_folds = pd.read_csv(distance_run/'fold_metrics.csv')
    schedule = source_folds.loc[source_folds.policy.eq('base_all') & source_folds.phase.eq('test')]
    summaries, folds, years, identities = [], [], [], []
    for symbol, group in data.groupby('symbol', sort=True):
        valid_n = int(schedule.loc[schedule.symbol.eq(symbol), 'valid_sessions'].sum())
        population = pd.DataFrame(index=range(valid_n))
        sets = {p:set(group.loc[group[p+'_selected'], 'entry_agg_trade_id'].astype('int64')) for p in POLICIES}
        assert sets['intersection'] == sets['distance'] & sets['location']
        assert sets['union'] == sets['distance'] | sets['location']
        assert len(sets['union']) == len(sets['distance'])+len(sets['location'])-len(sets['intersection'])
        identities.append(dict(symbol=symbol, distance_entries=len(sets['distance']), location_entries=len(sets['location']),
            intersection_entries=len(sets['intersection']), union_entries=len(sets['union'])))
        for policy in POLICIES:
            selected = group.loc[group[policy+'_selected']].sort_values('session_date')
            summaries.append(dict(symbol=symbol, sample='walk_forward_test', policy=policy,
                retained_pct_base=100*len(selected)/len(group), **old.metrics(selected, population, cfg)))
            for year, part in group.groupby(group.session_date.str[:4]):
                selected_year = part.loc[part[policy+'_selected']].sort_values('session_date')
                years.append(dict(symbol=symbol, policy=policy, year=year, **old.metrics(selected_year, part, cfg)))
    for f in schedule.itertuples():
        group = data.loc[data.symbol.eq(f.symbol) & data.fold.eq(f.fold)]
        population = pd.DataFrame(index=range(int(f.valid_sessions)))
        for policy in POLICIES:
            selected = group.loc[group[policy+'_selected']].sort_values('session_date')
            folds.append(dict(symbol=f.symbol, policy=policy, phase='test', fold=int(f.fold),
                train_start=f.train_start, test_start=f.test_start, test_end_exclusive=f.test_end_exclusive,
                **old.metrics(selected, population, cfg)))
    summary = pd.DataFrame(summaries)
    folds = pd.DataFrame(folds)
    out.mkdir(parents=True, exist_ok=False)
    summary.to_csv(out/'summary.csv', index=False)
    folds.to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(years).to_csv(out/'yearly_metrics.csv', index=False)
    pd.DataFrame(identities).to_csv(out/'set_identities.csv', index=False)
    data.to_parquet(out/'test_predictions.parquet', index=False)
    confidence = absolute_bootstrap(data, folds)
    confidence.to_csv(out/'absolute_confidence.csv', index=False)
    stability = []
    for (symbol, policy), g in folds.groupby(['symbol', 'policy']):
        stability.append(dict(symbol=symbol, policy=policy, test_folds=len(g),
            nonempty_test_folds=int(g.trades.gt(0).sum()), positive_net_test_folds=int(g.net_mean_r.gt(0).sum())))
    pd.DataFrame(stability).to_csv(out/'stability.csv', index=False)
    snapshot = dict(rule_version='filter_sets_v27', config=cfg, assets=old.ASSETS,
        location_source=str(location_run), distance_source=str(distance_run),
        source_audits={str(path/'audit.json'):base.sha(path/'audit.json') for path in (location_run, distance_run)},
        source_parent_audit_sha256=location_snapshot['source_audit_sha256'], code_sha256=base.sha(__file__),
        distance_threshold_pct=1., location_rule='Exact frozen train-selected v25 membership; no new fit',
        union_deduplication_key=keys, profiles_built=0, raw_rows_read=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (old, base, __import__('validate_simple_base_filters'), __import__('observe_va_orderflow'),
             __import__('observe'), __import__('observe_four_profiles'), __import__('backtest_poc_rr'))})
    (out/'config_snapshot.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', predictions=len(data), outputs=outputs), indent=2), encoding='utf-8')
    print(summary[['symbol', 'policy', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r']].to_string(index=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--location-run', type=Path, required=True)
    p.add_argument('--distance-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.location_run.resolve(), args.distance_run.resolve(), args.output.resolve())
