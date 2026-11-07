"""v24: A only, first VA touch -> 50% pullback -> same-edge touch entry."""
import argparse
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

import observe_open_poc_touch as old
from observe_half_pullback import WindowReader

base = old.base
MODES = ('wick_half', 'close_half')
WIDTH = 15*60000


def setup(ts, prices, first, edge, level, poc, opening, mode):
    side = 1 if edge == 'val' else -1
    distance = abs(poc-level)
    result = dict(status='no_half_pullback', pullback_midpoint=(poc+level)/2)
    if mode == 'wick_half':
        hits = np.flatnonzero(side*(prices[first+1:]-level) >= .5*distance)
        if not len(hits):
            return result
        pi = int(hits[0])+first+1
        available, start = int(ts[pi]), pi+1
    else:
        first_end = opening+((int(ts[first])-opening)//WIDTH+1)*WIDTH
        ends = np.arange(first_end, int(ts[-1])+1, WIDTH, dtype=np.int64)
        positions = np.searchsorted(ts, ends)
        good = (positions > first+1) & (positions < len(ts))
        ends, positions = ends[good], positions[good]
        hits = np.flatnonzero(side*(prices[positions-1]-level) >= .5*distance)
        if not len(hits):
            return result
        k = int(hits[0])
        start, pi, available = int(positions[k]), int(positions[k]-1), int(ends[k])
    result.update(status='no_second_touch', pullback_index=pi,
        pullback_price=float(prices[pi]), pullback_available_ms=available,
        pullback_observed_ms=int(ts[pi]))
    previous = prices[start-1:-1]
    hits = np.flatnonzero((prices[start:] == level) | ((previous-level)*(prices[start:]-level) < 0))
    if len(hits):
        result.update(status='candidate', entry_index=start+int(hits[0]))
    return result


def daily(source, arrays, cfg):
    common = {k:source[k] for k in old.COMMON if k in source}
    common.update(entry_rule='A_first_edge', opening_eligible=bool(source['opening_eligible']))
    if not source['valid_session'] or not source['opening_eligible']:
        return [dict(common, pullback_mode=m, status=source['status']) for m in MODES]
    ts, prices, qty, ids = arrays
    _, opening, end = base.bounds(date.fromisoformat(source['session_date']))
    assert base.quality(*arrays[:4], opening, end, cfg) == 'valid'
    assert prices[0] == source['opening_price']
    first = base.first_touch(prices[1:], float(prices[0]), source['val'], source['vah'])
    if first is None:
        assert source['status'] == 'no_selected_edge_touch'
        return [dict(common, pullback_mode=m, status='no_first_touch') for m in MODES]
    fi, side, edge, level = first['index']+1, first['side'], first['edge'], first['boundary']
    assert int(ids[fi]) == int(source['touch_agg_trade_id'])
    common.update(touch_edge=edge, touch_boundary=float(level),
        first_touch_ny=base.iso(ts[fi]), first_touch_agg_trade_id=int(ids[fi]))
    rows = []
    for mode in MODES:
        row = dict(common, pullback_mode=mode)
        event = setup(ts, prices, fi, edge, level, source['poc'], opening, mode)
        row.update(status=event['status'], pullback_midpoint=event['pullback_midpoint'])
        if 'pullback_index' in event:
            row.update(pullback_price=event['pullback_price'],
                pullback_available_ny=base.iso(event['pullback_available_ms']),
                pullback_observed_ny=base.iso(event['pullback_observed_ms']),
                pullback_agg_trade_id=int(ids[event['pullback_index']]))
        if event['status'] != 'candidate':
            rows.append(row)
            continue
        i = event['entry_index']
        assert i > event['pullback_index'] > fi and ts[i] >= event['pullback_available_ms']
        row.update(touch_timestamp_ny=base.iso(ts[i]), touch_agg_trade_id=int(ids[i]),
            entry_timestamp_ny=base.iso(ts[i]), entry_agg_trade_id=int(ids[i]))
        scored = base.outcome(prices[i:], source['poc'], -side, 'endpoint')
        j = scored.pop('exit_index', None)
        row.update(scored)
        if j is None:
            rows.append(row)
            continue
        entry, risk, exit_price = row['entry'], row['risk_price'], float(prices[i+j])
        gross = side*(exit_price-entry)/risk
        cost = .0001*(entry+exit_price)/risk
        row.update(barrier_gross_r=float(row['gross_r']), gross_r=float(gross),
            net_r=float(gross-cfg['fee_bps_per_side']*cost), cost_r_per_bps=float(cost),
            exit_price=exit_price, exit_timestamp_ny=base.iso(ts[i+j]), exit_agg_trade_id=int(ids[i+j]),
            target_distance_pct=float(100*risk/entry), hold_minutes=float((ts[i+j]-ts[i])/60000))
        rows.append(row)
    return rows


def report(data, cfg, source, out):
    summaries, fold_rows, years = [], [], []
    def metrics(part, population):
        return dict(**base.stats(part, len(population), cfg),
            opening_eligible_sessions=int(population.opening_eligible.sum()),
            gross_wins=int(part.gross_r.gt(0).sum()), net_wins=int(part.net_r.gt(0).sum()))
    for (symbol, mode), group in data.groupby(['symbol', 'pullback_mode'], sort=True):
        identity = dict(symbol=symbol, pullback_mode=mode)
        valid = group.loc[group.valid_session]
        tests = []
        for fold, (start, boundary, finish) in enumerate(base.fold_schedule(valid.session_date.min(), valid.session_date.max()), 1):
            train = valid.loc[valid.session_date.ge(str(start.date())) & valid.session_date.lt(str(boundary.date()))]
            test = valid.loc[valid.session_date.ge(str(boundary.date())) & valid.session_date.lt(str(finish.date()))]
            tests.append(test)
            for phase, population in (('train', train), ('test', test)):
                entries = population.loc[population.status.isin(base.ACTIVE)]
                purged = 0
                if phase == 'train' and len(entries):
                    cut = pd.Timestamp(boundary.date()).tz_localize(base.NY).tz_convert('UTC')
                    keep = pd.to_datetime(entries.exit_timestamp_ny, utc=True, format='ISO8601').lt(cut)
                    purged = int((~keep).sum())
                    entries = entries.loc[keep]
                fold_rows.append(dict(**identity, fold=fold, phase=phase, train_start=str(start.date()),
                    test_start=str(boundary.date()), test_end_exclusive=str(finish.date()),
                    purged_train_outcomes=purged, limited_sample=len(entries) < 20,
                    **metrics(entries, population)))
        tested = pd.concat(tests, ignore_index=True) if tests else valid.iloc[:0]
        assert not tested.session_date.duplicated().any()
        for sample, population in (('all_available', valid), ('walk_forward_test', tested)):
            summaries.append(dict(**identity, sample=sample,
                **metrics(population.loc[population.status.isin(base.ACTIVE)], population)))
            for year, part in population.groupby(population.session_date.str[:4]):
                years.append(dict(**identity, sample=sample, year=year,
                    **metrics(part.loc[part.status.isin(base.ACTIVE)], part)))
    summary = pd.DataFrame(summaries)
    folds = pd.DataFrame(fold_rows)
    summary.to_csv(out/'summary.csv', index=False)
    folds.to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(years).to_csv(out/'yearly_metrics.csv', index=False)
    data.groupby(['symbol', 'pullback_mode', 'status']).size().rename('sessions').reset_index().to_csv(out/'quality.csv', index=False)
    data.to_parquet(out/'sessions.parquet', index=False)
    before = pd.read_csv(source/'summary.csv')
    before = before.loc[before.direction.eq('first_edge') & before.policy.eq('base_all')].drop(columns=['direction', 'policy'])
    compare = summary.merge(before, on=['symbol', 'sample'], suffixes=('_second', '_first'), validate='many_to_one')
    compare.to_csv(out/'comparison.csv', index=False)
    old_folds = pd.read_csv(source/'fold_metrics.csv')
    old_keys = old_folds.loc[old_folds.direction.eq('first_edge') & old_folds.policy.eq('base_all'),
        ['symbol', 'fold', 'phase', 'train_start', 'test_start', 'test_end_exclusive']]
    for mode in MODES:
        new_keys = folds.loc[folds.pullback_mode.eq(mode), old_keys.columns]
        pd.testing.assert_frame_equal(new_keys.reset_index(drop=True), old_keys.reset_index(drop=True))
    lines = ['# A with second-touch / 50% pullback — v24', '',
        'A only: NY open strictly inside frozen previous 09:30–09:30 VA. First touched edge locks direction: '
        'VAH short, VAL long. Do not enter at first touch. Wait for at least 50% movement from that edge toward POC, '
        'then enter first subsequent observed touch/crossing of the SAME edge. '
        'wick_half observes the first trade print reaching the midpoint (no bar-close wait). '
        'close_half waits for a completed 15m interval close reaching midpoint; intervals aligned to NY 09:30 '
        '(also standard UTC 15m grid). The first-touch interval itself may qualify once completed. '
        'Movement beyond POC is allowed, no cancellation or direction switching. '
        'This carries the 50% condition from v19 but removes its body-touch and separate-pullback-candle conditions.', '',
        'One candidate per mode/period. TP frozen POC, SL symmetric 1:1 based on actual entry print. '
        'No delta, CVD, orderflow, bias-open-to-POC selection or other filters. '
        'Time exit before next NY open. Fee 4 bps each side, risk illustration 0.5% equity / 5x notional cap. '
        'No profile rebuilding or fitted thresholds. Same 8+2 train/test calendar, existing development history. '
        'Trade-print proxy excludes funding, spread, latency and book impact.', '',
        'Second-touch trades have new entry times and outcomes; '
        'they are not a simple subset of old first-touch trades.', '',
        '| Asset | Pullback | Entries first → second | Gross WR first → second | Net mean R first → second |',
        '|---|---|---:|---:|---:|']
    for r in compare.loc[compare['sample'].eq('walk_forward_test')].itertuples():
        lines.append(f'| {r.symbol} | {r.pullback_mode} | {int(r.trades_first)} → {int(r.trades_second)} | '
            f'{r.gross_wr_pct_first:.2f}% → {r.gross_wr_pct_second:.2f}% | {r.net_mean_r_first:+.4f} → {r.net_mean_r_second:+.4f} |')
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return summary


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
    for path, digest in manifest['dependencies'].items():
        assert base.sha(path) == digest, path
    cfg = manifest['config']
    v21 = json.loads((Path(manifest['source_run'])/'config_snapshot.json').read_text())
    raw = json.loads((Path(v21['source_run'])/'config_snapshot.json').read_text())
    for symbol, r in raw['sources'].items():
        stat = (args.root/cfg['assets'][symbol]).stat()
        assert stat.st_size == r['bytes'] and stat.st_mtime_ns == r['mtime_ns']
    cached = pd.read_parquet(source/'sessions.parquet')
    cached = cached.loc[cached.direction.eq('first_edge')]
    out.mkdir(parents=True, exist_ok=False)
    all_rows = []
    for symbol, group in cached.groupby('symbol', sort=True):
        reader = WindowReader(args.root/cfg['assets'][symbol])
        try:
            with (out/(symbol+'_daily.jsonl')).open('w', encoding='utf-8') as log:
                for n, record in enumerate(group.sort_values('session_date').to_dict('records'), 1):
                    clean = {k:v for k,v in record.items() if pd.notna(v)}
                    arrays = None
                    if clean['valid_session'] and clean['opening_eligible']:
                        _, start, end = base.bounds(date.fromisoformat(clean['session_date']))
                        arrays = reader.window(start, end)
                    rows = daily(clean, arrays, cfg)
                    all_rows.extend(rows)
                    for row in rows:
                        log.write(json.dumps(row, allow_nan=False)+'\n')
                    if n % 200 == 0:
                        log.flush()
                        print(symbol, n, clean['session_date'], flush=True)
            print(symbol, 'finished; row groups', reader.groups_read, flush=True)
        finally:
            reader.close()
    data = pd.DataFrame(all_rows)
    assert not data.duplicated(['symbol', 'pullback_mode', 'session_date']).any()
    summary = report(data, cfg, source, out)
    modules = (old, base, __import__('observe_half_pullback'), __import__('observe_second_push'), __import__('backtest_poc_rr'),
        __import__('observe'), __import__('observe_four_profiles'))
    snapshot = dict(rule_version='second_touch_base_v24', config=cfg,
        source_run=str(source), source_audit_sha256=base.sha(source/'audit.json'),
        code_sha256=base.sha(__file__), profiles_built=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in modules})
    (out/'config_snapshot.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', sessions=len(data),
        profiles_built=0, outputs=outputs), indent=2), encoding='utf-8')
    print(summary.loc[summary['sample'].eq('walk_forward_test'),
        ['symbol', 'pullback_mode', 'trades', 'gross_wr_pct', 'net_mean_r']].to_string(index=False), flush=True)


if __name__ == '__main__':
    main()
