"""v25: ETH/LTC v21 entries; distance terciles and opening-location filters separately."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import previous_value_area_reversal as base
from observe_va_orderflow import controls

ASSETS = ('ETHUSDC', 'LTCUSDC')
MIN_TRAIN = 25
POLICIES = ('base_all', 'distance_train_selected', 'location_train_selected')


def features(data, cfg):
    result = data.copy()
    active = result.status.isin(base.ACTIVE)
    result['opening_group'] = np.where((result.opening_price < result.val) | (result.opening_price > result.vah),
        'outside_va', 'inside_va')
    result['trade_direction'] = np.where(result.side.eq(1), 'long', 'short')
    result['location_side'] = result.opening_group+'_'+result.trade_direction
    result['distance_pct_known'] = 100*abs(result.poc-result.entry)/result.entry
    result['estimated_fee_r_known'] = 2*.0001*cfg['fee_bps_per_side']*result.entry/abs(result.poc-result.entry)
    assert np.array_equal(result.loc[active, 'side'], np.sign(result.loc[active, 'poc']-result.loc[active, 'opening_price']))
    np.testing.assert_allclose(result.loc[active, 'risk_price'], abs(result.loc[active, 'poc']-result.loc[active, 'entry']))
    return result


def distance_groups(values, low, high):
    return pd.Series(np.where(values <= low, 'near', np.where(values <= high, 'middle', 'far')), index=values.index)


def choose(train, column):
    eligible = []
    for label, group in train.groupby(column, sort=True):
        score = float(group.net_r.mean())
        if len(group) >= MIN_TRAIN and score > 0:
            eligible.append((score, str(label)))
    return sorted(eligible, key=lambda x:(-x[0], x[1]))[0][1] if eligible else None


def metrics(trades, population, cfg):
    result = base.stats(trades, len(population), cfg)
    result.update(gross_wins=int(trades.gross_r.gt(0).sum()), net_wins=int(trades.net_r.gt(0).sum()))
    if trades.empty:
        for label in ('gross', 'net'):
            result.update({label+'_total_r':0., label+'_account_return_pct':0., label+'_capital_base_r':0.})
    return result


def run(source, out):
    audit = json.loads((source/'audit.json').read_text())
    assert audit['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(source/name) == digest, name
    snapshot = json.loads((source/'config_snapshot.json').read_text())
    cfg = snapshot['config']
    raw = pd.read_csv(source/'sessions.csv', float_precision='round_trip')
    data = features(raw.loc[raw.symbol.isin(ASSETS)], cfg)
    schedules = pd.read_csv(source/'fold_metrics.csv')
    schedules = schedules.loc[schedules.symbol.isin(ASSETS) & schedules.phase.eq('test')]
    assert not schedules.duplicated(['symbol', 'fold']).any()
    out.mkdir(parents=True, exist_ok=False)
    fold_rows, groups, cuts, predictions = [], [], [], []
    for f in schedules.itertuples():
        valid = data.loc[data.symbol.eq(f.symbol) & data.valid_session]
        tr_pop = valid.loc[valid.session_date.ge(f.train_start) & valid.session_date.lt(f.test_start)]
        te_pop = valid.loc[valid.session_date.ge(f.test_start) & valid.session_date.lt(f.test_end_exclusive)]
        train = tr_pop.loc[tr_pop.status.isin(base.ACTIVE)].copy()
        test = te_pop.loc[te_pop.status.isin(base.ACTIVE)].copy()
        cutoff = pd.Timestamp(f.test_start).tz_localize(base.NY).tz_convert('UTC')
        available = pd.to_datetime(train.exit_timestamp_ny, utc=True, format='ISO8601').lt(cutoff)
        purged = int((~available).sum())
        train = train.loc[available]
        low, high = train.distance_pct_known.quantile([1/3, 2/3]).to_numpy()
        train['distance_group'] = distance_groups(train.distance_pct_known, low, high)
        test['distance_group'] = distance_groups(test.distance_pct_known, low, high)
        selected_distance = choose(train, 'distance_group')
        selected_location = choose(train, 'opening_group')
        meta = dict(symbol=f.symbol, fold=int(f.fold), train_start=f.train_start,
            test_start=f.test_start, test_end_exclusive=f.test_end_exclusive)
        cuts.append(dict(**meta, train_trades=len(train), purged_train_outcomes=purged,
            distance_low_pct=float(low), distance_high_pct=float(high),
            distance_selected=selected_distance, location_selected=selected_location))
        for phase, active, population in (('train', train, tr_pop), ('test', test, te_pop)):
            active = active.copy()
            active['distance_selected'] = active.distance_group.eq(selected_distance) if selected_distance else False
            active['location_selected'] = active.opening_group.eq(selected_location) if selected_location else False
            if phase == 'test':
                predictions.append(active.assign(fold=int(f.fold), distance_low_pct=float(low), distance_high_pct=float(high),
                    distance_choice=selected_distance, location_choice=selected_location))
            for policy, part in (('base_all', active),
                    ('distance_train_selected', active.loc[active.distance_selected]),
                    ('location_train_selected', active.loc[active.location_selected])):
                fold_rows.append(dict(**meta, phase=phase, policy=policy,
                    purged_train_outcomes=purged, limited_sample=len(part) < 20, **metrics(part, population, cfg)))
            for family, column in (('distance', 'distance_group'), ('location', 'opening_group'), ('location_side', 'location_side')):
                for label, part in active.groupby(column, sort=True):
                    groups.append(dict(**meta, phase=phase, family=family, category=label,
                        **metrics(part, population, cfg)))
    predicted = pd.concat(predictions, ignore_index=True)
    assert not predicted.duplicated(['symbol', 'session_date']).any()
    folds = pd.DataFrame(fold_rows)
    group_folds = pd.DataFrame(groups)
    summary, group_summary, control_rows, stability = [], [], [], []
    for symbol, part in predicted.groupby('symbol', sort=True):
        valid_n = int(folds.loc[folds.symbol.eq(symbol) & folds.phase.eq('test') & folds.policy.eq('base_all'), 'valid_sessions'].sum())
        population = pd.DataFrame(index=range(valid_n))
        for policy in POLICIES:
            selected = part if policy == 'base_all' else part.loc[part[policy.split('_')[0]+'_selected']]
            summary.append(dict(symbol=symbol, sample='walk_forward_test', policy=policy,
                **metrics(selected.sort_values('session_date'), population, cfg)))
        for family, column in (('distance', 'distance_group'), ('location', 'opening_group'), ('location_side', 'location_side')):
            for label, selected in part.groupby(column, sort=True):
                group_summary.append(dict(symbol=symbol, family=family, category=label,
                    **metrics(selected.sort_values('session_date'), population, cfg)))
        for number, family in enumerate(('distance', 'location')):
            trials = part.assign(feature_status='valid', selected=part[family+'_selected'])
            meta = folds.loc[folds.symbol.eq(symbol) & folds.phase.eq('test') & folds.policy.eq('base_all')]
            seed = 20261025+100*ASSETS.index(symbol)+number
            control = controls(trials, meta, repeats=2000, seed=seed).iloc[0].to_dict()
            control_rows.append(dict(control, family=family, seed=seed))
    for (symbol, policy), g in folds.groupby(['symbol', 'policy']):
        tr = g.loc[g.phase.eq('train')].set_index('fold')
        te = g.loc[g.phase.eq('test')].set_index('fold')
        both = te[['trades', 'net_mean_r']].join(tr[['trades', 'net_mean_r']], rsuffix='_train')
        ok = both.trades.gt(0) & both.trades_train.gt(0)
        stability.append(dict(symbol=symbol, policy=policy, test_folds=len(te),
            nonempty_test_folds=int(te.trades.gt(0).sum()), positive_net_test_folds=int(te.net_mean_r.gt(0).sum()),
            comparable_folds=int(ok.sum()), train_test_both_positive=int((ok & both.net_mean_r.gt(0) & both.net_mean_r_train.gt(0)).sum())))
    pd.DataFrame(summary).to_csv(out/'summary.csv', index=False)
    group_folds.to_csv(out/'group_fold_metrics.csv', index=False)
    pd.DataFrame(group_summary).to_csv(out/'group_summary.csv', index=False)
    folds.to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(cuts).to_csv(out/'train_selections.csv', index=False)
    pd.DataFrame(control_rows).to_csv(out/'controls.csv', index=False)
    pd.DataFrame(stability).to_csv(out/'stability.csv', index=False)
    data.to_parquet(out/'sessions.parquet', index=False)
    predicted.to_parquet(out/'test_predictions.parquet', index=False)
    manifest = dict(rule_version='simple_base_filters_v25', source_run=str(source),
        source_audit_sha256=base.sha(source/'audit.json'), config=cfg, assets=ASSETS,
        minimum_train_group_trades=MIN_TRAIN, selection_objective='highest positive net mean R; ties alphabetical',
        distance_quantiles=[1/3, 2/3], code_sha256=base.sha(__file__), profiles_built=0, raw_rows_read=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (base, __import__('observe_va_orderflow'), __import__('backtest_poc_rr'),
             __import__('observe'), __import__('observe_four_profiles'))})
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', predictions=len(predicted),
        train_selections=len(cuts), outputs=outputs), indent=2), encoding='utf-8')
    print(pd.DataFrame(summary)[['symbol', 'policy', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r']].to_string(index=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.source_run.resolve(), args.output.resolve())
