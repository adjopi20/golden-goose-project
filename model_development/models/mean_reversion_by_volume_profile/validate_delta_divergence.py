"""Independent v23 metrics, pre-touch features and random-frequency controls."""
import argparse
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

import observe_delta_divergence as model
from observe_va_orderflow import controls


def check_metrics(part, row, cfg):
    assert len(part) == row.trades
    if part.empty:
        return
    assert int(part.gross_r.gt(0).sum()) == row.gross_wins
    assert int(part.net_r.gt(0).sum()) == row.net_wins
    distance = part.risk_price.to_numpy()/part.entry.to_numpy()
    risk = np.minimum(cfg['account_risk_fraction'], cfg['max_entry_notional_equity']*distance)
    for label in ('gross', 'net'):
        r = part[label+'_r'].to_numpy()
        np.testing.assert_allclose([r.mean(), np.median(r), r.sum(), 100*(r > 0).mean(),
            100*np.mean(r*distance), 100*(np.prod(1+r*risk)-1)],
            [getattr(row, label+'_'+k) for k in ('mean_r', 'median_r', 'total_r', 'wr_pct',
                'mean_position_pct', 'account_return_pct')], atol=1e-9)


def independent_part(active, policy):
    if policy == 'base_all':
        return active
    prefix = policy.split('_')[0]
    available = active[prefix+'_observable'].fillna(False).astype(bool)
    if policy.endswith('matched'):
        return active.loc[available]
    flag = active[prefix+'_signal'].fillna(False).astype(bool)
    return active.loc[available & (~flag if '_no_' in policy else flag)]


