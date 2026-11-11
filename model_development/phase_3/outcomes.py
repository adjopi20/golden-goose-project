"""Explicit Phase 3 outcome diagnostics; never launches observation or backtest."""
import argparse
from datetime import datetime,timezone
import json
import math
from pathlib import Path

import numpy as np

from model_development.phase_3.run_input import load_run,verify_source,digest,IDENTITY
from model_development.phase_3.kelly import kelly_summary
from model_development.phase_3.monte_carlo import (
    resample_paths, shuffle_trade_paths, shuffled_performance_bands, scenarios, stressed_r,
)


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task',nargs='+',choices=['kelly','monte-carlo'],required=True)
    p.add_argument('--run',type=Path,required=True,help='An explicitly selected completed Phase 1 or Phase 2 run')
    p.add_argument('--output',type=Path,required=True)
    for flag in ('model-id','instrument','configuration','submodel','portfolio'):
        p.add_argument('--'+flag)
    p.add_argument('--resamples',type=int,default=2000)
    p.add_argument('--simulations',type=int,default=5000)
    p.add_argument('--mc-method',choices=['calendar-bootstrap','trade-shuffle'],default='calendar-bootstrap',
                   help='Calendar block bootstrap or shuffle all closed trades without replacement')
    p.add_argument('--block-days',type=int,default=7)
    p.add_argument('--seed',type=int,default=20260927)
    p.add_argument('--scenario-config',type=Path,help='JSON containing scenarios using the existing stress fields')
    p.add_argument('--scenario',default='baseline',help='Named stress from scenario-config; default baseline')
    p.add_argument('--initial-equity',type=float)
    p.add_argument('--risk-fraction',type=float,help='Explicit conditional ledger-sizing study, not execution replay')
    p.add_argument('--max-leverage',type=float)
    return p


def scenario_arrays(data):
    """Adapt validated selected outcomes to the existing fixed-ledger stress kernel."""
    trades=data['trades'];remaining=[]
    gap=data.get('scenario',{}).get('stop_extra_r',0.)
    for trade in trades:
        reason=trade.get('exit_reason')
        trigger=trade.get('stop_trigger_timestamp_ms')
        if gap and not reason and trigger is None:
            raise ValueError('Stop-gap scenario requires recorded exit reason or stop trigger')
        stopped=trigger is not None or 'stop' in str(reason or '').lower()
        quantity=trade['quantity']
        rest=trade.get('remaining_exit_quantity')
        if gap and stopped and trade.get('tp1_filled') and rest is None:
            raise ValueError('Stopped partial trade requires remaining_exit_quantity')
        fraction=1. if rest is None else rest/quantity
        if not math.isfinite(fraction) or not 0<=fraction<=1+1e-8:
            raise ValueError('Invalid remaining quantity for scenario')
        remaining.append(fraction if stopped else 0.)
    return dict(r=np.array([t['net_R'] for t in trades]),
                fee_r=np.array([t['fees']/t['initial_risk'] for t in trades]),
                distance=np.array([abs(t['entry_price']-t['stop'])/t['entry_price'] for t in trades]),
                stop_remaining=np.array(remaining))


