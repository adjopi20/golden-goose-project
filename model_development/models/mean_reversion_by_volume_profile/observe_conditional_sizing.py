"""v30: six user-specified conditional sizing cases on frozen LTC RR=1 memberships."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import previous_value_area_reversal as base

CASES = {1:('union', 'location'), 2:('intersection', 'location'),
         3:('intersection', 'atr_high'), 4:('union', 'atr_high'),
         5:('union', 'location_and_atr_high'), 6:('intersection', 'location_and_atr_high')}
MULTIPLIERS = (1.5, 2.)


def verified(path):
    audit = json.loads((path/'audit.json').read_text())
    assert audit['status'] == 'passed'
    assert json.loads((path/'VALIDATION.json').read_text())['status'] == 'passed'
    for name, digest in audit['outputs'].items():
        assert base.sha(path/name) == digest
    return json.loads((path/'config_snapshot.json').read_text())


def condition(data, name):
    if name == 'none':
        return pd.Series(False, index=data.index)
    if name == 'location':
        return data.location_selected
    if name == 'atr_high':
        return data.atr_high
    if name == 'location_and_atr_high':
        return data.location_selected & data.atr_high
    raise ValueError(name)


def simulate(data, flag, multiplier, cfg):
    d = data.copy().sort_values('entry_timestamp_ny').reset_index(drop=True)
    flag = pd.Series(flag.to_numpy(dtype=bool), index=data.index).reindex(data.sort_values('entry_timestamp_ny').index).to_numpy()
    distance = d.risk_price.to_numpy()/d.entry.to_numpy()
    target = cfg['account_risk_fraction']*np.where(flag, multiplier, 1.)
    normal = np.minimum(cfg['account_risk_fraction'], cfg['max_entry_notional_equity']*distance)
    actual = np.minimum(target, cfg['max_entry_notional_equity']*distance)
    returns = actual*d.net_r.to_numpy()
    curve = np.r_[1., np.cumprod(1+returns)]
    assert (curve > 0).all()
    dd = 1-curve/np.maximum.accumulate(curve)
    cash = curve[:-1]*returns
    before, after = curve[:-1], curve[1:]
    d['boost_condition'] = flag
    d['requested_multiplier'] = np.where(flag, multiplier, 1.)
    d['target_risk_fraction'], d['actual_risk_fraction'] = target, actual
    d['normal_risk_fraction'] = normal
    d['actual_size_multiple'] = actual/normal
    d['notional_equity'] = actual/distance
    d['cap_binding'] = actual < target-1e-15
    d['net_trade_return_fraction'] = returns
    d['equity_before'], d['equity_after'] = before, after
    d['net_cash_pnl'] = cash
    d['fee_cash'] = before*actual*(d.gross_r-d.net_r).to_numpy()
    d['capital_weighted_net_r'] = d.net_r*actual/cfg['account_risk_fraction']
    d['closed_drawdown_fraction'] = dd[1:]
    positives, negatives = cash[cash > 0].sum(), -cash[cash < 0].sum()
    result = dict(trades=len(d), boost_condition_trades=int(flag.sum()),
        actual_increased_trades=int((actual > normal+1e-15).sum()),
        capped_trades=int(d.cap_binding.sum()), boost_cap_limited_trades=int((flag & (actual < target-1e-15)).sum()),
        gross_wr_pct=100*d.gross_r.gt(0).mean(), net_wr_pct=100*d.net_r.gt(0).mean(),
        unweighted_mean_gross_r=d.gross_r.mean(), unweighted_mean_net_r=d.net_r.mean(),
        unweighted_total_net_r=d.net_r.sum(),
        capital_weighted_total_net_r=d.capital_weighted_net_r.sum(),
        capital_weighted_mean_net_r=d.capital_weighted_net_r.mean(),
        risk_weighted_mean_net_r=float(np.sum(actual*d.net_r.to_numpy())/actual.sum()) if len(d) else None,
        net_account_return_pct=100*(curve[-1]-1), max_closed_dd_pct=100*dd.max(),
        return_over_closed_dd=float((curve[-1]-1)/dd.max()) if dd.max() else None,
        profit_factor_net_cash=float(positives/negatives) if negatives else None,
        mean_actual_risk_pct=100*actual.mean() if len(d) else None,
        max_actual_risk_pct=100*actual.max() if len(d) else None,
        max_notional_equity=d.notional_equity.max(), total_fee_initial_equity_pct=100*d.fee_cash.sum())
    return d, result


def run(checkpoint, macro_run, out):
    cp, macro = verified(checkpoint), verified(macro_run)
    assert cp['config'] == macro['config']
    cfg = cp['config']
    parent = pd.read_parquet(checkpoint/'test_predictions.parquet')
    parent = parent.loc[parent.symbol.eq('LTCUSDC')]
    labels = pd.read_parquet(macro_run/'test_labels.parquet')
    labels = labels.loc[labels.feature.eq('atr30_pct') & labels.model.isin(('union', 'intersection'))].copy()
    assert labels.rr.eq(1.).all() and labels.regime.isin(('low', 'middle', 'high')).all()
    keys = ['symbol', 'session_date', 'entry_agg_trade_id']
    labels = labels.merge(parent[keys+['location_selected']], on=keys, validate='many_to_one')
    labels['atr_high'] = labels.regime.eq('high')
    assert labels.loc[labels.model.eq('intersection'), 'location_selected'].all()
    assert not labels.duplicated(['model', 'session_date']).any()
    schedules = pd.read_csv(checkpoint/'fold_metrics.csv')
    schedules = schedules.loc[schedules.symbol.eq('LTCUSDC') & schedules.policy.eq('base_all')]
    specs = [('baseline_union', 0, 'union', 'none', 1.), ('baseline_intersection', 0, 'intersection', 'none', 1.)]
    specs += [(f'case{case}_x{multiplier:g}', case, parent_model, name, multiplier)
        for case, (parent_model, name) in CASES.items() for multiplier in MULTIPLIERS]
    summaries, ledgers, folds, years = [], [], [], []
    for scenario, case, parent_model, name, multiplier in specs:
        source = labels.loc[labels.model.eq(parent_model)].copy()
        flags = condition(source, name)
        ledger, metrics = simulate(source, flags, multiplier, cfg)
        meta = dict(scenario=scenario, case=case, parent_model=parent_model, condition=name, multiplier=multiplier)
        summaries.append(dict(meta, **metrics))
        ledgers.append(ledger.assign(**meta))
        for f in schedules.itertuples():
            p = source.loc[source.fold.eq(f.fold)]
            _, r = simulate(p, condition(p, name), multiplier, cfg)
            folds.append(dict(meta, fold=int(f.fold), test_start=f.test_start,
                test_end_exclusive=f.test_end_exclusive, **r))
        for year, p in source.groupby(source.session_date.str[:4]):
            _, r = simulate(p, condition(p, name), multiplier, cfg)
            years.append(dict(meta, year=year, **r))
    summary = pd.DataFrame(summaries)
    trades = pd.concat(ledgers, ignore_index=True)
    for mult in MULTIPLIERS:
        a = trades.loc[trades.scenario.eq(f'case3_x{mult:g}')].set_index('entry_agg_trade_id')
        b = trades.loc[trades.scenario.eq(f'case6_x{mult:g}')].set_index('entry_agg_trade_id')
        pd.testing.assert_frame_equal(a[['actual_risk_fraction', 'equity_after']], b[['actual_risk_fraction', 'equity_after']])
    originals = pd.read_csv(checkpoint/'summary.csv')
    for model in ('union', 'intersection'):
        s = summary.loc[summary.scenario.eq('baseline_'+model)].iloc[0]
        p = originals.loc[originals.symbol.eq('LTCUSDC') & originals.policy.eq(model)].iloc[0]
        np.testing.assert_allclose([s.net_account_return_pct, s.max_closed_dd_pct, s.capital_weighted_total_net_r],
            [p.net_account_return_pct, p.net_account_max_dd_pct, p.net_capital_base_r], atol=1e-9)
    out.mkdir(parents=True, exist_ok=False)
    summary.to_csv(out/'summary.csv', index=False)
    pd.DataFrame(folds).to_csv(out/'fold_metrics.csv', index=False)
    pd.DataFrame(years).to_csv(out/'yearly_metrics.csv', index=False)
    trades.to_parquet(out/'sizing_ledger.parquet', index=False)
    labels.to_parquet(out/'source_membership.parquet', index=False)
    manifest = dict(rule_version='conditional_sizing_v30', assets=['LTCUSDC'], rr=1., config=cfg,
        checkpoint_run=str(checkpoint), macro_run=str(macro_run), cases=CASES, multipliers=MULTIPLIERS,
        source_audits={str(p/'audit.json'):base.sha(p/'audit.json') for p in (checkpoint, macro_run)},
        sizing='Target normal equity risk * conditional multiplier; preserve 5x notional cap',
        code_sha256=base.sha(__file__), profiles_built=0, raw_rows_read=0,
        dependencies={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
            (base, __import__('observe'), __import__('observe_four_profiles'), __import__('backtest_poc_rr'))})
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    outputs = {f.name:base.sha(f) for f in out.iterdir() if f.is_file()}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', scenarios=len(summary), outputs=outputs), indent=2), encoding='utf-8')
    print(summary[['scenario', 'trades', 'boost_condition_trades', 'net_account_return_pct',
        'max_closed_dd_pct', 'capital_weighted_total_net_r', 'risk_weighted_mean_net_r']].to_string(index=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint-run', type=Path, required=True)
    p.add_argument('--macro-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    run(args.checkpoint_run.resolve(), args.macro_run.resolve(), args.output.resolve())
