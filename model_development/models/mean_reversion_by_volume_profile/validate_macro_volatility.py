"""Independent 31-day calculations, causal labels, shared cutoffs and v29 metrics."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_macro_volatility as model
from validate_simple_base_filters import check


def bootstrap30(labels, schedule):
    result = []
    for number, ((parent, feature, regime), group) in enumerate(labels.groupby(['model', 'feature', 'regime'], sort=True)):
        if regime not in model.REGIMES:
            continue
        rng = np.random.default_rng(20261025+4000+number)
        total, count = np.zeros(4000), np.zeros(4000)
        for f in schedule.itertuples():
            p = group.loc[group.fold.eq(f.fold)]
            dates = pd.date_range(f.test_start, pd.Timestamp(f.test_end_exclusive)-pd.Timedelta(days=1))
            daily = pd.DataFrame(dict(net=p.net_r.to_numpy(), count=np.ones(len(p))),
                index=pd.to_datetime(p.session_date)).reindex(dates, fill_value=0).to_numpy()
            n = len(dates)
            starts = rng.integers(n, size=(4000, int(np.ceil(n/30)), 1))
            indices = ((starts+np.arange(30))%n).reshape(4000, -1)[:, :n]
            draws = daily[indices].sum(axis=1)
            total += draws[:, 0]
            count += draws[:, 1]
        means = total[count > 0]/count[count > 0]
        result.append(dict(model=parent, feature=feature, regime=regime, repeats=4000,
            calendar_block_days=30, net_mean_ci025=float(np.quantile(means, .025)),
            net_mean_ci975=float(np.quantile(means, .975))))
    return pd.DataFrame(result)


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
    states = pd.read_parquet(out/'daily_macro_states.parquet')
    ledger = pd.read_parquet(out/'feature_ledger.parquet')
    labels = pd.read_parquet(out/'test_labels.parquet')
    cuts = pd.read_csv(out/'train_cutpoints.csv', float_precision='round_trip')
    source = pd.read_parquet(Path(manifest['source_trade_run'])/'trades.parquet')
    source = source.loc[source.rr.eq(1.)]
    key = ['model', 'session_date', 'entry_agg_trade_id']
    pd.testing.assert_frame_equal(source.set_index(key).sort_index(), ledger[source.columns].set_index(key).sort_index())
    cached = pd.read_parquet(Path(manifest['source_macro_run'])/'macro_states.parquet')
    bars = cached.loc[cached.symbol.eq('LTCUSDC') & cached.clock.eq('1d')].sort_values('start_ms')
    pd.testing.assert_frame_equal(states[['start_ms', 'open', 'high', 'low', 'close', 'valid']].reset_index(drop=True),
        bars[['start_ms', 'open', 'high', 'low', 'close', 'valid']].reset_index(drop=True))
    calculated = 0
    for i in range(len(states)):
        valid = i >= 30 and states.valid.iloc[i-30:i+1].all()
        if not valid:
            assert states.loc[i, list(model.FEATURES)].isna().all()
            continue
        window = states.iloc[i-30:i+1]
        highs, lows, closes = window.high.to_numpy(), window.low.to_numpy(), window.close.to_numpy()
        true_ranges = [max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1])) for j in range(1, 31)]
        returns = np.log(closes[1:]/closes[:-1])
        np.testing.assert_allclose(states.loc[i, 'atr30_pct'], 100*np.mean(true_ranges)/closes[-1], atol=1e-10)
        np.testing.assert_allclose(states.loc[i, 'std30_return_pct'], 100*np.std(returns, ddof=1), atol=1e-10)
        calculated += 1
    timestamps = np.array([pd.Timestamp(x).value//1000000 for x in ledger.entry_timestamp_ny], dtype=np.int64)
    expected_start = (timestamps//model.DAY-1)*model.DAY
    assert np.array_equal(ledger.macro_day_start_ms, expected_start)
    assert np.array_equal(ledger.macro_available_ms, expected_start+model.DAY)
    matched = states.set_index('start_ms').reindex(expected_start)
    for feature in model.FEATURES:
        np.testing.assert_allclose(ledger[feature], matched[feature], equal_nan=True)
    for r in cuts.itertuples():
        start = pd.Timestamp(r.train_start).tz_localize(model.base.NY).value//1000000
        end = pd.Timestamp(r.test_start).tz_localize(model.base.NY).value//1000000
        historical = states.loc[(states.available_ms >= start) & (states.available_ms < end), r.feature].dropna()
        assert len(historical) == r.train_daily_observations
        assert r.supported == (len(historical) >= 60)
        np.testing.assert_allclose([r.low_cut, r.high_cut], np.quantile(historical, [1/3, 2/3]))
        part = labels.loc[labels.fold.eq(r.fold) & labels.feature.eq(r.feature)]
        assert part.low_cut.eq(r.low_cut).all() and part.high_cut.eq(r.high_cut).all()
        value = part.feature_value.to_numpy()
        expected = np.where(np.isnan(value), 'unavailable', np.where(value <= r.low_cut, 'low', np.where(value <= r.high_cut, 'middle', 'high')))
        assert np.array_equal(part.regime, expected)
    assert not labels.duplicated(['model', 'entry_agg_trade_id', 'feature']).any()
    for _, group in labels.groupby(['entry_agg_trade_id', 'feature']):
        assert group.regime.nunique() == 1 and group.feature_value.nunique() == 1
    checked = 0
    for name in ('regime_summary.csv', 'fold_metrics.csv', 'yearly_metrics.csv'):
        for r in pd.read_csv(out/name).itertuples():
            p = labels.loc[labels.model.eq(r.model) & labels.feature.eq(r.feature) & labels.regime.eq(r.regime)]
            if hasattr(r, 'fold'):
                p = p.loc[p.fold.eq(r.fold)]
            if hasattr(r, 'year'):
                p = p.loc[p.session_date.str[:4].eq(str(r.year))]
            check(p.sort_values('session_date'), r, cfg)
            checked += 1
    for r in pd.read_csv(out/'baseline_summary.csv').itertuples():
        p = ledger.loc[ledger.model.eq(r.model)].sort_values('session_date')
        check(p, r, cfg)
        for feature in model.FEATURES:
            selected = labels.loc[labels.model.eq(r.model) & labels.feature.eq(feature)]
            assert len(selected) == r.trades
            np.testing.assert_allclose(selected.net_r.sum(), r.net_total_r)
        checked += 1
    source_summary = pd.read_csv(Path(manifest['source_trade_run'])/'summary.csv')
    for r in pd.read_csv(out/'baseline_summary.csv').itertuples():
        before = source_summary.loc[source_summary.model.eq(r.model) & source_summary.rr.eq(1)].iloc[0]
        for name in ('trades', 'gross_wr_pct', 'gross_mean_r', 'net_mean_r', 'net_total_r'):
            np.testing.assert_allclose(getattr(r, name), before[name])
    schedule = pd.read_csv(Path(manifest['source_trade_run'])/'fold_metrics.csv')
    schedule = schedule.loc[schedule.model.eq('A_first_touch') & schedule.rr.eq(1.)]
    bootstrap30(labels, schedule).to_csv(out/'confidence_block30.csv', index=False)
    result = dict(status='passed', source_outcomes_unchanged=True, daily_windows_recomputed=calculated,
        causal_entry_joins_checked=len(ledger), train_cutpoints_checked=len(cuts), classifications_checked=len(labels),
        performance_rows_checked=checked, baseline_models_reconciled=5, shared_model_cutoffs=True,
        raw_rows_read=0, validator_sha256=model.base.sha(__file__))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
