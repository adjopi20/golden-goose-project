"""Export frozen entry subsets; execution belongs to backtest_engine.replay_aggtrades.

No future outcomes are read by any selector. Existing historical test-fold
membership is retained; ETH August is a separately tagged forward extension.
"""
import argparse
import csv
from collections import defaultdict, deque
from datetime import date
import hashlib
import json
from pathlib import Path
import statistics

POLICIES = {
    'BTCUSDC': ('ema_0p5', 'adx_25', 'adx_30'),
    'ETHUSDC': ('baseline', 'c2_union_i_a_p', 'ma_aligned', 'ema_0p5', 'adx_30', 'c1_delta'),
    'BNBUSDC': ('c2_initiative', 'ema_0p1', 'adx_30'),
    'HYPEUSDT': ('baseline', 'c1_delta', 'atr_activation', 'c2_initiative',
                 'c2_persistence', 'ma_aligned', 'ma_mixed', 'adx_25', 'c2_union_i_p'),
}


def read(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line]


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, allow_nan=False)+'\n' for r in rows), encoding='utf-8')


def keep(row, policy):
    trend = row['trend_15m']
    state = row['effort_result_pre_entry']['setup_direction']
    labels = state['labels']
    initiative = 'initiative' in labels
    persistence = state['state'] == 'SUPPORT_PERSISTING'
    if policy == 'baseline': return True
    if policy in ('c1_delta', 'atr_activation'):
        plug = 'delta_without_result' if policy == 'c1_delta' else policy
        return row['research_gates'][plug]['gate'] is not False
    if policy == 'c2_initiative': return initiative
    if policy == 'c2_persistence': return persistence
    if policy == 'c2_union_i_p': return initiative or persistence
    if policy == 'c2_union_i_a_p': return initiative or persistence or 'possible_absorption' in labels
    if policy.startswith('ma_'): return trend['trade_alignment'] == policy[3:]
    if policy.startswith('ema_'):
        value = trend.get('price_vs_ema200_pct')
        sign = 1 if row['direction'] == 'long' else -1
        return value is None or sign*value >= float(policy[4:].replace('p', '.'))
    if policy.startswith('adx_'):
        value = trend.get('adx14')
        return value is None or value >= float(policy[4:])
    raise ValueError(policy)


