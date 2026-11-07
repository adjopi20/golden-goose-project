"""Independent dollar-account loop, masks, source joins and scenario identities for v30."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import observe_conditional_sizing as model


def independent_loop(part, multiplier, name, cfg, detailed=False):
    part = part.sort_values('entry_timestamp_ny')
    equity, peak, drawdown = 1., 1., 0.
    weighted, weighted_gross, risk_sum, wins, increased, capped, flag_count = 0., 0., 0., 0, 0, 0, 0
    last_exit = None
    for r in part.itertuples():
        entry_time = pd.Timestamp(r.entry_timestamp_ny)
        if last_exit is not None:
            assert last_exit < entry_time
        last_exit = pd.Timestamp(r.exit_timestamp_ny)
        flag = bool(r.location_selected) if name == 'location' else bool(r.atr_high) if name == 'atr_high' else bool(r.location_selected and r.atr_high) if name == 'location_and_atr_high' else False
        flag_count += int(flag)
        distance = abs(r.entry-r.sl)/r.entry
        target_fraction = cfg['account_risk_fraction']*(multiplier if flag else 1.)
        normal_fraction = min(cfg['account_risk_fraction'], cfg['max_entry_notional_equity']*distance)
        desired_units = equity*target_fraction/abs(r.entry-r.sl)
        units = min(desired_units, equity*cfg['max_entry_notional_equity']/r.entry)
        actual_risk = units*abs(r.entry-r.sl)/equity
        fees = units*(r.entry+r.exit_price)*cfg['fee_bps_per_side']*.0001
        gross_cash = units*r.side*(r.exit_price-r.entry)
        net_cash = gross_cash-fees
        if detailed:
            assert flag == r.boost_condition
            np.testing.assert_allclose([r.equity_before, r.actual_risk_fraction, r.notional_equity,
                r.net_cash_pnl, r.fee_cash], [equity, actual_risk, units*r.entry/equity, net_cash, fees], atol=1e-11)
        weighted += net_cash/equity/cfg['account_risk_fraction']
        weighted_gross += gross_cash/equity/cfg['account_risk_fraction']
        risk_sum += actual_risk
        wins += int(net_cash > 0)
        increased += int(actual_risk > normal_fraction+1e-15)
        capped += int(actual_risk < target_fraction-1e-15)
        equity += net_cash
        assert equity > 0
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak)
        if detailed:
            np.testing.assert_allclose(r.equity_after, equity, atol=1e-11)
    return dict(net_account_return_pct=100*(equity-1), max_closed_dd_pct=100*drawdown,
        capital_weighted_total_net_r=weighted,
        risk_weighted_mean_net_r=weighted*cfg['account_risk_fraction']/risk_sum if risk_sum else np.nan,
        actual_increased_trades=increased, capped_trades=capped, boost_condition_trades=flag_count)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    args = p.parse_args()
    out = args.run
    manifest = json.loads((out/'config_snapshot.json').read_text())
    audit = json.loads((out/'audit.json').read_text())
    assert model.base.sha(model.__file__) == manifest['code_sha256']
    for name, digest in audit['outputs'].items():
        assert model.base.sha(out/name) == digest
    cfg = manifest['config']
    data = pd.read_parquet(out/'sizing_ledger.parquet')
    source = pd.read_parquet(out/'source_membership.parquet')
    checkpoint = pd.read_parquet(Path(manifest['checkpoint_run'])/'test_predictions.parquet')
    checkpoint = checkpoint.loc[checkpoint.symbol.eq('LTCUSDC')]
    assert data.symbol.eq('LTCUSDC').all() and data.rr.eq(1.).all()
    assert not data.duplicated(['scenario', 'session_date']).any()
    assert source.loc[source.model.eq('intersection'), 'location_selected'].all()
    original_macro = pd.read_parquet(Path(manifest['macro_run'])/'test_labels.parquet')
    original_macro = original_macro.loc[original_macro.feature.eq('atr30_pct') & original_macro.model.isin(('union', 'intersection'))]
    for parent, group in source.groupby('model'):
        expected = checkpoint.loc[checkpoint[parent+'_selected']]
        assert set(group.entry_agg_trade_id) == set(expected.entry_agg_trade_id)
        joined = group.merge(checkpoint[['entry_agg_trade_id', 'location_selected']], on='entry_agg_trade_id',
            suffixes=('', '_source'), validate='many_to_one')
        assert joined.location_selected.eq(joined.location_selected_source).all()
        before = original_macro.loc[original_macro.model.eq(parent)].set_index('entry_agg_trade_id')
        after = group.set_index('entry_agg_trade_id')
        pd.testing.assert_series_equal(before.regime.sort_index(), after.regime.sort_index())
    checked = 0
    for filename in ('summary.csv', 'fold_metrics.csv', 'yearly_metrics.csv'):
        table = pd.read_csv(out/filename)
        for r in table.itertuples():
            part = data.loc[data.scenario.eq(r.scenario)]
            parent = source.loc[source.model.eq(r.parent_model)]
            if hasattr(r, 'fold'):
                part = part.loc[part.fold.eq(r.fold)]
            if hasattr(r, 'year'):
                part = part.loc[part.session_date.str[:4].eq(str(r.year))]
            assert len(part) == r.trades
            computed = independent_loop(part, r.multiplier, r.condition, cfg, detailed=filename == 'summary.csv')
            for field, value in computed.items():
                np.testing.assert_allclose(getattr(r, field), value, atol=1e-9, equal_nan=True)
            checked += 1
            if filename == 'summary.csv':
                assert set(part.entry_agg_trade_id) == set(parent.entry_agg_trade_id)
                fields = ['entry', 'exit_price', 'sl', 'poc', 'gross_r', 'net_r', 'status', 'entry_timestamp_ny', 'exit_timestamp_ny']
                pd.testing.assert_frame_equal(part.set_index('entry_agg_trade_id')[fields].sort_index(),
                    parent.set_index('entry_agg_trade_id')[fields].sort_index())
                np.testing.assert_allclose([r.gross_wr_pct, r.net_wr_pct, r.unweighted_mean_gross_r,
                    r.unweighted_mean_net_r], [100*parent.gross_r.gt(0).mean(), 100*parent.net_r.gt(0).mean(),
                    parent.gross_r.mean(), parent.net_r.mean()])
    for mult in model.MULTIPLIERS:
        a = data.loc[data.scenario.eq(f'case3_x{mult:g}')].sort_values('entry_agg_trade_id')
        b = data.loc[data.scenario.eq(f'case6_x{mult:g}')].sort_values('entry_agg_trade_id')
        np.testing.assert_array_equal(a.entry_agg_trade_id, b.entry_agg_trade_id)
        np.testing.assert_allclose(a.actual_risk_fraction, b.actual_risk_fraction)
        np.testing.assert_allclose(a.equity_after, b.equity_after)
        uniform = data.loc[data.scenario.eq(f'case2_x{mult:g}')]
        assert uniform.boost_condition.all()
        np.testing.assert_allclose(uniform.actual_risk_fraction, cfg['account_risk_fraction']*mult)
    result = dict(status='passed', scenarios_reconciled=14, account_trade_steps_verified=len(data),
        metric_rows_checked=checked, entries_and_outcomes_unchanged=True, cap_preserved=True,
        case2_uniform_and_case6_equals_case3_verified=True, validator_sha256=model.base.sha(__file__))
    (out/'VALIDATION.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
