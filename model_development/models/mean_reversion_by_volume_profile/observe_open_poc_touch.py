"""v21: NY-open POC bias, first touch of its VAL/VAH, no candle condition."""
import argparse
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

import previous_value_area_reversal as base
from observe_half_pullback import WindowReader

COMMON = ('symbol', 'session_date', 'valid_session', 'profile_start_ny',
          'profile_end_ny', 'period_end_ny', 'period_hours', 'opening_price',
          'poc', 'val', 'vah', 'profile_volume')


def candidate(prices, poc, val, vah):
    side = int(np.sign(poc-prices[0]))
    if not side:
        return None
    edge, level = ('val', val) if side == 1 else ('vah', vah)
    # Opening establishes bias. A pre-open crossing cannot trigger an entry.
    previous = np.r_[prices[0], prices[:-1]]
    hits = np.flatnonzero((prices == level) | ((previous-level)*(prices-level) < 0))
    return (int(hits[0]), side, edge, level) if len(hits) else None


def analyse(source, arrays, cfg):
    row = {k: source[k] for k in COMMON if k in source and pd.notna(source[k])}
    if not source['valid_session']:
        return dict(row, status='source_'+source['status'])
    ts, prices, qty, ids = arrays
    _, opening, end = base.bounds(date.fromisoformat(source['session_date']))
    assert base.quality(*arrays, opening, end, cfg) == 'valid'
    assert prices[0] == source['opening_price']
    row['opening_location'] = ('below_val' if prices[0] < source['val'] else
        'above_vah' if prices[0] > source['vah'] else 'within_va')
    row['opening_bias'] = int(np.sign(source['poc']-prices[0]))
    if not row['opening_bias']:
        return dict(row, status='open_at_poc')
    found = candidate(prices, source['poc'], source['val'], source['vah'])
    if found is None:
        return dict(row, status='no_selected_edge_touch')
    i, side, edge, level = found
    row.update(touch_edge=edge, touch_boundary=level,
               touch_timestamp_ny=base.iso(ts[i]), touch_agg_trade_id=int(ids[i]))
    result = base.outcome(prices[i:], source['poc'], -side, 'endpoint')
    j = result.pop('exit_index', None)
    row.update(result)
    if j is None:
        return row  # First candidate consumed even if jump makes TP invalid.
    entry, risk, exit_price = result['entry'], result['risk_price'], float(prices[i+j])
    gross = side*(exit_price-entry)/risk
    cost = .0001*(entry+exit_price)/risk
    row.update(barrier_gross_r=result['gross_r'], gross_r=gross,
        exit_price=exit_price, cost_r_per_bps=cost,
        net_r=gross-cfg['fee_bps_per_side']*cost,
        entry_timestamp_ny=base.iso(ts[i]), exit_timestamp_ny=base.iso(ts[i+j]),
        entry_agg_trade_id=int(ids[i]), exit_agg_trade_id=int(ids[i+j]),
        target_distance_pct=100*risk/entry, hold_minutes=float((ts[i+j]-ts[i])/60000))
    assert opening <= ts[i] <= ts[i+j] < end
    assert np.isclose(row['sl'], 2*entry-source['poc'])
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--profiles-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    source, out = args.profiles_run.resolve(), args.output.resolve()
    snapshot = json.loads((source/'config_snapshot.json').read_text())
    audit = json.loads((source/'audit.json').read_text())
    assert audit['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(source/name) == digest, name
    cfg = snapshot['config']
    for symbol, record in snapshot['sources'].items():
        stat = (args.root/cfg['assets'][symbol]).stat()
        assert stat.st_size == record['bytes'] and stat.st_mtime_ns == record['mtime_ns']
    data = pd.read_csv(source/'sessions.csv', float_precision='round_trip')
    data = data.loc[data.timeframe.eq('15m') & data.entry_mode.eq('body_close')]
    assert not data.duplicated(['symbol', 'session_date']).any()
    out.mkdir(parents=True, exist_ok=False)
    all_rows = []
    for symbol, group in data.groupby('symbol', sort=True):
        reader = WindowReader(args.root/cfg['assets'][symbol])
        try:
            with (out/(symbol+'_daily.jsonl')).open('w', encoding='utf-8') as log:
                for n, row in enumerate(group.sort_values('session_date').to_dict('records'), 1):
                    arrays = None
                    if row['valid_session']:
                        _, start, end = base.bounds(date.fromisoformat(row['session_date']))
                        arrays = reader.window(start, end)
                    result = analyse(row, arrays, cfg)
                    all_rows.append(result)
                    log.write(json.dumps(result, allow_nan=False)+'\n')
                    if n % 200 == 0:
                        log.flush()
                        print(symbol, n, row['session_date'], flush=True)
            print(symbol, 'finished; row groups', reader.groups_read, flush=True)
        finally:
            reader.close()
    frame = pd.DataFrame(all_rows)
    assert not frame.duplicated(['symbol', 'session_date']).any()
    trades = frame.loc[frame.status.isin(base.ACTIVE)]
    np.testing.assert_allclose(trades.net_r,
        trades.gross_r-.0001*cfg['fee_bps_per_side']*(trades.entry+trades.exit_price)/trades.risk_price)
    assert trades.side.eq(trades.opening_bias).all()
    base.write_reports(frame, cfg, out)
    summary = pd.read_csv(out/'summary.csv')
    lines = ['# Open-to-POC bias, VAL/VAH touch — v21', '',
        'Frozen previous 09:30 NY to current 09:30 NY profile, reused from v17. '
        'Opening below POC: long only at VAL; above POC: short only at VAH. '
        'Opening anywhere allowed; exactly at POC skipped. Direction stays fixed all period, '
        'including if POC is reached before entry. First observed equality/crossing of selected edge '
        'after opening; opening exactly on edge enters immediately. No pre-open crossing, candles, '
        'push count, orderflow, or fitted thresholds. Maximum one trade per period. '
        'TP frozen POC; symmetric stop based on actual touch print; time exit before next NY open.', '',
        'Observed trade-print entry/exit proxy; crossings fill at observed price, not a guaranteed '
        'limit fill exactly on the boundary. Invalid first fill consumes opportunity. '
        'Fee 4 bps each side. No funding, spread, latency or book impact. '
        'Same quality population and 8-month train / 2-month test / step 2 calendar as v17. '
        'No fitting; previously studied history remains development evidence.', '',
        '| Asset | Sample | Valid sessions | Entries | TP | SL | Time exit | Gross winning closes | Net winning closes | Mean gross R | Mean net R |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in summary.to_dict('records'):
        lines.append(f"| {r['symbol']} | {r['sample']} | {int(r['valid_sessions'])} | {int(r['trades'])} | "
            f"{int(r['tp_first'])} | {int(r['sl_first'])} | {int(r['time_exit'])} | "
            f"{r['gross_wr_pct']:.2f}% | {r['net_wr_pct']:.2f}% | {r['gross_mean_r']:.4f} | {r['net_mean_r']:.4f} |")
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    manifest = dict(rule_version='open_poc_touch_v21', config=cfg,
        source_run=str(source), source_audit_sha256=base.sha(source/'audit.json'),
        code_sha256=base.sha(__file__), profiles_built=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (base, __import__('observe_half_pullback'), __import__('observe_second_push'),
             __import__('backtest_poc_rr'), __import__('observe'), __import__('observe_four_profiles'))})
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', sessions=len(frame),
        trades=len(trades), profiles_built=0, outputs=outputs), indent=2), encoding='utf-8')
    print(summary.to_string(index=False), flush=True)


if __name__ == '__main__':
    main()