def monte_carlo(data,args):
    trades=data['trades']
    scenario=data.get('scenario',{'name':'baseline'})
    stressed=stressed_r(scenario_arrays(data),scenario)
    method=getattr(args,'mc_method','calendar-bootstrap')
    if method not in ('calendar-bootstrap','trade-shuffle'):
        raise ValueError('Unknown Monte Carlo method')
    if args.risk_fraction is not None:
        if args.initial_equity is None or args.max_leverage is None:
            raise ValueError('Conditional sizing requires explicit initial-equity and max-leverage')
        if (not all(math.isfinite(x) for x in (args.initial_equity,args.max_leverage,args.risk_fraction))
                or args.initial_equity<=0 or args.max_leverage<=0 or not 0<args.risk_fraction<1):
            raise ValueError('Invalid conditional sizing values')
        initial=args.initial_equity
        distance=np.array([abs(r['entry_price']-r['stop'])/r['entry_price'] for r in trades])
        returns=np.minimum(args.risk_fraction,args.max_leverage*distance)*stressed
        basis='explicit_fixed_fraction_ledger_counterfactual_not_backtest'
    else:
        if args.initial_equity is not None or args.max_leverage is not None:
            raise ValueError('Capital overrides require an explicit risk-fraction counterfactual')
        if data['initial_equity'] is None:
            raise ValueError('Phase 1 has no geometric capital: supply explicit risk-fraction, initial-equity and max-leverage for a conditional study')
        initial=data['initial_equity']
        returns=np.array([r['net_pnl']/r['equity_before_entry'] for r in trades])
        observed_risk=np.array([r['initial_risk']/r['equity_before_entry'] for r in trades])
        returns+=observed_risk*(stressed-np.array([r['net_R'] for r in trades]))
        basis='resampled_observed_net_account_return_pairs'
    config=dict(basis=basis,initial_equity=initial,risk_fraction=args.risk_fraction,max_leverage=args.max_leverage,
        simulations=args.simulations,block_days=args.block_days,seed=args.seed,horizon_days=data['days'],
        calendar='UTC actual exit days; multiple closed trades compound within a day',
        drawdown_basis='Daily closed-equity boundaries, not intraday closed-trade or mark-to-market drawdown',
        insolvency='Existing path_stats absorbs wealth at zero; negative debt/liquidation is not modeled')
    config['method']=method
    config['scenario']=scenario
    config['scenario_limits']='Fixed-ledger hypothetical stress; original trade dates/opportunity set retained, not a new fill or market replay'
    if method=='trade-shuffle':
        config.update(
            basis='shuffled_'+basis,block_days=None,horizon_days=None,horizon_trades=len(trades),
            calendar='No calendar resampling; path axis is completed-trade count',
            drawdown_basis='Every closed-trade boundary, including initial equity; not mark-to-market',
            sampling='Uniform random permutation without replacement; every selected trade occurs exactly once per path',
            terminal_equity='Order invariant for fixed trade return factors, apart from floating-point rounding',
            path_storage='All simulated equity paths include the initial-equity point',
            numpy_version=np.__version__,
        )
    if not trades:return dict(status='insufficient_sample',configuration=config,estimates=None)
    if method=='trade-shuffle':
        paths=shuffle_trade_paths(returns,initial,simulations=args.simulations,seed=args.seed)
        peaks=np.maximum.accumulate(paths,axis=1)
        drawdowns=np.max(1-paths/peaks,axis=1)
        finals=paths[:,-1]
        minimum=paths.min(axis=1)
        below=(paths[:,1:]<initial) & ~np.isclose(paths[:,1:],initial,rtol=1e-12,atol=0.)
        fraction_below=below.mean(axis=1)
        average_equity=(paths[:,0]/2+paths[:,1:-1].sum(axis=1)+paths[:,-1]/2)/len(returns)
        return dict(
            status='conditional_trade_order_shuffle_not_new_market_evidence',configuration=config,
            estimates=dict(
                equity_p05=float(np.quantile(finals,.05)),equity_p50=float(np.median(finals)),equity_p95=float(np.quantile(finals,.95)),
                dd_mean=float(drawdowns.mean()),dd_p50=float(np.median(drawdowns)),
                dd_p95=float(np.quantile(drawdowns,.95)),dd_p99=float(np.quantile(drawdowns,.99)),dd_worst=float(drawdowns.max()),
                minimum_equity_p05=float(np.quantile(minimum,.05)),minimum_equity_p50=float(np.median(minimum)),
                minimum_equity_worst=float(minimum.min()),
                frequency_finish_below_initial=float(np.mean(below[:,-1])),frequency_insolvent=float(np.mean(finals==0)),
                frequency_ever_below_initial=float(np.mean(below.any(axis=1))),
                mean_fraction_trades_below_initial=float(fraction_below.mean()),
                mean_equity_over_trade_progress=float(average_equity.mean()),
                average_equity_p05=float(np.quantile(average_equity,.05)),average_equity_p50=float(np.median(average_equity)),
            ),
            source_trade_ids=[r['trade_id'] for r in trades],trade_net_returns=returns.tolist(),
            equity_paths=paths.tolist(),
            closed_trade_performance_bands=shuffled_performance_bands(paths),
            equity_bands=dict(mean=paths.mean(axis=0).tolist(),median=np.median(paths,axis=0).tolist(),
                              p05=np.quantile(paths,.05,axis=0).tolist(),p95=np.quantile(paths,.95,axis=0).tolist()),
            path_statistics=dict(max_drawdown=drawdowns.tolist(),minimum_equity=minimum.tolist(),
                                 fraction_trades_below_initial=fraction_below.tolist(),
                                 average_equity_over_trade_progress=average_equity.tolist()),
        )
    # Group repeated day indices multiplicatively; never overwrite same-day trades.
    factors=np.ones(data['days'])
    np.multiply.at(factors,data['offset'],np.maximum(0.,1+returns))
    final,dd=resample_paths(factors-1,initial,block_days=args.block_days,simulations=args.simulations,seed=args.seed)
    return dict(status='conditional_resampling_not_new_market_evidence',configuration=config,
        estimates=dict(equity_p05=float(np.quantile(final,.05)),equity_p50=float(np.median(final)),equity_p95=float(np.quantile(final,.95)),
            dd_p50=float(np.median(dd)),dd_p95=float(np.quantile(dd,.95)),dd_p99=float(np.quantile(dd,.99)),
            frequency_finish_below_initial=float(np.mean(final<initial)),frequency_insolvent=float(np.mean(final==0))))