def extend_eth(source, august_obs, august_cache, study):
    """Same existing feature functions, only new August signals, no outcome fitting."""
    import numpy as np
    from .audit_opportunity import (load_minutes, hourly_indicators, trend_indicators_15m,
                                    trend_snapshot_15m, feature_snapshot)
    from .evaluate_plugs import _bars_by_day, slot, candidate_features, _gate
    from .effort_result import annotate_session

    old_obs = Path(source['observation_dir'])
    bars = _bars_by_day(old_obs)
    bars.update(_bars_by_day(august_obs))
    profiles = {p['session_day']: p for folder in (old_obs, august_obs)
                for p in read(folder/'prepared_profiles.jsonl') if p['profile_window'] == 'pre_ny'}
    # July 31 is context only, filling the gap after the old July-30 ledger.
    new_signals = [s for s in read(august_obs/'pre_ny/signals.jsonl') if s['session_day'] >= '2026-08-01']
    if any(not '2026-08-01' <= s['session_day'] <= '2026-08-30' for s in new_signals):
        raise ValueError('ETH extension must contain only 2026-08-01 through 2026-08-30')
    folds = [r for r in read(study/'folds.jsonl') if r['symbol'] == 'ETHUSDC'
             and r['profile_window'] == 'pre_ny' and r['plug'] == 'delta_without_result'
             and r['train_end_exclusive'] < '2026-08-01' and r['test_start'] < '2026-08-01']
    latest = max(folds, key=lambda r: r['test_start'])
    thresholds = latest['thresholds']
    if not thresholds: raise ValueError('Missing pre-August C1 threshold')
    minutes = load_minutes([Path(p['path']) for p in source['minute_caches']] + [august_cache])
    hours = hourly_indicators(minutes)
    trend = trend_indicators_15m(minutes)
    ends, atrs = hours.end_ms.to_numpy(), hours.atr14.to_numpy()
    stamps = minutes.timestamp_ms.to_numpy()
    history = defaultdict(lambda: deque(maxlen=20))
    lookup = {}
    for day in sorted(profiles):
        session = [b for b in bars.get(day, []) if 36 <= slot(int(b['open_timestamp_ms'])) < 48]
        refs = {}
        for q in range(12):
            refs[q] = {'prior_sessions': len(history[q])}
            if len(history[q]) >= 10:
                refs[q].update({k: statistics.median(p[k] for p in history[q])
                                for k in ('volume', 'buy_volume', 'sell_volume', 'body')})
        if day >= '2026-08-01':
            atr_map = {}
            for b in session:
                stamp = int(b['open_timestamp_ms'])
                i = int(np.searchsorted(ends, stamp, side='right'))-1
                if i >= 0 and 0 <= stamp-int(ends[i]) < 3600000 and np.isfinite(atrs[i]) and atrs[i] > 0:
                    atr_map[stamp] = float(atrs[i])
            lookup[day] = {r['feature_as_of_ms']: r for r in
                           annotate_session(day, session, profiles[day], refs, atr_map)}
        for b in session:
            q = slot(int(b['open_timestamp_ms']))-36
            history[q].append(dict(volume=float(b['buy_volume'])+float(b['sell_volume']),
                buy_volume=float(b['buy_volume']), sell_volume=float(b['sell_volume']),
                body=abs(float(b['close'])-float(b['open']))))
    rows = []
    for signal in new_signals:
        day, asof = signal['session_day'], int(signal['feature_as_of_ms'])
        snapshot = feature_snapshot(minutes, stamps, hours, asof, profiles[day])
        # Only delta/result features drive C1; reference fill is not used as a gate.
        feature_row = dict(session_day=day, actual_direction=signal['direction'],
            actual_entry_ms=signal['entry_eligible_timestamp_ms'],
            actual_entry_price=signal['entry_reference'], actual_stop=signal['stop'])
        features = candidate_features(feature_row, snapshot, profiles[day], bars, sorted(bars), 4.)
        evidence = lookup[day][asof]
        if evidence['status'] != 'observed': raise ValueError(f'Missing ETH evidence: {day}')
        rows.append(dict(symbol='ETHUSDC', sample_id=signal['sample_id'], session_day=day,
            direction=signal['direction'], feature_as_of_ms=asof,
            trend_15m=trend_snapshot_15m(trend, asof, signal['direction']),
            effort_result_pre_entry=dict(setup_direction=evidence['directions'][signal['direction']]),
            features=features, research_gates={'delta_without_result': dict(
                gate=_gate(features, 'delta_without_result', thresholds),
                threshold_source='frozen_before_august', thresholds=thresholds)},
            research_period='eth_august_extension'))
    return rows, new_signals, dict(frozen_c1_fold=latest, extension_candidates=len(rows))


