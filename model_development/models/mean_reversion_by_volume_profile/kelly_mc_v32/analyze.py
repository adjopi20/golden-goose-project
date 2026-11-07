"""Compounding empirical Kelly and block Monte Carlo from audited v31 trades only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from scipy.optimize import brentq

INITIAL = 10_000.
SEED = 20261025
FRACTIONS = (.1, .25, .5, 1.)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ny_dates(values):
    return pd.to_datetime(values, utc=True, format='ISO8601').dt.tz_convert('America/New_York').dt.tz_localize(None)


def log_growth(f, r, cap, boost):
    returns = np.minimum(f*boost, cap)*r
    return float(np.log1p(returns).mean()) if (returns > -1).all() else -np.inf


def kelly(r, cap, boost):
    """Global optimum: each interval between cap knots is concave, whole curve need not be."""
    r, cap, boost = (np.asarray(x, dtype=float) for x in (r, cap, boost))
    if len(r) < 50:
        raise ValueError("Kelly calibration requires at least 50 trades")
    if not all(np.isfinite(a).all() for a in (r, cap, boost)) or (cap <= 0).any() or (boost <= 0).any():
        raise ValueError("Invalid Kelly inputs")
    upper = float(np.max(cap/boost))  # above this all trades are saturated
    fatal = (r < 0) & (1+cap*r <= 0)
    if fatal.any():
        upper = min(upper, float(np.min(-1/(r[fatal]*boost[fatal])))*(1-1e-10))
    knots = np.unique(np.r_[0., (cap/boost)[cap/boost < upper], upper])
    candidates = list(knots)
    for lo, hi in zip(knots[:-1], knots[1:]):
        active = ((lo+hi)/2)*boost < cap
        rb = r[active]*boost[active]
        derivative = lambda x: float(np.sum(rb/(1+x*rb)))
        if derivative(lo) > 0 and derivative(hi) < 0:
            candidates.append(float(brentq(derivative, lo, hi, xtol=1e-14)))
    ordered = sorted(candidates)
    scores = np.array([log_growth(f, r, cap, boost) for f in ordered])
    best = float(scores.max())
    f = float(ordered[int(np.flatnonzero(scores >= best-1e-14)[0])])
    return dict(target_fraction=f, mean_log_growth=log_growth(f, r, cap, boost),
                search_upper=upper, all_caps_saturated=bool((f*boost >= cap-1e-12).all()))


def indices(paths, days, block, seed):
    """Circular stationary bootstrap: geometric block lengths with specified mean."""
    rng = np.random.default_rng(seed)
    result = np.empty((paths, days), dtype=np.int32)
    result[:, 0] = rng.integers(days, size=paths)
    for j in range(1, days):
        restart = rng.random(paths) < 1/block
        result[:, j] = (result[:, j-1]+1)%days
        result[restart, j] = rng.integers(days, size=int(restart.sum()))
    return result


def monte_carlo(daily_returns, draws):
    log_paths = np.empty((len(draws), draws.shape[1]+1))
    log_paths[:, 0] = 0.
    np.cumsum(np.log1p(daily_returns)[draws], axis=1, out=log_paths[:, 1:])
    peaks = np.maximum.accumulate(log_paths, axis=1)
    max_dd = 100*(-np.expm1(np.min(log_paths-peaks, axis=1)))
    terminal = 100*np.expm1(log_paths[:, -1])
    summary = dict(paths=len(draws), probability_ending_loss_pct=100*float((terminal < 0).mean()),
        probability_dd_ge_20_pct=100*float((max_dd >= 20).mean()),
        probability_dd_ge_50_pct=100*float((max_dd >= 50).mean()),
        probability_equity_below_half_initial_pct=100*float((log_paths.min(axis=1) <= np.log(.5)).mean()))
    for p in (5, 50, 95):
        summary[f"return_p{p}_pct"] = float(np.percentile(terminal, p))
        summary[f"max_dd_p{p}_pct"] = float(np.percentile(max_dd, p))
    band = INITIAL*np.exp(np.percentile(log_paths, (5, 50, 95), axis=0)).T
    return summary, band


def bootstrap_fit(r, cap, boost, positions, draws, fit):
    # A grid is used only for estimation uncertainty; the main Kelly optimum
    # above is solved interval by interval, including every cap kink.
    grid = np.unique(np.r_[np.linspace(0, fit['search_upper'], 1001), cap/boost, fit['target_fraction']])
    grid = grid[grid <= fit['search_upper']]
    logs = np.log1p(np.minimum(grid[:, None]*boost, cap)*r).T
    counts = np.array([np.bincount(d, minlength=draws.shape[1])[positions] for d in draws], dtype=float)
    scores = counts@logs
    best = scores.argmax(axis=1)
    values = grid[best]
    return dict(repeats=len(draws), method="stationary calendar bootstrap, mean block 30 days; grid-only uncertainty",
                risk_target_p025_pct=float(100*np.quantile(values, .025)),
                risk_target_median_pct=float(100*np.median(values)),
                risk_target_p975_pct=float(100*np.quantile(values, .975)),
                zero_kelly_pct=float(100*(values == 0).mean()))


def prepare(source):
    audit = json.loads((source/'audit.json').read_text())
    if audit.get('status') != 'passed':
        raise ValueError('Source run is incomplete')
    for name, digest in audit['outputs'].items():
        if sha(source/name) != digest:
            raise ValueError(f'Source output hash mismatch: {name}')
    snapshot = json.loads((source/'config_snapshot.json').read_text())
    data = pd.read_parquet(source/'trades.parquet')
    data = data.loc[data.symbol.eq('LTCUSDC')].copy().sort_values('entry_ms').reset_index(drop=True)
    sessions = pd.read_csv(source/'sessions.csv')
    sessions = sessions.loc[sessions.symbol.eq('LTCUSDC')]
    if data.duplicated('session_date').any() or not np.isfinite(data.net_r).all():
        raise ValueError('Invalid one-trade-per-session source')
    np.testing.assert_allclose(data.net_r, data.gross_r-data.fee_r)
    days = pd.date_range(sessions.session_date.min(), sessions.session_date.max(), freq='D')
    return data, days, snapshot


def ledger(data, effective, core):
    d = data.copy().reset_index(drop=True)
    if (1+effective*d.net_r.to_numpy() <= 0).any():
        raise ValueError('Sizing bankrupts the account on an observed trade')
    ret = effective*d.net_r.to_numpy()
    curve = np.r_[1., np.cumprod(1+ret)]
    d['actual_risk_fraction'] = effective
    d['normal_risk_fraction'] = np.minimum(.005, 5*d.risk_price/d.entry)
    d['target_risk_fraction'] = effective
    d['boost_condition'] = False  # historical summary does not relabel Kelly as a signal boost
    d['cap_binding'] = np.isclose(effective, 5*d.risk_price/d.entry)
    d['notional_equity'] = effective/(d.risk_price/d.entry)
    d['net_trade_return_fraction'] = ret
    d['capital_weighted_net_r'] = effective*d.net_r/.005
    d['equity_before'], d['equity_after'] = curve[:-1], curve[1:]
    d['net_cash_pnl'] = curve[:-1]*ret
    d['fee_cash'] = curve[:-1]*effective*d.fee_r
    core.validate_ledger(d)
    return d, core.metrics(d)


def calendar_returns(data, effective, days):
    positions = days.get_indexer(pd.to_datetime(data.session_date))
    if (positions < 0).any():
        raise ValueError('Trade outside calendar')
    result = np.zeros(len(days))
    result[positions] = effective*data.net_r.to_numpy()
    return result, positions


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--source-run', type=Path, default=Path('model_development/models/mean_reversion_by_volume_profile/runs/frozen_sizing_v31_full'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--paths', type=int, default=5000)
    p.add_argument('--bootstrap-fits', type=int, default=1000)
    args = p.parse_args(argv)
    if args.paths < 100 or args.bootstrap_fits < 100:
        raise ValueError('At least 100 paths/fit-bootstrap draws required')
    root, out, source = args.root.resolve(), (args.root/args.output).resolve(), (args.root/args.source_run).resolve()
    sys.path.insert(0, str(root/'model_development/models/mean_reversion_by_volume_profile/frozen_sizing_v31'))
    import backtest as core
    source_audit_sha, core_sha = sha(source/'audit.json'), sha(core.__file__)
    data, days, snapshot = prepare(source)
    if sha(core.__file__) != snapshot['code_sha256']:
        raise ValueError('Installed source execution code differs from audited source')
    out.mkdir(parents=True, exist_ok=False)
    (out/'audit.json').write_text(json.dumps(dict(status='running')), encoding='utf-8')
    fits, histories, ledgers, paths, period_rows = [], [], [], [], []
    raw_end_ms = max(s['last_ms'] for s in snapshot['sources']['LTCUSDC'])
    streams, primary, fit_inputs = {}, {}, {}
    point_cache = {}
    expected = pd.read_csv(source/'summary.csv')
    for name, parent, condition, mult in core.scenarios():
        mask = data.distance | data.location if parent == 'union' else data.distance & data.location
        d = data.loc[mask].reset_index(drop=True)
        flag = (d.location if condition == 'location' else d.atr_high if condition == 'atr_high' else
                d.location & d.atr_high if condition == 'location_and_atr' else pd.Series(False, index=d.index))
        boost = np.where(flag, mult, 1.)
        scale = boost.min()
        normalized = boost/scale
        cap, r = 5*(d.risk_price/d.entry).to_numpy(), d.net_r.to_numpy()
        key = hashlib.sha256(np.column_stack([r, cap, normalized]).tobytes()).hexdigest()
        if key not in point_cache:
            point_cache[key] = kelly(r, cap, normalized)
        fit = point_cache[key]
        full = np.minimum(fit['target_fraction']*normalized, cap)
        fits.append(dict(model=name, parent=parent, trades=len(d),
            normal_target_risk_pct=100*fit['target_fraction']/scale,
            full_actual_risk_min_pct=100*full.min(), full_actual_risk_median_pct=100*np.median(full),
            full_actual_risk_max_pct=100*full.max(), mean_log_growth=fit['mean_log_growth'],
            max_notional_equity=float((full/(d.risk_price/d.entry)).max()),
            all_caps_saturated=fit['all_caps_saturated']))
        policies = [('risk_0.5pct', np.minimum(.005*boost, cap)), ('risk_1pct', np.minimum(.01*boost, cap))]
        policies += [(f'kelly_{q:g}', q*full) for q in FRACTIONS]
        is_primary = name.startswith('baseline_')
        for policy, effective in policies:
            trade_ledger, result = ledger(d, effective, core)
            meta = dict(model=name, parent=parent, policy=policy)
            histories.append(dict(meta, **result, median_actual_risk_pct=100*float(np.median(effective))))
            ledgers.append(trade_ledger.assign(**meta))
            daily, positions = calendar_returns(d, effective, days)
            trace = pd.DataFrame(dict(timestamp_ny=[core.iso(core.bounds(days[0].date())[1])]+
                [core.iso(v) for v in trade_ledger.exit_ms]+[core.iso(raw_end_ms)],
                equity=INITIAL*np.r_[1., trade_ledger.equity_after.to_numpy(), trade_ledger.equity_after.iloc[-1]],
                event=['initial']+['trade_close']*len(trade_ledger)+['data_end'], **meta))
            paths.append(trace)
            for year in sorted(set(days.year)):
                part = trade_ledger.loc[trade_ledger.session_date.str.startswith(str(year))]
                period_rows.append(dict(meta, year=year, **core.metrics(part)))
            if is_primary or policy == 'kelly_1':
                streams[(name, policy)] = daily
            if is_primary:
                primary[(name, policy)] = trace
                fit_inputs[name] = (d, r, cap, normalized, positions, fit)
            if policy == 'risk_0.5pct':
                reference = expected.loc[expected.symbol.eq('LTCUSDC') & expected.scenario.eq(name)].iloc[0]
                np.testing.assert_allclose([result['net_account_return_pct'], result['max_closed_dd_pct']],
                                          [reference.net_account_return_pct, reference.max_closed_dd_pct], atol=1e-10)
    print('Kelly fits and original 14 baseline accounts reconciled', flush=True)
    mc_rows, bands = [], []
    for block in (30, 7):
        draws = indices(args.paths, len(days), block, SEED+block)
        cache = {}
        for (name, policy), daily in streams.items():
            key = hashlib.sha256(daily.tobytes()).hexdigest()
            if key not in cache:
                cache[key] = monte_carlo(daily, draws)
            result, band = cache[key]
            mc_rows.append(dict(model=name, policy=policy, mean_block_days=block, **result))
            if name == 'baseline_intersection' and policy in ('risk_0.5pct', 'kelly_0.25', 'kelly_1') and block == 30:
                bands.append(pd.DataFrame(dict(day=np.arange(len(band)), p5=band[:, 0], p50=band[:, 1], p95=band[:, 2], model=name, policy=policy)))
        print(f'Monte Carlo: {args.paths:,} paths, mean calendar block {block} days complete', flush=True)
    bootstrap_rows, temporal = [], []
    fit_draws = indices(args.bootstrap_fits, len(days), 30, SEED+300)
    for name, (d, r, cap, boost, positions, fit) in fit_inputs.items():
        bootstrap_rows.append(dict(model=name, **bootstrap_fit(r, cap, boost, positions, fit_draws, fit)))
        cutoff = core.stamp(pd.Timestamp('2026-01-01').tz_localize(core.NY))
        train = d.loc[d.session_date.lt('2026-01-01') & d.exit_ms.lt(cutoff)].copy()
        test = d.loc[d.session_date.ge('2026-01-01')].copy()
        training = kelly(train.net_r.to_numpy(), 5*(train.risk_price/train.entry).to_numpy(), np.ones(len(train)))
        for q in (.25, .5, 1.):
            effective = q*np.minimum(training['target_fraction'], 5*(test.risk_price/test.entry).to_numpy())
            _, result = ledger(test, effective, core)
            temporal.append(dict(model=name, train_trades=len(train), test_trades=len(test),
                                 fit_end_exclusive='2026-01-01', fraction=q,
                                 trained_target_risk_pct=100*training['target_fraction'], **result))
    # Permutations keep the complete set of returns: the final product is invariant.
    perm_rng = np.random.default_rng(SEED+999)
    d, r, cap, boost, _, fit = fit_inputs['baseline_intersection']
    trade_returns = np.minimum(fit['target_fraction']*boost, cap)*r
    expected_final = float(np.prod(1+trade_returns))
    permuted = np.array([np.prod(1+perm_rng.permutation(trade_returns)) for _ in range(1000)])
    np.testing.assert_allclose(permuted, expected_final, rtol=1e-12)
    hist, fit_table, mc = pd.DataFrame(histories), pd.DataFrame(fits), pd.DataFrame(mc_rows)
    hist.to_csv(out/'historical_compounding.csv', index=False)
    fit_table.to_csv(out/'kelly_fits.csv', index=False)
    mc.to_csv(out/'monte_carlo.csv', index=False)
    pd.DataFrame(bootstrap_rows).to_csv(out/'kelly_uncertainty.csv', index=False)
    pd.DataFrame(temporal).to_csv(out/'locked_2026_diagnostic.csv', index=False)
    pd.DataFrame(period_rows).to_csv(out/'yearly_compounding.csv', index=False)
    pd.concat(paths, ignore_index=True).to_csv(out/'equity_paths.csv', index=False)
    pd.concat(bands, ignore_index=True).to_csv(out/'monte_carlo_bands.csv', index=False)
    pd.concat(ledgers, ignore_index=True).to_parquet(out/'compounding_ledger.parquet', index=False)
    draw_plots(out, days, primary, bands)
    report(out, hist, fit_table, mc, bootstrap_rows, temporal, days, args.paths)
    if sha(source/'audit.json') != source_audit_sha or sha(core.__file__) != core_sha:
        raise ValueError('Source audit or execution dependency changed during analysis')
    for name, digest in json.loads((source/'audit.json').read_text())['outputs'].items():
        if sha(source/name) != digest:
            raise ValueError(f'Source artifact changed during analysis: {name}')
    manifest = dict(version='kelly_mc_v32', initial_equity=INITIAL, source=str(source), source_audit_sha256=source_audit_sha,
        code_sha256=sha(__file__), core_sha256=sha(core.__file__), paths=args.paths, bootstrap_fits=args.bootstrap_fits, seed=SEED,
        calendar_start=str(days.min().date()), calendar_end=str(days.max().date()),
        formula='E_next=E*(1+effective_risk*net_R); full=min(f_K*conditional_boost,5*risk_price/entry); fractional=q*full',
        fractional_definition='Scale the complete already-capped full-Kelly position, not only its uncapped risk target',
        calibration='full-history empirical plug-in, in-sample; separate retrospective locked-2026 diagnostic, 50-trade minimum',
        temporal_exception='For sparse intersection, use expanding history through 2025 instead of 8 months, to meet 50-trade minimum; diagnostic only, not a replacement strategy protocol',
        monte_carlo='stationary circular calendar bootstrap, mean block 30 days; 7-day sensitivity; frozen sizing/outcome tuples, no price/profile/ATR rebuild',
        limits='conditional on estimated historical distribution; no proof of stationarity or new OOS edge; no spread/funding/intratrade DD',
        environment=dict(python=sys.version, numpy=np.__version__, pandas=pd.__version__))
    (out/'config_snapshot.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    outputs = {f.name:sha(f) for f in out.iterdir() if f.is_file() and f.name != 'audit.json'}
    (out/'audit.json').write_text(json.dumps(dict(status='passed', original_accounts_reconciled=14,
        cash_accounting_rows_checked=sum(len(d) for d in ledgers), permutation_terminal_invariant=True,
        source_audit_unchanged=True, outputs=outputs), indent=2), encoding='utf-8')
    print(hist.loc[hist.model.eq('baseline_intersection'), ['model','policy','trades','net_account_return_pct','max_closed_dd_pct']].to_string(index=False), flush=True)
    print(f'Completed: {out}', flush=True)


def draw_plots(out, days, primary, bands):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    policy_colors = ['#111827', '#64748b', '#059669', '#2563eb', '#d97706', '#dc2626']
    labels = ['Risk 0.5%', 'Risk 1%', '0.1 Kelly', 'Quarter Kelly', 'Half Kelly', 'Full Kelly']
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), sharex=True, constrained_layout=True)
    for col, model in enumerate(('baseline_intersection', 'baseline_union')):
        for ((_, policy), trace), label, color in zip([(k,v) for k,v in primary.items() if k[0] == model], labels, policy_colors):
            x = ny_dates(trace.timestamp_ny)
            eq = trace.equity.to_numpy()
            axes[0,col].step(x, eq, where='post', label=label, color=color, linewidth=1.5)
            dd = 100*(1-eq/np.maximum.accumulate(eq))
            axes[1,col].step(x, dd, where='post', color=color, linewidth=1.2)
        axes[0,col].set_title(model.replace('baseline_', '').title()+' | historical in-sample Kelly')
        axes[0,col].set_ylabel('Equity (USDC); start 10,000')
        axes[1,col].set_ylabel('Closed-trade drawdown (%)')
        axes[0,col].legend(fontsize=8, ncol=2)
        axes[1,col].invert_yaxis()
        axes[1,col].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=6))
        axes[1,col].xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        axes[1,col].tick_params(axis='x', labelrotation=15)
        for row in (0,1):
            axes[row,col].grid(alpha=.2)
            axes[row,col].axvline(pd.Timestamp('2026-01-01'), color='#94a3b8', linestyle=':', linewidth=1)
    fig.suptitle('LTCUSDC compounding | 5x notional cap | fees 0.04% per side\nKelly calibrated on the full history; optimized paths are not OOS results', fontsize=12)
    fig.savefig(out/'equity_path.png', dpi=160)
    plt.close(fig)
    fig, axes = plt.subplots(1, 3, figsize=(15, 5), constrained_layout=True)
    for ax, frame, label in zip(axes, bands, ('Risk 0.5%', 'Quarter Kelly', 'Full Kelly')):
        ax.fill_between(frame.day, frame.p5, frame.p95, color='#60a5fa', alpha=.25, label='Pointwise 5th–95th percentile')
        ax.plot(frame.day, frame.p50, color='#1d4ed8', label='Median simulated equity')
        ax.axhline(INITIAL, color='#64748b', linestyle='--', linewidth=1)
        ax.set_title(label)
        ax.set_xlabel('Synthetic calendar day')
        ax.set_ylabel('Equity (USDC)')
        ax.grid(alpha=.2)
    axes[0].legend(fontsize=8)
    fig.suptitle('Intersection | stationary bootstrap, mean block 30 days | independent y scales\nFixed historical Kelly estimate; conditional empirical simulations, not a forecast', fontsize=12)
    fig.savefig(out/'monte_carlo_equity.png', dpi=160)
    plt.close(fig)


def report(out, hist, fits, mc, uncertainty, temporal, days, count):
    lines = ['# LTCUSDC: empirical Kelly and compounding Monte Carlo — v32', '',
        f'Calendar: {days.min().date()} through {days.max().date()}. Initial equity 10,000 USDC, illustrative only. '
        'Every position scales with current equity. Entry/exit geometry, fees and 5x notional cap come from the audited v31 run. No raw archives were replayed.', '',
        'Kelly maximizes mean log(1 + effective risk × net R), using all observed time exits and fees. '
        'The cap makes the whole objective potentially non-concave: every cap interval is searched, including kinks. '
        'Fractional Kelly scales the entire already-capped optimal position. The target is stop-risk/equity, not position-notional/equity.', '',
        '**Primary model was preselected as intersection; union and all six conditional structures are comparisons. '
        'Full-history fits and their chronological equity paths are in-sample optimized illustrations, not live-ready estimates or new OOS tests.**', '',
        '| Parent | Policy | Trades | Median actual risk | Return | Closed DD |', '|---|---|---:|---:|---:|---:|']
    for r in hist.loc[hist.model.str.startswith('baseline_')].to_dict('records'):
        lines.append(f"| {r['parent']} | {r['policy']} | {r['trades']} | {r['median_actual_risk_pct']:.3f}% | {r['net_account_return_pct']:+.2f}% | {r['max_closed_dd_pct']:.2f}% |")
    lines += ['', '## Kelly calibration', '', '| Model | Trades | Normal target risk | Full actual median risk | Full actual max risk |', '|---|---:|---:|---:|---:|']
    for r in fits.to_dict('records'):
        lines.append(f"| {r['model']} | {r['trades']} | {r['normal_target_risk_pct']:.3f}% | {r['full_actual_risk_median_pct']:.3f}% | {r['full_actual_risk_max_pct']:.3f}% |")
    lines += ['', 'When all_caps_saturated=True, the optimum reaches the imposed 5x notional ceiling on every observed trade. '
        'The listed normal target is the smallest budget that saturates all caps; larger targets give the same positions, so it is not a uniquely identified unconstrained Kelly fraction. '
        'For baseline intersection/union here, fractional Kelly is consequently constant notional leverage: 0.1/0.25/0.5/1 Kelly = 0.5/1.25/2.5/5 times current equity. '
        'Stop-risk percentages still vary with each entry-to-stop distance.', '',
        'Case 2 boosts every intersection entry. Re-optimizing its normal Kelly target cancels the constant 1.5/2 multiplier, so full allocations match baseline intersection. Cases 3 and 6 are identical.', '',
        '## Monte Carlo', '', f'{count:,} paths per policy, same {len(days)}-day horizon. Stationary circular bootstrap with mean 30-day blocks; 7-day sensitivity in monte_carlo.csv. '
        'No-trade/invalid-data dates stay flat. Historical sizing/outcome tuples remain attached, and profile/ATR are not rebuilt from resampled returns. '
        'Kelly is held fixed during these simulations. Permuting the same trades leaves terminal wealth unchanged; a separate 1,000-permutation assertion verifies this.', '',
        '| Policy, intersection | Return p5 | Return median | Return p95 | P(end below start) | DD p95 | P(DD >=50%) |', '|---|---:|---:|---:|---:|---:|---:|']
    for r in mc.loc[mc.model.eq('baseline_intersection') & mc.mean_block_days.eq(30)].to_dict('records'):
        lines.append(f"| {r['policy']} | {r['return_p5_pct']:+.2f}% | {r['return_p50_pct']:+.2f}% | {r['return_p95_pct']:+.2f}% | {r['probability_ending_loss_pct']:.2f}% | {r['max_dd_p95_pct']:.2f}% | {r['probability_dd_ge_50_pct']:.2f}% |")
    lines += ['', '## Kelly estimation uncertainty', '', 'Kelly is re-estimated on calendar-block resamples using a dense grid only for this diagnostic. This distribution does not adjust for the many previous strategy trials.', '']
    for r in uncertainty:
        lines.append(f"- {r['model']}: risk-target percentile 2.5/50/97.5 = {r['risk_target_p025_pct']:.2f}% / {r['risk_target_median_pct']:.2f}% / {r['risk_target_p975_pct']:.2f}%; zero Kelly {r['zero_kelly_pct']:.1f}%.")
    lines += ['', '## Retrospective locked-2026 sizing diagnostic', '',
        'Fit on all trades completed before NY midnight 2026-01-01; hold size fixed on 2026 entries. '
        'Expanding calibration is an explicit sparse-sample diagnostic exception to the default 8+2 protocol, to meet the 50-trade minimum. '
        'This does not replace v31 rules. The test history was already inspected in earlier research, so it is not a fresh holdout.', '',
        '| Parent | Train / 2026 trades | Fraction | Target from train | 2026 return | 2026 DD |', '|---|---:|---:|---:|---:|---:|']
    for r in temporal:
        lines.append(f"| {r['model']} | {r['train_trades']} / {r['test_trades']} | {r['fraction']:g} | {r['trained_target_risk_pct']:.2f}% | {r['net_account_return_pct']:+.2f}% | {r['max_closed_dd_pct']:.2f}% |")
    lines += ['', '## Files and limits', '',
        'equity_paths.csv records equity at actual trade-close timestamps, with initial/end-of-data endpoints; the figure uses steps at those closes. '
        'compounding_ledger.parquet contains the cash accounting. '
        'historical_compounding.csv and yearly_compounding.csv include every configuration and fraction. '
        'monte_carlo.csv includes conditional structures at full Kelly and the primary parents at all reported fractions. '
        'monte_carlo_bands.csv contains pointwise 5/50/95 bands, not simultaneous confidence bands.', '',
        'Bootstrap probabilities are conditional on the estimated historical distribution. Resampling cannot establish a stable edge, recover unobserved losing regimes, '
        'or undo repeated historical model selection. In the prior v31 run, full-history LTCUSDT failed; these USDC-only simulations do not overturn that finding. '
        'Full Kelly is a mathematical empirical optimum, not a risk recommendation. Spread, funding, latency, market impact, liquidation and intratrade DD are unmodeled.', '',
        'Primary references: [Kelly 1956](https://onlinelibrary.wiley.com/doi/abs/10.1002/j.1538-7305.1956.tb03809.x), '
        '[Politis–Romano stationary bootstrap](https://statistics.stanford.edu/technical-reports/stationary-bootstrap).']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
