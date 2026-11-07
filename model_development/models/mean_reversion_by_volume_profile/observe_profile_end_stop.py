"""v22: v21 entries, previous profile extremes as stop, capped at 1:1 risk."""
import argparse
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

import observe_open_poc_touch as old

base = old.base


def stop_geometry(entry, poc, side, low, high):
    reward = side*(poc-entry)
    extreme = low if side == 1 else high
    profile_risk = side*(entry-extreme)
    if reward <= 0 or profile_risk <= 0:
        return None
    risk = min(profile_risk, reward)
    return dict(tp=float(poc), sl=float(entry-side*risk), risk_price=float(risk),
        risk_bps=float(10000*risk/entry), profile_stop=float(extreme),
        profile_stop_distance=float(profile_risk), reward_price=float(reward),
        planned_rr=float(reward/risk), stop_capped_to_equal=bool(profile_risk > reward))


def scan(prices, side, tp, sl, risk):
    targets = np.flatnonzero(side*(prices[1:]-tp) >= 0)
    stops = np.flatnonzero(side*(prices[1:]-sl) <= 0)
    it = int(targets[0])+1 if len(targets) else len(prices)
    iss = int(stops[0])+1 if len(stops) else len(prices)
    if it < iss:
        return 'tp_first', it, side*(tp-prices[0])/risk
    if iss < it:
        return 'sl_first', iss, -1.
    return 'time_exit', len(prices)-1, side*(prices[-1]-prices[0])/risk


def analyse(source, arrays, cfg):
    row = dict(source)
    if source['status'] not in base.ACTIVE:
        return row
    ts, prices, qty, ids = arrays
    start, opening, end = base.bounds(date.fromisoformat(source['session_date']))
    split = int(np.searchsorted(ts, opening))
    assert base.quality(*(a[:split] for a in arrays), start, opening, cfg) == 'valid'
    assert base.quality(*(a[split:] for a in arrays), opening, end, cfg) == 'valid'
    assert ids[split] == ids[split-1]+1
    i = int(np.searchsorted(ids, int(source['entry_agg_trade_id'])))
    assert i < len(ids) and ids[i] == int(source['entry_agg_trade_id'])
    assert prices[i] == source['entry'] and i >= split
    assert base.iso(ts[i]) == source['entry_timestamp_ny']
    # Reproduce the old outcome on the same raw prints before changing the stop.
    previous = base.outcome(prices[i:], source['poc'], -int(source['side']), 'endpoint')
    assert previous['status'] == source['status']
    assert ids[i+previous['exit_index']] == int(source['exit_agg_trade_id'])
    low, high = float(prices[:split].min()), float(prices[:split].max())
    row.update(source_v21_status=source['status'], source_v21_gross_r=source['gross_r'],
        source_v21_net_r=source['net_r'], profile_low=low, profile_high=high)
    geometry = stop_geometry(source['entry'], source['poc'], int(source['side']), low, high)
    if geometry is None:
        # No artificial risk floor or replacement trade for zero/negative stop distance.
        for key in ('gross_r', 'net_r', 'risk_price', 'sl', 'exit_price', 'cost_r_per_bps'):
            row[key] = None
        return dict(row, status='invalid_profile_stop_geometry')
    row.update(geometry)
    status, j, barrier = scan(prices[i:], int(source['side']), source['poc'], row['sl'], row['risk_price'])
    exit_price, risk, entry = float(prices[i+j]), row['risk_price'], source['entry']
    gross = source['side']*(exit_price-entry)/risk
    cost = .0001*(entry+exit_price)/risk
    row.update(status=status, barrier_gross_r=float(barrier), gross_r=float(gross),
        net_r=float(gross-cfg['fee_bps_per_side']*cost), cost_r_per_bps=float(cost),
        exit_price=exit_price, trigger_trade_price=exit_price,
        exit_timestamp_ny=base.iso(ts[i+j]), exit_agg_trade_id=int(ids[i+j]),
        target_distance_pct=100*row['reward_price']/entry,
        hold_minutes=float((ts[i+j]-ts[i])/60000))
    assert start <= ts[0] < opening <= ts[i] <= ts[i+j] < end
    assert row['planned_rr'] >= 1 and risk <= row['reward_price']
    return row


