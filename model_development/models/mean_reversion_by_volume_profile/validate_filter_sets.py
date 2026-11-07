"""Independent Python-set membership and financial-metric reconciliation for v27."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_filter_sets as model
from validate_simple_base_filters import check


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    manifest = json.loads((out/'config_snapshot.json').read_text())
    audit = json.loads((out/'audit.json').read_text())
    assert model.base.sha(model.__file__) == manifest['code_sha256']
    for name, digest in audit['outputs'].items():
        assert model.base.sha(out/name) == digest
    cfg = manifest['config']
    locations = pd.read_parquet(Path(manifest['location_source'])/'test_predictions.parquet')
    distances = pd.read_parquet(Path(manifest['distance_source'])/'test_predictions.parquet')
    data = pd.read_parquet(out/'test_predictions.parquet')
    assert not data.duplicated(['symbol', 'session_date']).any()
    assert set(data.symbol.unique()) == {'ETHUSDC', 'LTCUSDC'}
    masks_checked = 0
    source_sets = {}
    for symbol, group in data.groupby('symbol'):
        source_distance = distances.loc[distances.symbol.eq(symbol)]
        source_location = locations.loc[locations.symbol.eq(symbol)]
        key = ['session_date', 'entry_agg_trade_id']
        for source in (source_distance, source_location):
            fields = ['entry', 'exit_price', 'entry_timestamp_ny', 'exit_timestamp_ny', 'side', 'poc', 'sl', 'risk_price', 'gross_r', 'net_r', 'fold']
            pd.testing.assert_frame_equal(group.set_index(key)[fields].sort_index(), source.set_index(key)[fields].sort_index())
        d = set(source_distance.loc[source_distance.distance_pct_known.ge(1.), 'entry_agg_trade_id'].astype('int64'))
        l = set(source_location.loc[source_location.location_selected, 'entry_agg_trade_id'].astype('int64'))
        source_sets[symbol] = {'base_all':set(group.entry_agg_trade_id.astype('int64')), 'distance':d, 'location':l,
            'intersection':d.intersection(l), 'union':d.union(l)}
        for policy, expected in source_sets[symbol].items():
            actual = set(group.loc[group[policy+'_selected'], 'entry_agg_trade_id'].astype('int64'))
            assert actual == expected
            masks_checked += len(group)
        assert len(d.union(l)) == len(d)+len(l)-len(d.intersection(l))
    np.testing.assert_allclose(data.gross_r, data.side*(data.exit_price-data.entry)/data.risk_price)
    np.testing.assert_allclose(data.net_r, data.gross_r-.0001*cfg['fee_bps_per_side']*(data.entry+data.exit_price)/data.risk_price)
    checked = 0
    for name in ('summary.csv', 'fold_metrics.csv', 'yearly_metrics.csv'):
        table = pd.read_csv(out/name)
        for r in table.itertuples():
            part = data.loc[data.symbol.eq(r.symbol) & data.entry_agg_trade_id.isin(source_sets[r.symbol][r.policy])]
            if hasattr(r, 'fold'):
                part = part.loc[part.fold.eq(r.fold)]
            if hasattr(r, 'year'):
                part = part.loc[part.session_date.str[:4].eq(str(r.year))]
            check(part.sort_values('session_date'), r, cfg)
            checked += 1
    summary = pd.read_csv(out/'summary.csv')
    component_rows = 0
    for source_path, new_policy, source_policy in ((manifest['location_source'], 'location', 'location_train_selected'),
            (manifest['distance_source'], 'distance', 'min10')):
        source = pd.read_csv(Path(source_path)/'summary.csv')
        for symbol in ('ETHUSDC', 'LTCUSDC'):
            before = source.loc[source.symbol.eq(symbol) & source['sample'].eq('walk_forward_test') & source.policy.eq(source_policy)].iloc[0]
            after = summary.loc[summary.symbol.eq(symbol) & summary.policy.eq(new_policy)].iloc[0]
            for field in ('trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r'):
                np.testing.assert_allclose(after[field], before[field])
            component_rows += 1
    for symbol, group in summary.groupby('symbol'):
        g = group.set_index('policy')
        np.testing.assert_allclose(g.loc['union', 'net_total_r'],
            g.loc['distance', 'net_total_r']+g.loc['location', 'net_total_r']-g.loc['intersection', 'net_total_r'])
    ci = pd.read_csv(out/'absolute_confidence.csv')
    assert len(ci) == 10 and ci.net_mean_ci025.le(ci.net_mean_ci975).all()
    result = dict(status='passed', test_trades_checked=len(data), membership_checks=masks_checked,
        performance_rows_checked=checked, source_components_reconciled=component_rows,
        union_deduplicated=True, entries_and_outcomes_unchanged=True, new_training_or_threshold_selection=False,
        validator_sha256=model.base.sha(__file__))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
