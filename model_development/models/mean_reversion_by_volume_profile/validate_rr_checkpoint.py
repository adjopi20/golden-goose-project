"""Independent full raw-event replay, frozen membership, RR and metric checks for v28."""
import argparse
from datetime import date
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_rr_checkpoint as model
from validate_simple_base_filters import check


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
    trades = pd.read_parquet(out/'trades.parquet')
    parents = pd.read_parquet(out/'source_membership.parquet')
    checkpoint = Path(manifest['checkpoint_run'])
    original = pd.read_parquet(checkpoint/'test_predictions.parquet')
    original = original.loc[original.symbol.eq('LTCUSDC')]
    schedule = pd.read_csv(checkpoint/'fold_metrics.csv')
    schedule = schedule.loc[schedule.symbol.eq('LTCUSDC') & schedule.policy.eq('base_all')]
    a_source = Path(manifest['a_source_run'])
    first = pd.read_parquet(a_source/'sessions.parquet')
    first = first.loc[first.symbol.eq('LTCUSDC') & first.direction.eq('first_edge') & first.status.isin(model.base.ACTIVE)]
    known_a_dates = set()
    for f in schedule.itertuples():
        known_a_dates.update(first.loc[first.session_date.ge(f.test_start) & first.session_date.lt(f.test_end_exclusive), 'session_date'])
    expected = {'A_first_touch':first.loc[first.session_date.isin(known_a_dates)]}
    expected.update({m:original.loc[original[m+'_selected']] for m in model.MODELS[1:]})
    for parent, source in expected.items():
        p_rows = parents.loc[parents.model.eq(parent)]
        assert set(p_rows.entry_agg_trade_id.astype('int64')) == set(source.entry_agg_trade_id.astype('int64'))
        for rr in model.RATIOS:
            selected = trades.loc[trades.model.eq(parent) & trades.rr.eq(rr)]
            assert not selected.session_date.duplicated().any()
            assert set(selected.entry_agg_trade_id) == set(p_rows.entry_agg_trade_id)
            merged = selected.merge(p_rows, on=['model', 'session_date', 'entry_agg_trade_id'], suffixes=('', '_old'), validate='one_to_one')
            for name in ('entry', 'poc', 'side', 'entry_timestamp_ny', 'fold'):
                assert merged[name].eq(merged[name+'_old']).all()
            if rr == 1:
                assert merged.exit_agg_trade_id.eq(merged.exit_agg_trade_id_old).all()
                np.testing.assert_allclose(merged.net_r, merged.net_r_old)
    reward = trades.side*(trades.poc-trades.entry)
    risk = reward/trades.rr
    np.testing.assert_allclose(trades.risk_price, risk)
    np.testing.assert_allclose(trades.sl, trades.entry-trades.side*risk)
    np.testing.assert_allclose(trades.target_distance_pct, 100*reward/trades.entry)
    np.testing.assert_allclose(trades.gross_r, trades.side*(trades.exit_price-trades.entry)/risk)
    np.testing.assert_allclose(trades.net_r, trades.gross_r-.0001*cfg['fee_bps_per_side']*(trades.entry+trades.exit_price)/risk)
    for key, group in trades.groupby(['model', 'entry_agg_trade_id']):
        g = group.sort_values('rr')
        assert (np.diff(g.exit_agg_trade_id) <= 0).all()
        assert (np.diff(g.status.eq('tp_first').astype(int)) <= 0).all()
    checked = 0
    for name in ('summary.csv', 'fold_metrics.csv', 'yearly_metrics.csv'):
        table = pd.read_csv(out/name)
        for r in table.itertuples():
            part = trades.loc[trades.model.eq(r.model) & trades.rr.eq(r.rr)]
            if hasattr(r, 'fold'):
                part = part.loc[part.fold.eq(r.fold)]
            if hasattr(r, 'year'):
                part = part.loc[part.session_date.str[:4].eq(str(r.year))]
            check(part.sort_values('session_date'), r, cfg)
            checked += 1
    summary = pd.read_csv(out/'summary.csv')
    for rr, group in summary.groupby('rr'):
        s = group.set_index('model')
        np.testing.assert_allclose(s.loc['union', 'net_total_r'], s.loc['distance', 'net_total_r']+
            s.loc['location', 'net_total_r']-s.loc['intersection', 'net_total_r'])
    raw = manifest['raw_source']
    stat = Path(raw['path']).stat()
    assert stat.st_size == raw['bytes'] and stat.st_mtime_ns == raw['mtime_ns']
    reader = model.WindowReader(Path(raw['path']))
    probes = []
    unique = trades.drop_duplicates(['session_date', 'entry_agg_trade_id', 'rr'])
    try:
        for day, group in unique.groupby('session_date', sort=True):
            _, start, end = model.base.bounds(date.fromisoformat(day))
            ts, prices, qty, ids = reader.window(start, end)
            assert model.base.quality(ts, prices, qty, ids, start, end, cfg) == 'valid'
            for r in group.itertuples():
                i = int(np.searchsorted(ids, int(r.entry_agg_trade_id)))
                assert prices[i] == r.entry
                if r.side == 1:
                    target = np.flatnonzero(prices[i+1:] >= r.poc)+i+1
                    stop = np.flatnonzero(prices[i+1:] <= r.sl)+i+1
                else:
                    target = np.flatnonzero(prices[i+1:] <= r.poc)+i+1
                    stop = np.flatnonzero(prices[i+1:] >= r.sl)+i+1
                ti = int(target[0]) if len(target) else len(prices)
                si = int(stop[0]) if len(stop) else len(prices)
                status = 'tp_first' if ti < si else 'sl_first' if si < ti else 'time_exit'
                j = min(ti, si, len(prices)-1)
                assert status == r.status and int(ids[j]) == int(r.exit_agg_trade_id)
                assert prices[j] == r.exit_price and model.base.iso(ts[j]) == r.exit_timestamp_ny
                probes.append(dict(session_date=day, entry_agg_trade_id=int(r.entry_agg_trade_id), rr=r.rr,
                    exit_agg_trade_id=int(ids[j]), status=status))
    finally:
        reader.close()
    pd.DataFrame(probes).to_csv(out/'raw_replay_checks.csv', index=False)
    result = dict(status='passed', parent_memberships_checked=len(parents), rr_trades_checked=len(trades),
        performance_rows_checked=checked, raw_unique_entry_rr_replayed=len(probes),
        every_unique_raw_entry_rr_verified=True, membership_frozen=True, checkpoint_unchanged=True,
        validator_sha256=model.base.sha(__file__))
    for path, digest in manifest['source_audits'].items():
        assert model.base.sha(path) == digest
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