def validate(out, root):
    audit = json.loads((out/'audit.json').read_text())
    for name, digest in audit['outputs'].items():
        assert model.base.sha(out/name) == digest
    manifest = json.loads((out/'config_snapshot.json').read_text())
    assert model.base.sha(model.__file__) == manifest['code_sha256']
    cfg = manifest['config']
    data = pd.read_parquet(out/'sessions.parquet')
    source = pd.read_csv(Path(manifest['source_run'])/'sessions.csv', float_precision='round_trip')
    assert not data.duplicated(['symbol', 'direction', 'session_date']).any()
    for direction, rows in data.groupby('direction'):
        for c in ('valid_session', 'opening_price', 'poc', 'val', 'vah'):
            pd.testing.assert_series_equal(rows.set_index(['symbol', 'session_date'])[c].sort_index(),
                source.set_index(['symbol', 'session_date'])[c].sort_index(), check_names=False)
    active = data.loc[data.status.isin(model.base.ACTIVE)]
    assert active.opening_eligible.all()
    assert (active.val < active.opening_price).all() and (active.opening_price < active.vah).all()
    assert active.touch_edge.eq(np.where(active.side.eq(1), 'val', 'vah')).all()
    np.testing.assert_allclose(active.risk_price, abs(active.entry-active.poc))
    np.testing.assert_allclose(active.sl, 2*active.entry-active.poc)
    np.testing.assert_allclose(active.gross_r, active.side*(active.exit_price-active.entry)/active.risk_price)
    np.testing.assert_allclose(active.net_r, active.gross_r-.0001*cfg['fee_bps_per_side']*(active.entry+active.exit_price)/active.risk_price)
    bias = active.loc[active.direction.eq('open_poc_bias')].merge(source,
        on=['symbol', 'session_date'], suffixes=('', '_source'), validate='one_to_one')
    assert bias.entry_agg_trade_id.eq(bias.entry_agg_trade_id_source).all()
    assert bias.exit_agg_trade_id.eq(bias.exit_agg_trade_id_source).all()
    folds = pd.read_csv(out/'fold_metrics.csv')
    summary = pd.read_csv(out/'summary.csv')
    checked, predictions = 0, []
    for (symbol, direction), population in data.groupby(['symbol', 'direction']):
        valid = population.loc[population.valid_session]
        f = folds.loc[folds.symbol.eq(symbol) & folds.direction.eq(direction)]
        tests = []
        for meta in f.loc[f.policy.eq('base_all')].itertuples():
            start = meta.train_start if meta.phase == 'train' else meta.test_start
            end = meta.test_start if meta.phase == 'train' else meta.test_end_exclusive
            period = valid.loc[valid.session_date.ge(start) & valid.session_date.lt(end)]
            trades = period.loc[period.status.isin(model.base.ACTIVE)].sort_values('session_date')
            if meta.phase == 'train' and len(trades):
                cut = pd.Timestamp(meta.test_start).tz_localize(model.base.NY).tz_convert('UTC')
                trades = trades.loc[pd.to_datetime(trades.exit_timestamp_ny, utc=True, format='ISO8601').lt(cut)]
            if meta.phase == 'test':
                tests.append(period)
                predictions.append(trades.assign(fold=int(meta.fold)))
            for r in f.loc[f.fold.eq(meta.fold) & f.phase.eq(meta.phase)].itertuples():
                check_metrics(independent_part(trades, r.policy), r, cfg)
                checked += 1
        tested = pd.concat(tests, ignore_index=True)
        assert not tested.session_date.duplicated().any()
        for r in summary.loc[summary.symbol.eq(symbol) & summary.direction.eq(direction)].itertuples():
            period = valid if r.sample == 'all_available' else tested
            assert len(period) == r.valid_sessions
            check_metrics(independent_part(period.loc[period.status.isin(model.base.ACTIVE)].sort_values('session_date'), r.policy), r, cfg)
            checked += 1
    probes = []
    for symbol, rows in active.groupby('symbol'):
        reader = model.FlowReader(root/cfg['assets'][symbol])
        try:
            picks = rows.groupby(['direction', 'touch_edge', 'cvd_signal', 'delta15_observable', 'delta15_signal']).head(1).sort_values('session_date')
            for r in picks.itertuples():
                _, start, end = model.base.bounds(date.fromisoformat(r.session_date))
                ts, prices, qty, ids, maker = reader.window(start, end)
                i = int(np.searchsorted(ids, int(r.entry_agg_trade_id)))
                assert prices[i] == r.entry
                outward = 1 if r.touch_edge == 'vah' else -1
                signed = np.where(maker[:i], -qty[:i], qty[:i])
                cvd = float(signed.sum())
                np.testing.assert_allclose(cvd, r.cvd, rtol=1e-10, atol=1e-6)
                assert bool(outward*cvd < 0) == r.cvd_signal
                complete = (int(ts[i])-start)//900000
                assert r.delta15_observable == (complete >= 3)
                if complete >= 3:
                    beginning = start+(complete-3)*900000
                    values, closes = [], []
                    for k in range(3):
                        indices = np.flatnonzero((ts >= beginning+k*900000) & (ts < beginning+(k+1)*900000))
                        assert len(indices) and indices[-1] < i
                        values.append(float(np.sum(np.where(maker[indices], -qty[indices], qty[indices]))))
                        closes.append(float(prices[indices[-1]]))
                    np.testing.assert_allclose(values, [getattr(r, 'delta15_'+str(k)) for k in range(3)], rtol=1e-10, atol=1e-6)
                    pressure = np.array(values)*outward
                    assert bool(pressure[0] > pressure[1] > pressure[2] and outward*(closes[-1]-closes[0]) > 0) == r.delta15_signal
                # Independently find first crossing of selected edge(s) from the NY open.
                edges = [r.val, r.vah] if r.direction == 'first_edge' else [r.val if r.side == 1 else r.vah]
                candidates = []
                for level in edges:
                    hits = np.flatnonzero((prices[1:] == level) | ((prices[:-1] < level) & (prices[1:] > level)) | ((prices[:-1] > level) & (prices[1:] < level)))+1
                    if len(hits):
                        candidates.append(int(hits[0]))
                assert min(candidates) == i
                tp = np.flatnonzero(r.side*(prices[i+1:]-r.poc) >= 0)+i+1
                sl = np.flatnonzero(r.side*(prices[i+1:]-r.sl) <= 0)+i+1
                ti = int(tp[0]) if len(tp) else len(prices)
                si = int(sl[0]) if len(sl) else len(prices)
                expected = 'tp_first' if ti < si else 'sl_first' if si < ti else 'time_exit'
                exit_i = min(ti, si, len(prices)-1)
                assert expected == r.status and int(ids[exit_i]) == int(r.exit_agg_trade_id)
                probes.append(dict(symbol=symbol, direction=r.direction, session_date=r.session_date,
                    cvd_signal=r.cvd_signal, delta15_observable=r.delta15_observable, delta15_signal=r.delta15_signal))
        finally:
            reader.close()
    pd.DataFrame(probes).to_csv(out/'raw_spotchecks.csv', index=False)
    result = dict(status='passed', baseline_trades_checked=len(active), v21_bias_entries_reconciled=len(bias),
        performance_rows_checked=checked, raw_feature_windows_checked=len(probes))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    return pd.concat(predictions, ignore_index=True), folds


