"""Independent chronological, entry/exit and performance checks for v24."""
import argparse
from datetime import date
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_second_touch as model


def check_metrics(trades, r, cfg):
    assert len(trades) == r.trades
    if trades.empty:
        return
    distance = trades.risk_price.to_numpy()/trades.entry.to_numpy()
    risk = np.minimum(cfg['account_risk_fraction'], cfg['max_entry_notional_equity']*distance)
    for label in ('gross', 'net'):
        values = trades[label+'_r'].to_numpy()
        np.testing.assert_allclose([values.mean(), np.median(values), values.sum(), 100*(values > 0).mean(),
            100*np.mean(values*distance), 100*(np.prod(1+risk*values)-1)],
            [getattr(r, label+'_'+k) for k in ('mean_r', 'median_r', 'total_r', 'wr_pct',
                'mean_position_pct', 'account_return_pct')], atol=1e-9)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
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
    assert not any('delta' in c or 'cvd' in c for c in data.columns)
    source = pd.read_parquet(Path(manifest['source_run'])/'sessions.parquet')
    source = source.loc[source.direction.eq('first_edge')]
    for mode, g in data.groupby('pullback_mode'):
        for c in ('valid_session', 'opening_eligible', 'opening_price', 'poc', 'val', 'vah'):
            pd.testing.assert_series_equal(g.set_index(['symbol', 'session_date'])[c].sort_index(),
                source.set_index(['symbol', 'session_date'])[c].sort_index(), check_names=False)
    active = data.loc[data.status.isin(model.base.ACTIVE)]
    assert active.opening_eligible.all()
    assert (active.first_touch_agg_trade_id < active.pullback_agg_trade_id).all()
    assert (active.pullback_agg_trade_id < active.entry_agg_trade_id).all()
    assert active.touch_edge.eq(np.where(active.side.eq(1), 'val', 'vah')).all()
    np.testing.assert_allclose(active.risk_price, abs(active.entry-active.poc))
    np.testing.assert_allclose(active.sl, 2*active.entry-active.poc)
    np.testing.assert_allclose(active.gross_r, active.side*(active.exit_price-active.entry)/active.risk_price)
    np.testing.assert_allclose(active.net_r, active.gross_r-.0001*cfg['fee_bps_per_side']*(active.entry+active.exit_price)/active.risk_price)
    summary = pd.read_csv(out/'summary.csv')
    folds = pd.read_csv(out/'fold_metrics.csv')
    checked = 0
    for (symbol, mode), rows in data.groupby(['symbol', 'pullback_mode']):
        valid = rows.loc[rows.valid_session]
        owned_tests = []
        for r in folds.loc[folds.symbol.eq(symbol) & folds.pullback_mode.eq(mode)].itertuples():
            start = r.train_start if r.phase == 'train' else r.test_start
            end = r.test_start if r.phase == 'train' else r.test_end_exclusive
            part = valid.loc[valid.session_date.ge(start) & valid.session_date.lt(end)]
            assert len(part) == r.valid_sessions
            trades = part.loc[part.status.isin(model.base.ACTIVE)].sort_values('session_date')
            if r.phase == 'train' and len(trades):
                cut = pd.Timestamp(r.test_start).tz_localize(model.base.NY).tz_convert('UTC')
                trades = trades.loc[pd.to_datetime(trades.exit_timestamp_ny, utc=True, format='ISO8601').lt(cut)]
            if r.phase == 'test':
                owned_tests.append(part)
            check_metrics(trades, r, cfg)
            checked += 1
        tested = pd.concat(owned_tests, ignore_index=True)
        assert not tested.session_date.duplicated().any()
        for r in summary.loc[summary.symbol.eq(symbol) & summary.pullback_mode.eq(mode)].itertuples():
            part = valid if r.sample == 'all_available' else tested
            assert len(part) == r.valid_sessions
            check_metrics(part.loc[part.status.isin(model.base.ACTIVE)].sort_values('session_date'), r, cfg)
            checked += 1
    probes = []
    for symbol, rows in active.groupby('symbol'):
        reader = model.WindowReader(args.root/cfg['assets'][symbol])
        try:
            keys = ['pullback_mode', 'touch_edge', 'status']
            probes_to_read = pd.concat([rows.groupby(keys).head(1), rows.groupby(keys).tail(1)]).drop_duplicates(['pullback_mode', 'session_date']).sort_values('session_date')
            for r in probes_to_read.itertuples():
                _, opening, end = model.base.bounds(date.fromisoformat(r.session_date))
                ts, prices, _, ids = reader.window(opening, end)
                fi = int(np.searchsorted(ids, int(r.first_touch_agg_trade_id)))
                level = r.val if r.side == 1 else r.vah
                first = []
                for boundary in (r.val, r.vah):
                    hits = np.flatnonzero((prices[1:] == boundary) |
                        ((prices[:-1] < boundary) & (prices[1:] > boundary)) |
                        ((prices[:-1] > boundary) & (prices[1:] < boundary)))+1
                    if len(hits):
                        first.append(int(hits[0]))
                assert fi == min(first)
                midpoint = (level+r.poc)/2
                if r.pullback_mode == 'wick_half':
                    hits = np.flatnonzero(r.side*(prices[fi+1:]-level) >= .5*abs(r.poc-level))
                    pi = fi+1+int(hits[0])
                    start, available = pi+1, int(ts[pi])
                else:
                    pi, start, available = None, None, None
                    finish = opening+((int(ts[fi])-opening)//900000+1)*900000
                    while finish <= ts[-1]:
                        last = int(np.searchsorted(ts, finish))-1
                        if last > fi and r.side*(prices[last]-level) >= .5*abs(r.poc-level):
                            pi, start, available = last, last+1, finish
                            break
                        finish += 900000
                    assert pi is not None
                assert int(ids[pi]) == int(r.pullback_agg_trade_id)
                assert model.base.iso(available) == r.pullback_available_ny
                hits = np.flatnonzero((prices[start:] == level) |
                    ((prices[start-1:-1] < level) & (prices[start:] > level)) |
                    ((prices[start-1:-1] > level) & (prices[start:] < level)))
                assert len(hits)
                ei = start+int(hits[0])
                assert int(ids[ei]) == int(r.entry_agg_trade_id)
                targets = np.flatnonzero(r.side*(prices[ei+1:]-r.poc) >= 0)+ei+1
                stops = np.flatnonzero(r.side*(prices[ei+1:]-r.sl) <= 0)+ei+1
                ti = int(targets[0]) if len(targets) else len(prices)
                si = int(stops[0]) if len(stops) else len(prices)
                status = 'tp_first' if ti < si else 'sl_first' if si < ti else 'time_exit'
                exit_i = min(ti, si, len(prices)-1)
                assert r.status == status and int(ids[exit_i]) == int(r.exit_agg_trade_id)
                probes.append(dict(symbol=symbol, pullback_mode=r.pullback_mode, session_date=r.session_date, status=status))
        finally:
            reader.close()
    pd.DataFrame(probes).to_csv(out/'raw_spotchecks.csv', index=False)
    result = dict(status='passed', active_trade_geometry_checked=len(active), performance_rows_checked=checked,
        raw_windows_checked=len(probes), no_delta_or_cvd_columns=True,
        validator_sha256=model.base.sha(__file__))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
