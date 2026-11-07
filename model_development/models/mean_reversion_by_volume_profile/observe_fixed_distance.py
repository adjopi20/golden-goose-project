"""v26: ETH/LTC simple base with user-specified fixed minimum POC distances."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_simple_base_filters as old
from validate_simple_base_filters import absolute_bootstrap

base = old.base
THRESHOLDS = {'base_all':0., 'min03':.3, 'min05':.5, 'min07':.7, 'min10':1., 'min20':2.}


def eligible(distance_pct, threshold):
    return distance_pct.ge(threshold)


def run(source, out):
    audit = json.loads((source/'audit.json').read_text())
    assert audit['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(source/name) == digest
    cfg = json.loads((source/'config_snapshot.json').read_text())['config']
    raw = pd.read_csv(source/'sessions.csv', float_precision='round_trip')
    data = raw.loc[raw.symbol.isin(old.ASSETS)].copy()
    data['distance_pct_known'] = 100*abs(data.poc-data.entry)/data.entry
    for policy, threshold in THRESHOLDS.items():
        data[policy+'_selected'] = eligible(data.distance_pct_known, threshold)
    schedules = pd.read_csv(source/'fold_metrics.csv')
    schedules = schedules.loc[schedules.symbol.isin(old.ASSETS) & schedules.phase.eq('test')]
    out.mkdir(parents=True, exist_ok=False)
    folds, predictions = [], []
    for f in schedules.itertuples():
        valid = data.loc[data.symbol.eq(f.symbol) & data.valid_session]
        train = valid.loc[valid.session_date.ge(f.train_start) & valid.session_date.lt(f.test_start)]
        test = valid.loc[valid.session_date.ge(f.test_start) & valid.session_date.lt(f.test_end_exclusive)]
        for phase, population in (('train', train), ('test', test)):
            trades = population.loc[population.status.isin(base.ACTIVE)].copy()
            purged = 0
            if phase == 'train':
                cutoff = pd.Timestamp(f.test_start).tz_localize(base.NY).tz_convert('UTC')
                known = pd.to_datetime(trades.exit_timestamp_ny, utc=True, format='ISO8601').lt(cutoff)
                purged = int((~known).sum())
                trades = trades.loc[known]
            else:
                predictions.append(trades.assign(fold=int(f.fold)))
            for policy, threshold in THRESHOLDS.items():
                part = trades.loc[trades[policy+'_selected']]
                folds.append(dict(symbol=f.symbol, policy=policy, threshold_pct=threshold,
                    fold=int(f.fold), phase=phase, train_start=f.train_start, test_start=f.test_start,
                    test_end_exclusive=f.test_end_exclusive, purged_train_outcomes=purged,
                    limited_sample=len(part) < 20, **old.metrics(part, population, cfg)))
    folds = pd.DataFrame(folds)
    predicted = pd.concat(predictions, ignore_index=True)
    assert not predicted.duplicated(['symbol', 'session_date']).any()
    summaries, yearly, stability = [], [], []
    for symbol, all_rows in data.groupby('symbol', sort=True):
        valid = all_rows.loc[all_rows.valid_session]
        tests = predicted.loc[predicted.symbol.eq(symbol)]
        valid_test = int(schedules.loc[schedules.symbol.eq(symbol), 'valid_sessions'].sum())
        for sample, active, population in (
                ('all_available', valid.loc[valid.status.isin(base.ACTIVE)], valid),
                ('walk_forward_test', tests, pd.DataFrame(index=range(valid_test)))):
            for policy, threshold in THRESHOLDS.items():
                part = active.loc[active[policy+'_selected']].sort_values('session_date')
                summaries.append(dict(symbol=symbol, sample=sample, policy=policy, threshold_pct=threshold,
                    retained_pct_base=100*len(part)/len(active), **old.metrics(part, population, cfg)))
                for year, group in active.groupby(active.session_date.str[:4]):
                    selected = group.loc[group[policy+'_selected']].sort_values('session_date')
                    yearly.append(dict(symbol=symbol, sample=sample, year=year, policy=policy, threshold_pct=threshold,
                        **old.metrics(selected, group, cfg)))
        for policy, g in folds.loc[folds.symbol.eq(symbol)].groupby('policy'):
            tr = g.loc[g.phase.eq('train')].set_index('fold')
            te = g.loc[g.phase.eq('test')].set_index('fold')
            both = te[['trades', 'net_mean_r']].join(tr[['trades', 'net_mean_r']], rsuffix='_train')
            available = both.trades.gt(0) & both.trades_train.gt(0)
            stability.append(dict(symbol=symbol, policy=policy, threshold_pct=THRESHOLDS[policy],
                test_folds=len(te), nonempty_test_folds=int(te.trades.gt(0).sum()),
                positive_net_test_folds=int(te.net_mean_r.gt(0).sum()), comparable_folds=int(available.sum()),
                train_test_both_positive=int((available & both.net_mean_r.gt(0) & both.net_mean_r_train.gt(0)).sum())))
    summary = pd.DataFrame(summaries)
    summary.to_csv(out/'summary.csv', index=False)
    folds.to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(yearly).to_csv(out/'yearly_metrics.csv', index=False)
    pd.DataFrame(stability).to_csv(out/'stability.csv', index=False)
    data.to_parquet(out/'sessions.parquet', index=False)
    predicted.to_parquet(out/'test_predictions.parquet', index=False)
    confidence = absolute_bootstrap(predicted, folds)
    confidence.to_csv(out/'absolute_confidence.csv', index=False)
    manifest = dict(rule_version='fixed_distance_v26', assets=old.ASSETS, thresholds_pct=THRESHOLDS,
        config=cfg, source_run=str(source), source_audit_sha256=base.sha(source/'audit.json'),
        code_sha256=base.sha(__file__), profiles_built=0, raw_rows_read=0,
        selection='All fixed thresholds evaluated separately; none chosen from test as a validated winner',
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (old, base, __import__('validate_simple_base_filters'), __import__('observe_va_orderflow'),
             __import__('backtest_poc_rr'), __import__('observe'), __import__('observe_four_profiles'))})
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', predictions=len(predicted), outputs=outputs), indent=2), encoding='utf-8')
    print(summary.loc[summary['sample'].eq('walk_forward_test'),
        ['symbol', 'threshold_pct', 'trades', 'retained_pct_base', 'gross_wr_pct', 'net_mean_r', 'net_total_r']].to_string(index=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.source_run.resolve(), args.output.resolve())
