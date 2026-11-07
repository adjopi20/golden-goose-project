"""Independent cutoff, training-only selection, ledger and metric checks for v25."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_simple_base_filters as model


def check(part, r, cfg):
    assert len(part) == int(r.trades)
    if part.empty:
        return
    distance = part.risk_price.to_numpy()/part.entry.to_numpy()
    risk = np.minimum(cfg['account_risk_fraction'], cfg['max_entry_notional_equity']*distance)
    for label in ('gross', 'net'):
        values = part[label+'_r'].to_numpy()
        np.testing.assert_allclose([values.mean(), np.median(values), values.sum(), 100*(values > 0).mean(),
            100*np.mean(values*distance), 100*(np.prod(1+values*risk)-1)],
            [getattr(r, label+'_'+k) for k in ('mean_r', 'median_r', 'total_r', 'wr_pct',
                'mean_position_pct', 'account_return_pct')], atol=1e-9)


def independent_choice(trades, column):
    candidates = []
    for category in sorted(trades[column].unique()):
        part = trades.loc[trades[column].eq(category)]
        mean = sum(part.net_r)/len(part)
        if len(part) >= 25 and mean > 0:
            candidates.append((mean, category))
    return sorted(candidates, key=lambda x:(-x[0], x[1]))[0][1] if candidates else None


def absolute_bootstrap(predictions, folds):
    results = []
    for number, ((symbol, policy), metadata) in enumerate(folds.loc[folds.phase.eq('test')].groupby(['symbol', 'policy'], sort=True)):
        rng = np.random.default_rng(20261025+1000+number)
        sums, counts = np.zeros(4000), np.zeros(4000)
        for f in metadata.itertuples():
            part = predictions.loc[predictions.symbol.eq(symbol) & predictions.fold.eq(f.fold)]
            if policy != 'base_all':
                part = part.loc[part[policy.split('_')[0]+'_selected']]
            dates = pd.date_range(f.test_start, pd.Timestamp(f.test_end_exclusive)-pd.Timedelta(days=1))
            daily = pd.DataFrame(dict(result=part.net_r.to_numpy(), count=np.ones(len(part))),
                index=pd.to_datetime(part.session_date)).reindex(dates, fill_value=0).to_numpy()
            n = len(dates)
            starts = rng.integers(n, size=(4000, int(np.ceil(n/7)), 1))
            selected = ((starts+np.arange(7))%n).reshape(4000, -1)[:, :n]
            draws = daily[selected].sum(axis=1)
            sums += draws[:, 0]
            counts += draws[:, 1]
        means = sums[counts > 0]/counts[counts > 0]
        results.append(dict(symbol=symbol, policy=policy, repeats=4000, calendar_block_days=7,
            net_mean_ci025=float(np.quantile(means, .025)), net_mean_ci975=float(np.quantile(means, .975))))
    return pd.DataFrame(results)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    manifest = json.loads((out/'config_snapshot.json').read_text())
    audit = json.loads((out/'audit.json').read_text())
    for name, digest in audit['outputs'].items():
        assert model.base.sha(out/name) == digest
    assert model.base.sha(model.__file__) == manifest['code_sha256']
    cfg = manifest['config']
    raw = pd.read_csv(Path(manifest['source_run'])/'sessions.csv', float_precision='round_trip')
    raw = raw.loc[raw.symbol.isin(model.ASSETS)]
    data = pd.read_parquet(out/'sessions.parquet')
    for c in raw.columns.difference(['symbol', 'session_date']):
        pd.testing.assert_series_equal(raw.set_index(['symbol', 'session_date'])[c].sort_index(),
            data.set_index(['symbol', 'session_date'])[c].sort_index(), check_names=False)
    active = data.loc[data.status.isin(model.base.ACTIVE)]
    expected_distance = abs(active.entry-active.poc)/active.entry
    np.testing.assert_allclose(active.distance_pct_known, 100*expected_distance)
    np.testing.assert_allclose(active.estimated_fee_r_known, .0008/expected_distance)
    assert active.opening_group.eq(np.where((active.opening_price < active.val) | (active.opening_price > active.vah), 'outside_va', 'inside_va')).all()
    cuts = pd.read_csv(out/'train_selections.csv')
    predictions = pd.read_parquet(out/'test_predictions.parquet')
    folds = pd.read_csv(out/'fold_metrics.csv')
    groups = pd.read_csv(out/'group_fold_metrics.csv')
    checked, classified = 0, 0
    for f in cuts.itertuples():
        valid = data.loc[data.symbol.eq(f.symbol) & data.valid_session]
        train = valid.loc[valid.session_date.ge(f.train_start) & valid.session_date.lt(f.test_start) & valid.status.isin(model.base.ACTIVE)].copy()
        cut = pd.Timestamp(f.test_start).tz_localize(model.base.NY).tz_convert('UTC')
        available = pd.to_datetime(train.exit_timestamp_ny, utc=True, format='ISO8601').lt(cut)
        assert int((~available).sum()) == f.purged_train_outcomes
        train = train.loc[available]
        low, high = np.quantile(train.distance_pct_known.to_numpy(), [1/3, 2/3])
        np.testing.assert_allclose([low, high], [f.distance_low_pct, f.distance_high_pct])
        train['distance_group'] = np.select([train.distance_pct_known <= low, train.distance_pct_known <= high], ['near', 'middle'], default='far')
        distance_choice = independent_choice(train, 'distance_group')
        location_choice = independent_choice(train, 'opening_group')
        assert distance_choice == (None if pd.isna(f.distance_selected) else f.distance_selected)
        assert location_choice == (None if pd.isna(f.location_selected) else f.location_selected)
        test = predictions.loc[predictions.symbol.eq(f.symbol) & predictions.fold.eq(f.fold)].copy()
        np.testing.assert_array_equal(test.distance_group,
            np.select([test.distance_pct_known <= low, test.distance_pct_known <= high], ['near', 'middle'], default='far'))
        assert test.distance_selected.eq(test.distance_group.eq(distance_choice) if distance_choice else False).all()
        assert test.location_selected.eq(test.opening_group.eq(location_choice) if location_choice else False).all()
        classified += len(test)
        for phase, part in (('train', train), ('test', test)):
            for r in folds.loc[folds.symbol.eq(f.symbol) & folds.fold.eq(f.fold) & folds.phase.eq(phase)].itertuples():
                selected = part
                if r.policy == 'distance_train_selected':
                    selected = part.loc[part.distance_group.eq(distance_choice)] if distance_choice else part.iloc[:0]
                elif r.policy == 'location_train_selected':
                    selected = part.loc[part.opening_group.eq(location_choice)] if location_choice else part.iloc[:0]
                check(selected.sort_values('session_date'), r, cfg)
                checked += 1
            for r in groups.loc[groups.symbol.eq(f.symbol) & groups.fold.eq(f.fold) & groups.phase.eq(phase)].itertuples():
                column = {'distance':'distance_group', 'location':'opening_group', 'location_side':'location_side'}[r.family]
                check(part.loc[part[column].eq(r.category)].sort_values('session_date'), r, cfg)
                checked += 1
    summary = pd.read_csv(out/'summary.csv')
    original = pd.read_csv(Path(manifest['source_run'])/'summary.csv')
    for r in summary.itertuples():
        part = predictions.loc[predictions.symbol.eq(r.symbol)]
        if r.policy != 'base_all':
            part = part.loc[part[r.policy.split('_')[0]+'_selected']]
        check(part.sort_values('session_date'), r, cfg)
        checked += 1
        if r.policy == 'base_all':
            old = original.loc[original.symbol.eq(r.symbol) & original['sample'].eq('walk_forward_test')].iloc[0]
            for c in ('trades', 'gross_wr_pct', 'net_wr_pct', 'net_mean_r', 'net_total_r', 'net_account_return_pct'):
                np.testing.assert_allclose(getattr(r, c), old[c])
    group_summary = pd.read_csv(out/'group_summary.csv')
    for r in group_summary.itertuples():
        column = {'distance':'distance_group', 'location':'opening_group', 'location_side':'location_side'}[r.family]
        part = predictions.loc[predictions.symbol.eq(r.symbol) & predictions[column].eq(r.category)]
        check(part.sort_values('session_date'), r, cfg)
        checked += 1
    assert classified == len(predictions) and not predictions.duplicated(['symbol', 'session_date']).any()
    confidence = absolute_bootstrap(predictions, folds)
    confidence.to_csv(out/'absolute_confidence.csv', index=False)
    result = dict(status='passed', source_trades_unchanged=True, source_baselines_reconciled=2,
        selections_checked=len(cuts), predictions_checked=classified, performance_rows_checked=checked,
        raw_rows_read=0, validator_sha256=model.base.sha(__file__),
        absolute_confidence_sha256=model.base.sha(out/'absolute_confidence.csv'))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    print(confidence.to_string(index=False))


if __name__ == '__main__':
    main()
