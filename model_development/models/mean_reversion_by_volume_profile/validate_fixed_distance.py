"""Independent fixed-distance masks, source invariance and fold/year metric reconciliation."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_fixed_distance as model
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
    data = pd.read_parquet(out/'sessions.parquet')
    raw = pd.read_csv(Path(manifest['source_run'])/'sessions.csv', float_precision='round_trip')
    raw = raw.loc[raw.symbol.isin(model.old.ASSETS)]
    pd.testing.assert_frame_equal(raw.set_index(['symbol', 'session_date']).sort_index(),
        data[raw.columns].set_index(['symbol', 'session_date']).sort_index())
    active = data.loc[data.status.isin(model.base.ACTIVE)]
    independent_distance = 100*abs(active.poc/active.entry-1)
    np.testing.assert_allclose(independent_distance, active.distance_pct_known, atol=1e-12)
    previous = pd.Series(True, index=active.index)
    for policy, threshold in model.THRESHOLDS.items():
        expected = (100*abs(active.poc-active.entry) >= threshold*active.entry)
        # Division and multiplication may differ at binary floating-point equality only.
        actual = active[policy+'_selected']
        disagreements = expected.ne(actual)
        assert np.isclose(active.loc[disagreements, 'distance_pct_known'], threshold, atol=1e-12, rtol=0).all()
        assert (~actual | previous).all()
        previous = actual
    predicted = pd.read_parquet(out/'test_predictions.parquet')
    folds = pd.read_csv(out/'fold_metrics.csv')
    summary = pd.read_csv(out/'summary.csv')
    yearly = pd.read_csv(out/'yearly_metrics.csv')
    original = pd.read_csv(Path(manifest['source_run'])/'summary.csv')
    assert not predicted.duplicated(['symbol', 'session_date']).any()
    checked = 0
    for r in folds.itertuples():
        valid = data.loc[data.symbol.eq(r.symbol) & data.valid_session]
        start = r.train_start if r.phase == 'train' else r.test_start
        end = r.test_start if r.phase == 'train' else r.test_end_exclusive
        population = valid.loc[valid.session_date.ge(start) & valid.session_date.lt(end)]
        assert len(population) == r.valid_sessions
        part = population.loc[population.status.isin(model.base.ACTIVE)]
        if r.phase == 'train':
            cutoff = pd.Timestamp(r.test_start).tz_localize(model.base.NY).tz_convert('UTC')
            known = pd.to_datetime(part.exit_timestamp_ny, utc=True, format='ISO8601').lt(cutoff)
            assert int((~known).sum()) == r.purged_train_outcomes
            part = part.loc[known]
        selected = part.loc[part.distance_pct_known >= r.threshold_pct].sort_values('session_date')
        check(selected, r, cfg)
        checked += 1
        if r.phase == 'test' and r.policy == 'base_all':
            saved = predicted.loc[predicted.symbol.eq(r.symbol) & predicted.fold.eq(r.fold)]
            assert saved.entry_agg_trade_id.tolist() == part.entry_agg_trade_id.tolist()
    for table in (summary, yearly):
        for r in table.itertuples():
            part = active.loc[active.symbol.eq(r.symbol)] if r.sample == 'all_available' else predicted.loc[predicted.symbol.eq(r.symbol)]
            if hasattr(r, 'year'):
                part = part.loc[part.session_date.str[:4].eq(str(r.year))]
            part = part.loc[part.distance_pct_known >= r.threshold_pct].sort_values('session_date')
            check(part, r, cfg)
            checked += 1
            if table is summary and r.policy == 'base_all':
                old = original.loc[original.symbol.eq(r.symbol) & original['sample'].eq(r.sample)].iloc[0]
                for col in ('trades', 'gross_wr_pct', 'net_wr_pct', 'net_mean_r', 'net_total_r'):
                    np.testing.assert_allclose(getattr(r, col), old[col])
    confidence = pd.read_csv(out/'absolute_confidence.csv')
    assert len(confidence) == 12 and (confidence.net_mean_ci025 <= confidence.net_mean_ci975).all()
    result = dict(status='passed', source_entries_and_outcomes_unchanged=True,
        baseline_rows_reconciled=4, predictions_checked=len(predicted),
        threshold_masks_checked=len(active)*len(model.THRESHOLDS), performance_rows_checked=checked,
        nested_thresholds=True, profiles_built=0, raw_rows_read=0, validator_sha256=model.base.sha(__file__))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