def write_comparison(frame, source, cfg, out):
    new = pd.read_csv(out/'summary.csv')
    before = pd.read_csv(source/'summary.csv')
    keys = ['symbol', 'sample']
    comparison = before.merge(new, on=keys, suffixes=('_v21', '_v22'), validate='one_to_one')
    comparison.to_csv(out/'comparison.csv', index=False)
    old_folds = pd.read_csv(source/'fold_metrics.csv')
    new_folds = pd.read_csv(out/'fold_metrics.csv')
    fold_keys = ['symbol', 'fold', 'phase', 'train_start', 'test_start', 'test_end_exclusive']
    pd.testing.assert_frame_equal(old_folds[fold_keys], new_folds[fold_keys])
    fold_compare = old_folds.merge(new_folds, on=fold_keys, suffixes=('_v21', '_v22'), validate='one_to_one')
    fold_compare.to_csv(out/'fold_comparison.csv', index=False)
    counts, stop_stats = [], []
    for symbol, group in frame.groupby('symbol'):
        valid = group.loc[group.valid_session]
        dates = set()
        tests = new_folds.loc[new_folds.symbol.eq(symbol) & new_folds.phase.eq('test')]
        for r in tests.itertuples():
            dates.update(valid.loc[valid.session_date.ge(r.test_start) & valid.session_date.lt(r.test_end_exclusive), 'session_date'])
        for sample, population in (('all_available', valid), ('walk_forward_test', valid.loc[valid.session_date.isin(dates)])):
            trades = population.loc[population.status.isin(base.ACTIVE)]
            counts.append(dict(symbol=symbol, sample=sample, valid_sessions=len(population), entries=len(trades),
                tp=int(trades.status.eq('tp_first').sum()), sl=int(trades.status.eq('sl_first').sum()),
                time_exit=int(trades.status.eq('time_exit').sum()), gross_wins=int(trades.gross_r.gt(0).sum()),
                net_wins=int(trades.net_r.gt(0).sum()), start=population.session_date.min(), end=population.session_date.max()))
            stop_stats.append(dict(symbol=symbol, sample=sample, trades=len(trades),
                capped_to_equal=int(trades.stop_capped_to_equal.eq(True).sum()),
                above_one_rr=int(trades.planned_rr.gt(1+1e-12).sum()),
                median_planned_rr=trades.planned_rr.median(), mean_planned_rr=trades.planned_rr.mean(),
                max_planned_rr=trades.planned_rr.max(), median_stop_pct=(100*trades.risk_price/trades.entry).median(),
                invalid_stop_geometry=int(population.status.eq('invalid_profile_stop_geometry').sum())))
    pd.DataFrame(counts).to_csv(out/'entry_win_counts.csv', index=False)
    stops = pd.DataFrame(stop_stats)
    stops.to_csv(out/'stop_statistics.csv', index=False)
    lines = ['# Profile-end stop capped at 1:1 — v22', '',
        'Exactly the v21 opening bias and first VAL/VAH touch entries. '
        'Long: stop at previous profile minimum price; short: previous profile maximum price. '
        'Risk distance = min(distance to that extreme, distance to POC). '
        'TP frozen POC, reward:risk >=1:1, one trade per NY-open period. '
        'No thresholds fitted, no extra filters, candles or orderflow. '
        'Extremes use raw prints from previous 09:30 NY inclusive to current 09:30 exclusive; '
        'no profile rebuild and no future information. Nonpositive stop geometry is separately excluded.', '',
        'Entry and barrier exits use observed trigger prints, not guaranteed fills exactly at levels. '
        'Realized losses may exceed -1R after overshoot/fees. Fee 4 bps EACH side; '
        'funding, spread, latency and book impact excluded. '
        'R is normalized to the NEW stop distance. Account illustration retains 0.5% equity risk '
        'and 5x entry-notional cap; a narrower stop may imply larger notional. '
        'Position return percentages are also supplied in summary.csv. '
        'Same 8+2 calendar and valid-session denominator as v21. Previously studied development history.', '',
        '| Asset | Sample | Entries old → new | Gross WR old → new | Gross mean R old → new | Net mean R old → new | Net total R old → new | Median planned reward:risk |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for r in comparison.to_dict('records'):
        s = stops.loc[stops.symbol.eq(r['symbol']) & stops['sample'].eq(r['sample'])].iloc[0]
        lines.append(f"| {r['symbol']} | {r['sample']} | {int(r['trades_v21'])} → {int(r['trades_v22'])} | "
            f"{r['gross_wr_pct_v21']:.2f}% → {r['gross_wr_pct_v22']:.2f}% | "
            f"{r['gross_mean_r_v21']:.4f} → {r['gross_mean_r_v22']:.4f} | "
            f"{r['net_mean_r_v21']:.4f} → {r['net_mean_r_v22']:.4f} | "
            f"{r['net_total_r_v21']:.2f} → {r['net_total_r_v22']:.2f} | {s.median_planned_rr:.3f}:1 |")
    lines += ['', '| Asset | Sample | TP | SL | Time exit | Gross profitable closes | Net profitable closes |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in counts:
        lines.append(f"| {r['symbol']} | {r['sample']} | {r['tp']} | {r['sl']} | {r['time_exit']} | {r['gross_wins']} | {r['net_wins']} |")
    lines += ['', '| Asset | Positive net test folds old → new | Net expectancy improvement folds |', '|---|---:|---:|']
    for symbol, g in fold_compare.loc[fold_compare.phase.eq('test')].groupby('symbol'):
        lines.append(f"| {symbol} | {int(g.net_mean_r_v21.gt(0).sum())}/{len(g)} → {int(g.net_mean_r_v22.gt(0).sum())}/{len(g)} | {int(g.net_mean_r_v22.gt(g.net_mean_r_v21).sum())}/{len(g)} |")
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return comparison


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--source-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    source, out = args.source_run.resolve(), args.output.resolve()
    manifest = json.loads((source/'config_snapshot.json').read_text())
    audit = json.loads((source/'audit.json').read_text())
    assert audit['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(source/name) == digest, name
    assert base.sha(old.__file__) == manifest['code_sha256']
    for path, digest in manifest['dependencies'].items():
        assert base.sha(path) == digest, path
    cfg = manifest['config']
    original_profiles = Path(manifest['source_run'])
    assert base.sha(original_profiles/'audit.json') == manifest['source_audit_sha256']
    raw_manifest = json.loads((original_profiles/'config_snapshot.json').read_text())
    for symbol, r in raw_manifest['sources'].items():
        stat = (args.root/cfg['assets'][symbol]).stat()
        assert stat.st_size == r['bytes'] and stat.st_mtime_ns == r['mtime_ns']
    data = pd.read_csv(source/'sessions.csv', float_precision='round_trip')
    out.mkdir(parents=True, exist_ok=False)
    result = []
    for symbol, group in data.groupby('symbol', sort=True):
        reader = old.WindowReader(args.root/cfg['assets'][symbol])
        try:
            with (out/(symbol+'_daily.jsonl')).open('w', encoding='utf-8') as log:
                for n, row in enumerate(group.sort_values('session_date').to_dict('records'), 1):
                    clean = {k:v for k,v in row.items() if pd.notna(v)}
                    arrays = None
                    if row['status'] in base.ACTIVE:
                        start, _, end = base.bounds(date.fromisoformat(row['session_date']))
                        arrays = reader.window(start, end)
                    analysed = analyse(clean, arrays, cfg)
                    result.append(analysed)
                    log.write(json.dumps(analysed, allow_nan=False)+'\n')
                    if n % 200 == 0:
                        log.flush()
                        print(symbol, n, row['session_date'], flush=True)
            print(symbol, 'finished; row groups', reader.groups_read, flush=True)
        finally:
            reader.close()
    frame = pd.DataFrame(result)
    assert not frame.duplicated(['symbol', 'session_date']).any()
    base.write_reports(frame, cfg, out)
    comparison = write_comparison(frame, source, cfg, out)
    snapshot = dict(rule_version='profile_end_stop_v22', config=cfg, source_run=str(source),
        source_audit_sha256=base.sha(source/'audit.json'), code_sha256=base.sha(__file__),
        profiles_built=0, dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (old, base, __import__('observe_half_pullback'), __import__('observe_second_push'),
             __import__('backtest_poc_rr'), __import__('observe'), __import__('observe_four_profiles'))})
    (out/'config_snapshot.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', sessions=len(frame),
        trades=int(frame.status.isin(base.ACTIVE).sum()), profiles_built=0, outputs=outputs), indent=2), encoding='utf-8')
    print(comparison[['symbol', 'sample', 'trades_v21', 'trades_v22', 'gross_wr_pct_v21',
        'gross_wr_pct_v22', 'gross_mean_r_v21', 'gross_mean_r_v22', 'net_mean_r_v21', 'net_mean_r_v22']].to_string(index=False), flush=True)


if __name__ == '__main__':
    main()