def run(args):
    if len(set(args.task))!=len(args.task):raise ValueError('Duplicate evaluation task')
    if min(args.resamples,args.simulations,args.block_days)<1 or args.seed<0:raise ValueError('Invalid resampling parameters')
    if 'monte-carlo' not in args.task and any(v is not None for v in (args.initial_equity,args.risk_fraction,args.max_leverage)):
        raise ValueError('Capital/risk overrides apply only to Monte Carlo; Kelly remains an uncapped reference')
    source=args.run.resolve();output=args.output.resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('Evaluation output must be separate from the immutable source run')
    selectors=dict(model_id=args.model_id,instrument=args.instrument,configuration_id=args.configuration,
                   submodel_id=args.submodel,portfolio_id=args.portfolio)
    data=load_run(source,selectors)
    scenario_path=getattr(args,'scenario_config',None)
    scenario_config=json.loads(scenario_path.read_text(encoding='utf-8-sig')) if scenario_path else {}
    available=scenarios(scenario_config)
    name=getattr(args,'scenario','baseline')
    selected=[s for s in available if s['name']==name]
    if len(selected)!=1:raise ValueError('Unknown selected scenario')
    data['scenario']=selected[0]
    scenario_hash=digest(scenario_path) if scenario_path else None
    output.mkdir(parents=True,exist_ok=False)
    metadata=dict(status='running',phase='post_modelling_evaluation',schema='outcome_diagnostics_v1',
        created_at=datetime.now(timezone.utc).isoformat(),source_run=str(source),source_hashes=data['hashes'],
        selected_account=data['identity'],source_accounting=data['source_summary'],tasks=args.task,results={},
        source_context=data['source_context'],
        source_bounds_ms=data['source_bounds_ms'],calendar_origin_ms=data['calendar_origin_ms'],calendar_days=data['days'],
        source_partial_calendar_bins=[bool(data['source_bounds_ms'][0]%86400000),bool(data['source_bounds_ms'][1]%86400000)],
        prior_inspection=data['metadata'].get('prior_inspection',data['metadata'].get('prior_test_inspection')),
        iteration_id=data['account_metadata'].get('iteration_id',data['metadata'].get('iteration_id')),
        checkpoint_id=data['account_metadata'].get('checkpoint_id',data['metadata'].get('checkpoint_id')),
        source_units={k:data['account_metadata'].get(k,data['metadata'].get(k)) for k in ('quantity_unit','pnl_unit','units')},
        scenario=selected[0],scenario_config_hash=scenario_hash,
        parameters={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items() if k not in ('run','output')},
        limits='Selected closed outcomes only; no new OOS, automatic promotion, entry filtering or phase transition. Resampling is conditional on inspected data. Source gross/net/fees remain separate. A sizing estimate must be explicitly frozen and replayed in Phase 2 by the user for execution claims.')
    import model_development.phase_3.kelly as kelly
    import model_development.phase_3.monte_carlo as stress
    paths=[Path(__file__),Path(__file__).with_name('evaluate.py'),Path(__file__).with_name('run_input.py'),
           Path(kelly.__file__),Path(stress.__file__),Path(stress.__file__).with_name('ledger_io.py')]
    metadata['code']={str(p.resolve()):digest(p) for p in paths}
    def save():(output/'run.json').write_text(json.dumps(metadata,indent=2,allow_nan=False),encoding='utf-8')
    save()
    try:
        if 'kelly' in args.task:
            metadata['results']['kelly']=dict(
                gross=kelly_summary(data,np.array([r['gross_R'] for r in data['trades']]),resamples=args.resamples,block_days=args.block_days,seed=args.seed),
                net=kelly_summary(data,stressed_r(scenario_arrays(data),data['scenario']),resamples=args.resamples,block_days=args.block_days,seed=args.seed),
                definition='Empirical uncapped price-stop risk/equity log-growth reference; gross is original source gross, net uses the selected scenario; quarter Kelly is a reference, not an execution-constrained recommendation; UTC exit-calendar cluster bootstrap')
        if 'monte-carlo' in args.task:metadata['results']['monte-carlo']=monte_carlo(data,args)
        verify_source(data)
        if scenario_path and digest(scenario_path)!=scenario_hash:raise ValueError('Scenario config changed during evaluation')
        if any(digest(Path(p))!=h for p,h in metadata['code'].items()):raise ValueError('Evaluation code changed during run')
        metadata['status']='complete'
    except BaseException as error:
        metadata.update(status='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        metadata['finished_at']=datetime.now(timezone.utc).isoformat();save()
    return metadata