def prepare(symbol, context_dir, output, august_obs=None, august_cache=None):
    manifest = json.loads((context_dir/'manifest.json').read_text(encoding='utf-8'))
    source = next(s for s in manifest['sources'] if s['symbol'] == symbol)
    raw = [r for r in read(context_dir/'candidate_context.jsonl') if r['symbol'] == symbol]
    rows = [{**r, 'research_period': 'historical_test_folds'} for r in raw
            if 'delta_without_result' in r['research_gates']]
    signals = read(Path(source['observation_dir'])/'pre_ny/signals.jsonl')
    extension = None
    if august_obs is not None:
        if symbol != 'ETHUSDC' or august_cache is None: raise ValueError('ETH extension needs observation and minute cache')
        study = Path(manifest['source_study_manifest']).parent
        extra, new_signals, extension = extend_eth(source, august_obs, august_cache, study)
        rows.extend(extra)
        signals.extend(new_signals)
    if not rows: raise ValueError('No historical test-fold candidates')
    indexed = {s['sample_id']: s for s in signals}
    if len(indexed) != len(signals): raise ValueError('Duplicate input signal IDs')
    if len({r['sample_id'] for r in rows}) != len(rows): raise ValueError('Duplicate candidate IDs')
    output.mkdir(parents=True, exist_ok=True)
    if (output/'manifest.json').exists():
        raise FileExistsError(f'Prepared output already exists; use a fresh output root: {output}')
    counts, decisions = {}, []
    for policy in POLICIES[symbol]:
        selected = []
        for row in sorted(rows, key=lambda r: r['session_day']):
            signal = indexed[row['sample_id']]
            if (signal['direction'] != row['direction'] or
                int(signal['feature_as_of_ms']) != int(row['feature_as_of_ms']) or
                int(row['feature_as_of_ms']) > int(signal['entry_eligible_timestamp_ms'])):
                raise ValueError('Signal/context version mismatch')
            accepted = keep(row, policy)
            decisions.append(dict(sample_id=row['sample_id'], session_day=row['session_day'],
                policy=policy, accepted=accepted, research_period=row['research_period']))
            if accepted:
                selected.append({**signal, 'entry_variant': policy, 'research_period': row['research_period']})
        write(output/'signals'/policy/'signals.jsonl', selected)
        counts[policy] = dict(total=len(selected), historical=sum(s['research_period']=='historical_test_folds' for s in selected),
                              eth_august=sum(s['research_period']=='eth_august_extension' for s in selected))
    write(output/'selection_decisions.jsonl', decisions)
    (output/'grid.json').write_text(json.dumps({'tp1_r': [1., 1.5, 2.], 'tp1_fraction': [.1, .2, .5]}, indent=2), encoding='utf-8')
    result = dict(symbol=symbol, start_date=min(r['session_day'] for r in rows), end_date='2026-08-30',
        entry_policies=list(POLICIES[symbol]), counts=counts, portfolios=len(POLICIES[symbol])*9,
        source_context_sha256=hashlib.sha256((context_dir/'candidate_context.jsonl').read_bytes()).hexdigest(),
        source_signal_sha256=hashlib.sha256((Path(source['observation_dir'])/'pre_ny/signals.jsonl').read_bytes()).hexdigest(),
        extension=extension, selection='pre-entry masks on original signals; no replacement entry',
        caveat='Historical test folds have already been inspected; not untouched validation.',
        execution='Shared replay engine; TP1 plus original-stop/time remainder, no trail or BE',
        future_not_executed='C1/C2/C2-union plus ONE of MA, EMA or ADX; looser thresholds; never stack MA+EMA+ADX.')
    (output/'manifest.json').write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    return result


def collect(root):
    from backtest_engine.results import calculate_shared_backtest_metrics
    combined, august = [], []
    for asset in sorted(root.iterdir()):
        bt = asset/'backtest'
        if not (bt/'complete.json').exists(): continue
        with (bt/'comparison.csv').open(encoding='utf-8') as f:
            combined.extend({'symbol': asset.name.upper(), **r} for r in csv.DictReader(f))
        if asset.name.lower() != 'ethusdc': continue
        complete = json.loads((bt/'complete.json').read_text())
        for label in complete['portfolios']:
            trades = read(bt/label/'trades.jsonl')
            selected = [t for t in trades if '2026-08-01' <= t['session_day'] <= '2026-08-30']
            if selected:
                initial = selected[0]['equity_before_entry']
            else:
                summary = json.loads((bt/label/'summary.json').read_text())
                initial = summary['metrics']['final_equity']
            metrics = calculate_shared_backtest_metrics(selected, initial, date(2026,8,1), date(2026,8,30))
            august.append(dict(symbol='ETHUSDC', label=label, period='2026-08 extension',
                               period_initial_equity=initial, **metrics))
    for name, rows in [('comparison_all_assets.csv', combined), ('eth_august_only.csv', august)]:
        if rows:
            with (root/name).open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
    return dict(complete_portfolios=len(combined), eth_august_portfolios=len(august))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol', choices=POLICIES)
    p.add_argument('--context-dir', type=Path)
    p.add_argument('--output-dir', type=Path)
    p.add_argument('--summarize-root', type=Path)
    p.add_argument('--eth-aug-observation', type=Path)
    p.add_argument('--eth-aug-minute-cache', type=Path)
    a = p.parse_args()
    if a.summarize_root:
        print(json.dumps(collect(a.summarize_root), indent=2))
        return
    if not a.symbol or not a.context_dir or not a.output_dir:
        p.error('Preparation requires symbol, context-dir and output-dir')
    print(json.dumps(prepare(a.symbol, a.context_dir, a.output_dir,
                            a.eth_aug_observation, a.eth_aug_minute_cache), indent=2))


if __name__ == '__main__': main()
