"""Independent single-feature sizing experiments on unchanged execution ledgers."""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from model_development.phase_3.ledger_io import config_load, sources, ledger_load, output_begin, write_csv, write_json, receipt
from model_development.phase_3.volatility_sizing import exposure, matched_risk, simulate, sha256, TZ


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def feature_index(paths):
    lookup = {}
    for path in paths:
        for row in read_rows(path):
            key = row['sample_id']
            if key in lookup:
                raise ValueError(f'Duplicate context ID: {key}')
            lookup[key] = row
    return lookup


def attach_features(data, signals, context, symbol):
    lookup = {s['sample_id']: s for s in signals}
    if len(lookup) != len(signals):
        raise ValueError('Duplicate signal IDs')
    out = []
    for trade in data['rows']:
        sid = trade['sample_id']
        if sid not in lookup or sid not in context:
            raise ValueError(f'Missing signal/context for {sid}; do not silently omit a trade')
        s, c = lookup[sid], context[sid]
        asof = int(c['feature_as_of_ms'])
        if not (c['symbol'] == symbol == s['symbol'] and
                c['direction'] == s['direction'] == trade['direction'] and
                c['session_day'] == s['session_day'] == trade['session_day'] and
                asof == int(s['feature_as_of_ms']) <= int(s['entry_eligible_timestamp_ms']) <= int(trade['entry_timestamp_ms'])):
            raise ValueError(f'Context/signal/fill mismatch or lookahead: {sid}')
        trend = c['trend_15m']
        end = trend.get('end_ms')
        if end is not None and (int(end) > asof or int(end) != asof // 900000 * 900000):
            raise ValueError(f'Stale/future trend bar: {sid}')
        sign = 1 if s['direction'] == 'long' else -1
        if s['direction'] not in ('long', 'short'):
            raise ValueError('Unknown direction')
        def finite(key):
            value = trend.get(key)
            return float(value) if end is not None and value is not None and math.isfinite(float(value)) else None
        ema, adx = finite('price_vs_ema200_pct'), finite('adx14')
        if adx is not None and not 0 <= adx <= 100:
            raise ValueError(f'Invalid ADX: {sid}')
        out.append(dict(sample_id=sid, feature_as_of_ms=asof, bar_end_ms=end,
                        ema_directional_gap_pct=sign*ema if ema is not None else None,
                        adx14=adx))
    return out


def multipliers(features, family, threshold, low_multiplier):
    key = {'ema200': 'ema_directional_gap_pct', 'adx': 'adx14'}[family]
    values = [f[key] for f in features]
    # Missing indicator => baseline size, explicitly logged; never rejects a trade.
    return np.array([1. if v is None or v >= threshold else low_multiplier for v in values]), values


def walk_forward(data, features, family, threshold, risk, leverage, low_multiplier, min_days, min_trades):
    """No threshold selection. Monthly TRAIN-only fixed risk-matched control."""
    rows = data['rows']
    mult, values = multipliers(features, family, threshold, low_multiplier)
    sized, folds = [], []
    first = pd.Timestamp(data['start']).date()
    for month in sorted({r['session_day'][:7] for r in rows}):
        cutoff = pd.Timestamp(month+'-01', tz=TZ)
        if (cutoff.date()-first).days < min_days:
            continue
        cutoff_ms = int(cutoff.timestamp()*1000)
        train = [i for i, r in enumerate(rows) if r['exit_timestamp_ms'] < cutoff_ms]
        if len(train) < min_trades:
            continue
        d = data['distance'][train]
        target = float(np.mean(exposure(d, risk*mult[train], leverage)*d))
        fixed = matched_risk(d, target, risk, leverage)
        folds.append(dict(test_month=month, calibration_cutoff_ms=cutoff_ms,
                          train_trades=len(train), fixed_matched_risk=fixed,
                          train_challenger_mean_risk=target,
                          train_supported=sum(values[i] is not None and values[i] >= threshold for i in train),
                          train_feature_missing=sum(values[i] is None for i in train),
                          latest_train_exit_ms=max(rows[i]['exit_timestamp_ms'] for i in train)))
        for i, r in enumerate(rows):
            if r['session_day'][:7] != month:
                continue
            distance = data['distance'][i]
            sized.append(dict(index=i, fold=month, sigma=None, target=None,
                fallback=values[i] is None,
                weights=dict(fixed_base=min(risk/distance, leverage),
                             indicator_sizing=min(risk*mult[i]/distance, leverage),
                             fixed_train_matched=min(fixed/distance, leverage)),
                indicator_value=values[i], requested_multiplier=float(mult[i]),
                supported=values[i] >= threshold if values[i] is not None else None))
    return sized, folds


def load_inputs(config_path):
    cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))
    root = (Path(config_path).resolve().parent/cfg['path_root']).resolve()
    base, base_root = config_load(root/cfg['ledger_config'])
    v = cfg['indicator_sizing']
    if cfg.get('schema_version') != 1 or not 0 < v['low_multiplier'] < 1:
        raise ValueError('Invalid schema or low multiplier')
    if v['min_train_days'] < 1 or v['min_train_trades'] < 2:
        raise ValueError('Invalid calibration requirements')
    if not 0 < base['risk_cap'] < 1 or not all(math.isfinite(base[k]) and base[k] > 0 for k in ('initial_equity', 'max_leverage')):
        raise ValueError('Invalid base account settings')
    for family, thresholds in v['thresholds'].items():
        if family not in ('ema200', 'adx') or not thresholds or len(set(thresholds)) != len(thresholds):
            raise ValueError('Only separate EMA200 or ADX threshold lists are supported')
        if any(not math.isfinite(x) or x < 0 or (family == 'adx' and x > 100) for x in thresholds):
            raise ValueError('Invalid threshold')
    paths = [(root/p).resolve() for p in cfg['context_files']]
    context = feature_index(paths)
    loaded, provenance = [], [dict(context_path=str(p), sha256=sha256(p)) for p in paths]
    for name, path, spec in sources(base, base_root):
        data = ledger_load(path, spec)
        signal_path = (base_root/spec['ledger_root']).parent/'prepared/signals'/spec['where']['source_group']/'signals.jsonl'
        features = attach_features(data, read_rows(signal_path), context, spec['name'])
        loaded.append((name, data, features))
        provenance.append(dict(dataset=name, ledger_path=str(path), ledger_sha256=data['sha256'],
                               signal_path=str(signal_path), signal_sha256=sha256(signal_path)))
        print(f'{name}: {len(features)} trades, causal context validated', flush=True)
    return cfg, base, loaded, provenance


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--output-dir')
    p.add_argument('--family', choices=('ema200', 'adx', 'all'), default='all')
    p.add_argument('--check-inputs', action='store_true')
    args = p.parse_args()
    cfg, base, loaded, provenance = load_inputs(args.config)
    if args.check_inputs:
        print('Checks complete; no experiment executed.')
        return
    if not args.output_dir:
        p.error('--output-dir is required')
    out = output_begin(args.output_dir)
    v = cfg['indicator_sizing']
    summaries, ledgers, months, folds, features_out, groups, excluded = [], [], [], [], [], [], []
    for name, data, features in loaded:
        features_out.extend(dict(dataset=name, **f) for f in features)
        for family, thresholds in v['thresholds'].items():
            if args.family not in ('all', family):
                continue
            for threshold in thresholds:
                tag = dict(dataset=name, family=family, threshold=threshold)
                sized, calibration = walk_forward(data, features, family, threshold,
                    base['risk_cap'], base['max_leverage'], v['low_multiplier'],
                    v['min_train_days'], v['min_train_trades'])
                if not sized:
                    raise ValueError(f'Insufficient calibration history: {name}')
                folds.extend(dict(**tag, **f) for f in calibration)
                used = {s['index'] for s in sized}
                excluded.extend(dict(**tag, sample_id=r['sample_id'], reason='initial_calibration')
                                for i, r in enumerate(data['rows']) if i not in used)
                for label, supported in [('supported', True), ('not_supported', False), ('missing', None)]:
                    indices = [s['index'] for s in sized if s['supported'] is supported]
                    r = data['r'][indices]
                    groups.append(dict(**tag, group=label, trades=len(r),
                        net_wr=float(np.mean(r > 0)) if len(r) else None,
                        expectancy_net_r=float(np.mean(r)) if len(r) else None))
                for policy in ('fixed_base', 'indicator_sizing', 'fixed_train_matched'):
                    stat, ledger, monthly = simulate(data, sized, policy, base['initial_equity'])
                    summary_tag = dict(**tag, policy=policy)
                    summaries.append(dict(**summary_tag, **stat))
                    for row, decision in zip(ledger, sized):
                        # Keep generic replay accounting; expose relevant feature instead of vol fields.
                        row.pop('sigma_24h'); row.pop('target_daily_vol')
                        ledgers.append(dict(**summary_tag, **row,
                            indicator_value=decision['indicator_value'], supported=decision['supported'],
                            requested_multiplier=decision['requested_multiplier'],
                            feature_as_of_ms=features[decision['index']]['feature_as_of_ms']))
                    months.extend(dict(**summary_tag, **row) for row in monthly)
                print(f'{name}: {family} >= {threshold:g} complete', flush=True)
    for file, rows in [('summary', summaries), ('sized_trades', ledgers), ('monthly', months),
                       ('calibration_folds', folds), ('feature_snapshots', features_out),
                       ('condition_groups', groups), ('training_exclusions', excluded)]:
        if rows:
            write_csv(out/(file+'.csv'), rows)
    rec = receipt(cfg, provenance, 'indicator_sizing', args)
    rec['ledger_config'] = base
    rec['method'] = 'Independent single-feature sizing; predefined thresholds; baseline cap vs half-risk; TRAIN-only monthly risk-matched control; fixed trade set and exits.'
    rec['limitations'] += ' No threshold winner is selected. Prior-inspected historical walk-forward is not fresh OOS. ADX has no direction. 15m EMA200 is local context, not macro regime. Missing feature uses baseline sizing. WR/net-R expectancy are invariant to positive sizing. Equity compounds net R with linear costs; not a new execution replay.'
    write_json(out/'manifest.json', rec)
    write_json(out/'complete.json', dict(datasets=len(loaded), summary_rows=len(summaries), trade_rows=len(ledgers)))
    print(f'Complete: {out}')


if __name__ == '__main__':
    main()