def diagnostics(out, predictions, folds):
    controls_rows, stability = [], []
    for n, ((symbol, direction), group) in enumerate(predictions.groupby(['symbol', 'direction'], sort=True)):
        f = folds.loc[folds.symbol.eq(symbol) & folds.direction.eq(direction) & folds.phase.eq('test')]
        for offset, prefix in enumerate(('cvd', 'delta15')):
            matched = independent_part(group, prefix+'_matched').copy()
            matched['feature_status'] = 'valid'
            matched['selected'] = matched[prefix+'_signal'].eq(True)
            seed = 20261026+2*n+offset
            result = controls(matched, f.loc[f.policy.eq(prefix+'_matched')], repeats=2000, seed=seed)
            row = result.iloc[0].to_dict() if len(result) else dict(status='no_matched_trades')
            row.update(direction=direction, signal=prefix, seed=seed, symbol=symbol,
                matched_trades=len(matched), selected_trades=int(matched.selected.sum()))
            controls_rows.append(row)
    for (symbol, direction, policy), rows in folds.groupby(['symbol', 'direction', 'policy']):
        train = rows.loc[rows.phase.eq('train')].set_index('fold')
        test = rows.loc[rows.phase.eq('test')].set_index('fold')
        both = test[['trades', 'net_mean_r']].join(train[['trades', 'net_mean_r']], rsuffix='_train')
        nonempty = both.trades.gt(0) & both.trades_train.gt(0)
        stability.append(dict(symbol=symbol, direction=direction, policy=policy, total_folds=len(test),
            nonempty_test_folds=int(test.trades.gt(0).sum()), positive_net_test_folds=int(test.net_mean_r.gt(0).sum()),
            comparable_folds=int(nonempty.sum()),
            train_test_both_positive=int((nonempty & both.net_mean_r.gt(0) & both.net_mean_r_train.gt(0)).sum())))
    pd.DataFrame(controls_rows).to_csv(out/'controls.csv', index=False)
    pd.DataFrame(stability).to_csv(out/'stability.csv', index=False)
    names = ('VALIDATION.json', 'raw_spotchecks.csv', 'controls.csv', 'stability.csv')
    audit = dict(status='passed', controls_repeats=2000, calendar_block_days=7,
        multiple_comparison_adjusted=False, validator_sha256=model.base.sha(__file__),
        statistics_helper_sha256=model.base.sha(Path(__file__).with_name('observe_va_orderflow.py')),
        source_core_audit_sha256=model.base.sha(out/'audit.json'),
        outputs={name:model.base.sha(out/name) for name in names})
    (out/'diagnostics_audit.json').write_text(json.dumps(audit, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    predictions, folds = validate(args.run, args.root)
    diagnostics(args.run, predictions, folds)


if __name__ == '__main__':
    main()
