"""v29: causal daily ATR-SMA30% / std30 log-return regimes; five LTC RR=1 model_development.models."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import observe_simple_base_filters as old

base = old.base
FEATURES = ('atr30_pct', 'std30_return_pct')
REGIMES = ('low', 'middle', 'high')
DAY = 86400000


def verified(path):
    audit = json.loads((path/'audit.json').read_text())
    assert audit['status'] == 'passed'
    assert json.loads((path/'VALIDATION.json').read_text())['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(path/name) == digest
    return json.loads((path/'config_snapshot.json').read_text())


def build_states(bars, true_range):
    d = bars.sort_values('start_ms').set_index('start_ms').copy()
    assert not d.index.duplicated().any() and (d.index.to_numpy()%DAY == 0).all()
    d = d.reindex(np.arange(d.index.min(), d.index.max()+DAY, DAY)).rename_axis('start_ms')
    d['valid'] = d.valid.fillna(False).astype(bool)
    d['available_ms'] = d.index+DAY
    ohlc = d[['open', 'high', 'low', 'close']].where(d.valid)
    valid = d.valid & d.valid.shift(1, fill_value=False)
    tr = true_range(ohlc.high, ohlc.low, ohlc.close).where(valid)
    returns = np.log(ohlc.close).diff().where(valid)
    d['true_range'] = tr
    d['log_return'] = returns
    d['atr30'] = tr.rolling(30, min_periods=30).mean()
    d['atr30_pct'] = 100*d.atr30/ohlc.close
    d['std30_return_pct'] = 100*returns.rolling(30, min_periods=30).std(ddof=1)
    return d.reset_index()


def entry_states(trades, states):
    out = trades.copy()
    timestamps = np.array([pd.Timestamp(v).value//1000000 for v in trades.entry_timestamp_ny], dtype=np.int64)
    positions = np.searchsorted(states.available_ms.to_numpy(), timestamps, side='right')-1
    assert (positions >= 0).all()
    latest = states.iloc[positions].reset_index(drop=True)
    assert (latest.available_ms.to_numpy() <= timestamps).all()
    assert ((timestamps-latest.available_ms.to_numpy()) < DAY).all()
    out['macro_available_ms'] = latest.available_ms.to_numpy()
    out['macro_day_start_ms'] = latest.start_ms.to_numpy()
    for feature in FEATURES:
        out[feature] = latest[feature].to_numpy()
    return out


def classify(values, q1, q2):
    labels = pd.Series(np.where(values <= q1, 'low', np.where(values <= q2, 'middle', 'high')), index=values.index)
    return labels.where(values.notna(), 'unavailable')


def confidence(labels, schedule):
    result = []
    for number, ((model, feature, regime), group) in enumerate(labels.groupby(['model', 'feature', 'regime'], sort=True)):
        if regime not in REGIMES:
            continue
        rng = np.random.default_rng(20261025+3000+number)
        sums, counts = np.zeros(4000), np.zeros(4000)
        for f in schedule.itertuples():
            p = group.loc[group.fold.eq(f.fold)]
            days = pd.date_range(f.test_start, pd.Timestamp(f.test_end_exclusive)-pd.Timedelta(days=1))
            daily = pd.DataFrame(dict(net=p.net_r.to_numpy(), count=np.ones(len(p))),
                index=pd.to_datetime(p.session_date)).reindex(days, fill_value=0).to_numpy()
            n = len(days)
            starts = rng.integers(n, size=(4000, int(np.ceil(n/7)), 1))
            indices = ((starts+np.arange(7))%n).reshape(4000, -1)[:, :n]
            draws = daily[indices].sum(axis=1)
            sums += draws[:, 0]
            counts += draws[:, 1]
        means = sums[counts > 0]/counts[counts > 0]
        result.append(dict(model=model, feature=feature, regime=regime, repeats=4000,
            calendar_block_days=7, net_mean_ci025=float(np.quantile(means, .025)),
            net_mean_ci975=float(np.quantile(means, .975))))
    return pd.DataFrame(result)


def run(root, trade_run, macro_run, out):
    trade_snapshot = verified(trade_run)
    macro_snapshot = verified(macro_run)
    sys.path.insert(0, str(root))
    import indicator.trend as trend
    assert base.sha(trend.__file__) == macro_snapshot['indicator_sha256']
    cfg = trade_snapshot['config']
    raw_path = (root/cfg['assets']['LTCUSDC']).resolve()
    known = [r for r in macro_snapshot['sources'] if Path(r['path']).resolve() == raw_path]
    assert len(known) == 1
    stat = raw_path.stat()
    assert stat.st_size == known[0]['bytes'] and stat.st_mtime_ns == known[0]['mtime_ns']
    columns = ['symbol', 'clock', 'start_ms', 'available_ms', 'open', 'high', 'low', 'close',
        'valid', 'observations', 'first_observation_ms', 'last_observation_ms']
    cached = pd.read_parquet(macro_run/'macro_states.parquet', columns=columns)
    bars = cached.loc[cached.symbol.eq('LTCUSDC') & cached.clock.eq('1d')].drop(columns=['symbol', 'clock'])
    states = build_states(bars, trend.true_range)
    trades = pd.read_parquet(trade_run/'trades.parquet')
    trades = trades.loc[trades.rr.eq(1.)].copy().reset_index(drop=True)
    assert trades.symbol.eq('LTCUSDC').all()
    annotated = entry_states(trades, states)
    fold_source = pd.read_csv(trade_run/'fold_metrics.csv')
    schedule = fold_source.loc[fold_source.model.eq('A_first_touch') & fold_source.rr.eq(1.)]
    assert not schedule.fold.duplicated().any()
    cut_rows, labeled = [], []
    for f in schedule.itertuples():
        train_start = int(pd.Timestamp(f.train_start).tz_localize(base.NY).value//1000000)
        test_start = int(pd.Timestamp(f.test_start).tz_localize(base.NY).value//1000000)
        train_states = states.loc[states.available_ms.ge(train_start) & states.available_ms.lt(test_start)]
        for feature in FEATURES:
            known_values = train_states[feature].dropna()
            supported = len(known_values) >= 60
            q1, q2 = known_values.quantile([1/3, 2/3]).to_numpy() if supported else (np.nan, np.nan)
            cut_rows.append(dict(fold=int(f.fold), feature=feature, train_start=f.train_start,
                test_start=f.test_start, test_end_exclusive=f.test_end_exclusive,
                train_daily_observations=len(known_values), supported=supported,
                low_cut=float(q1), high_cut=float(q2)))
            p = annotated.loc[annotated.fold.eq(f.fold)].copy()
            p['feature'] = feature
            p['feature_value'] = p[feature]
            p['low_cut'], p['high_cut'] = q1, q2
            p['regime'] = classify(p.feature_value, q1, q2) if supported else 'unsupported_train'
            labeled.append(p)
    labels = pd.concat(labeled, ignore_index=True)
    assert not labels.duplicated(['model', 'entry_agg_trade_id', 'feature']).any()
    summaries, folds, years, baselines, coverage = [], [], [], [], []
    for model, part in annotated.groupby('model', sort=True):
        baselines.append(dict(model=model, **old.metrics(part.sort_values('session_date'),
            pd.DataFrame(index=range(int(schedule.valid_sessions.sum()))), cfg)))
        for feature in FEATURES:
            group = labels.loc[labels.model.eq(model) & labels.feature.eq(feature)]
            matched = group.loc[group.regime.isin(REGIMES)]
            coverage.append(dict(model=model, feature=feature, baseline_entries=len(part),
                classified_entries=len(matched), unavailable=int(group.regime.eq('unavailable').sum()),
                unsupported_train=int(group.regime.eq('unsupported_train').sum())))
            for regime in REGIMES+('unavailable', 'unsupported_train'):
                p = group.loc[group.regime.eq(regime)].sort_values('session_date')
                if p.empty and regime not in REGIMES:
                    continue
                summaries.append(dict(model=model, feature=feature, regime=regime,
                    retained_pct_baseline=100*len(p)/len(part), limited_sample=len(p) < 20,
                    **old.metrics(p, pd.DataFrame(index=range(int(schedule.valid_sessions.sum()))), cfg)))
                for f in schedule.itertuples():
                    selected = p.loc[p.fold.eq(f.fold)]
                    folds.append(dict(model=model, feature=feature, regime=regime, fold=int(f.fold),
                        test_start=f.test_start, test_end_exclusive=f.test_end_exclusive,
                        **old.metrics(selected, pd.DataFrame(index=range(int(f.valid_sessions))), cfg)))
                for year, selected in p.groupby(p.session_date.str[:4]):
                    annual = old.metrics(selected, selected, cfg)
                    annual.pop('valid_sessions')
                    annual.pop('entry_pct_valid_sessions')
                    years.append(dict(model=model, feature=feature, regime=regime, year=year, **annual))
    out.mkdir(parents=True, exist_ok=False)
    pd.DataFrame(summaries).to_csv(out/'regime_summary.csv', index=False)
    fold_table = pd.DataFrame(folds)
    fold_table.to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(years).to_csv(out/'yearly_metrics.csv', index=False)
    pd.DataFrame(baselines).to_csv(out/'baseline_summary.csv', index=False)
    pd.DataFrame(cut_rows).to_csv(out/'train_cutpoints.csv', index=False)
    pd.DataFrame(coverage).to_csv(out/'coverage.csv', index=False)
    annotated.to_parquet(out/'feature_ledger.parquet', index=False)
    labels.to_parquet(out/'test_labels.parquet', index=False)
    states.to_parquet(out/'daily_macro_states.parquet', index=False)
    confidence(labels, schedule).to_csv(out/'absolute_confidence.csv', index=False)
    stable = []
    for keys, group in fold_table.groupby(['model', 'feature', 'regime']):
        stable.append(dict(zip(('model', 'feature', 'regime'), keys), test_folds=len(group),
            nonempty_test_folds=int(group.trades.gt(0).sum()), positive_net_test_folds=int(group.net_mean_r.gt(0).sum())))
    pd.DataFrame(stable).to_csv(out/'stability.csv', index=False)
    snapshot = dict(rule_version='macro_volatility_v29', assets=['LTCUSDC'], rr=1.,
        source_trade_run=str(trade_run), source_macro_run=str(macro_run),
        source_audits={str(path/'audit.json'):base.sha(path/'audit.json') for path in (trade_run, macro_run)},
        raw_source=known[0], config=cfg, lookback_daily_bars=30, std_ddof=1, min_train_daily_observations=60,
        calibration='Daily feature states available in train NY-midnight window, shared across models; no outcomes used',
        code_sha256=base.sha(__file__), profiles_built=0, raw_rows_read=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (old, base, trend, __import__('observe_va_orderflow'), __import__('observe'),
             __import__('observe_four_profiles'), __import__('backtest_poc_rr'))})
    (out/'config_snapshot.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', entries=len(annotated),
        label_rows=len(labels), outputs=outputs), indent=2), encoding='utf-8')
    print(pd.DataFrame(summaries)[['model', 'feature', 'regime', 'trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r']].to_string(index=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--trade-run', type=Path, required=True)
    p.add_argument('--macro-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.root.resolve(), args.trade_run.resolve(), args.macro_run.resolve(), args.output.resolve())
