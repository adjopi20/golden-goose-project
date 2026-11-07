"""Compact lifecycle artifacts; reused execution metrics and chart conventions."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean
import json
import math

import pyarrow as pa
import pyarrow.parquet as pq

from model_development.phase_2.backtest import save_equity_chart
from model_development.phase_2.backtest import calculate_shared_backtest_metrics
from model_development.phase_1.observe import SCHEMA


def metrics(trades, config, start, end):
    result = calculate_shared_backtest_metrics(trades, config.initial_equity, start, end)
    if not trades:
        for key in ('win_rate','expectancy','expectancy_r','average_r_per_trade','avg_r','median_r'):
            result[key] = None
    gross = [{**t,'pnl':t['gross_pnl_before_costs'],'r':t['gross_pnl_before_costs']/t['actual_initial_risk']} for t in trades]
    original = calculate_shared_backtest_metrics(gross, config.initial_equity, start, end)
    result.update(return_geometric=result['final_equity']/config.initial_equity-1,
        gross_win_rate=original['win_rate'] if gross else None,
        gross_expectancy_r=original['expectancy_r'] if gross else None,
        gross_profit_factor_money=original['profit_factor'],
        gross_comparison_basis='same actual net-sized quantities; not a second gross-compounded portfolio')
    return result


def loss_reward_summary(trades):
    """Separate fee-inclusive losing outcomes, winning payoff and large winners."""
    winners=[t for t in trades if t['pnl']>0]
    losers=[t for t in trades if t['pnl']<0]
    tails=[t for t in trades if t['r']>=5]
    return dict(
        basis='Closed complete positions; net outcomes include all execution fees; R uses initial stop risk',
        net_winning_trades=len(winners), net_losing_trades=len(losers),
        total_net_wins=sum(t['pnl'] for t in winners),
        total_net_loss_magnitude=-sum(t['pnl'] for t in losers),
        average_net_win_R=mean(t['r'] for t in winners) if winners else None,
        average_net_loss_magnitude_R=-mean(t['r'] for t in losers) if losers else None,
        average_fee_R=mean(t['fees']/t['actual_initial_risk'] for t in trades) if trades else None,
        average_actual_risk_fraction=mean(t['actual_initial_risk']/t['equity_before_entry'] for t in trades) if trades else None,
        average_initial_stop_distance_pct=mean(abs(t['entry_price']-t.get('initial_stop',t['stop']))/t['entry_price']*100 for t in trades) if trades else None,
        tail_threshold_net_R=5, tail_trades=len(tails),
        tail_net_pnl=sum(t['pnl'] for t in tails), tail_net_R=sum(t['r'] for t in tails),
        largest_net_win_R=max((t['r'] for t in trades),default=None),
    )


def result_schema():
    fields = list(SCHEMA)
    extra = dict(batch_job_id=pa.string(),session_day=pa.string(),portfolio_id=pa.string(),job_id=pa.string(),instrument=pa.string(),
        actual_initial_risk=pa.float64(), calibration_fold=pa.int64(),equity_before_entry=pa.float64(),equity_after_exit=pa.float64(),
        tp1_filled=pa.bool_(),tp1_price=pa.float64(),tp1_timestamp_ms=pa.int64(),tp1_fraction=pa.float64(),tp1_r=pa.float64(),
        remaining_exit_quantity=pa.float64(),partial_gross=pa.float64(),partial_exit_fee=pa.float64(),final_exit_fee=pa.float64(),
        stop_trigger_timestamp_ms=pa.int64(),target_trigger_timestamp_ms=pa.int64(),
        initial_stop=pa.float64(),final_stop=pa.float64(),
        risk_lock_r=pa.float64(),risk_stop_updates=pa.int64())
    fields += [pa.field(k,v) for k,v in extra.items() if k not in SCHEMA.names]
    return pa.schema(fields)


def directional_expectancy(trades, start_day, end_day, *, months=3):
    """Closed outcomes by entry session, with explicit empty-side denominators."""
    values = {'long': [], 'short': []}
    periods = {}
    first_month = start_day.year * 12 + start_day.month - 1
    last_month = end_day.year * 12 + end_day.month - 1
    for month in range(first_month, last_month + 1, months):
        boundary = date((month + months) // 12, (month + months) % 12 + 1, 1)
        periods[month] = (max(start_day, date(month // 12, month % 12 + 1, 1)),
                          min(end_day, boundary - timedelta(days=1)), {'long': [], 'short': []})
    for trade in trades:
        day = date.fromisoformat(trade['session_day'])
        offset = day.year * 12 + day.month - 1 - first_month
        values[trade['direction']].append(trade['r'])
        periods[first_month + months * (offset // months)][2][trade['direction']].append(trade['r'])

    def summarize(sides):
        return {side: dict(trades=len(rs), expectancy_net_R=mean(rs) if rs else None)
                for side, rs in sides.items()}

    label = 'monthly_blocks' if months == 1 else 'three_month_blocks'
    return dict(overall=summarize(values), **{label: [
        dict(start=str(start), end=str(end), **summarize(sides))
        for start, end, sides in periods.values()]})


def write_result(session, output, metadata, start_day, end_day, end_ms, *, sink=None):
    rows, jobs = [], []
    for key,p in session.portfolios.items():
        identity = {k:metadata.get(k) for k in ('run_id','model_id','model_version','configuration_id','iteration_id','checkpoint_id')}
        identity.update(submodel_id=metadata.get('submodel_id') if key=='prepared' else str(key),
                        portfolio_id=str(key), job_id=str(key), instrument=metadata['instrument'])
        prefix=metadata.get('batch_job_id')
        if prefix:
            identity.update(batch_job_id=prefix,job_id=prefix+'::'+str(key),portfolio_id=prefix+'::'+str(key))
        for t in p.trades:
            signal = session.entries[key,t['sample_id']]
            rows.append(dict(t, **identity, record_type='closed_trade',trade_id=(identity['portfolio_id']+'::'+t['sample_id']) if prefix or len(session.portfolios)>1 else t['sample_id'],setup_id=t['sample_id'],
                **{name:signal.get(name) for name in ('feature_as_of_ms','feature_available_at_ms','decision_timestamp_ms','entry_eligible_timestamp_ms')},
                calibration_fold=signal.get('calibration_fold',signal.get('c1_fold')),
                gross_R=t['gross_pnl_before_costs']/t['actual_initial_risk'],net_R=t['r'],
                gross_pnl=t['gross_pnl_before_costs'],net_pnl=t['pnl'],initial_risk=t['actual_initial_risk']))
        for lane,block in session.blocks:
            if lane==key:
                names=('setup_id','session_day','direction','feature_as_of_ms','feature_available_at_ms','decision_timestamp_ms',
                       'entry_eligible_timestamp_ms','blocking_setup_id','blocking_entry_timestamp_ms')
                rows.append(dict(identity,record_type='position_block',**{name:block.get(name) for name in names}))
        summary = metrics(p.trades, session.config, start_day, end_day)
        if not math.isclose(summary['final_equity'],p.equity,rel_tol=1e-12,abs_tol=1e-8):
            raise ValueError('Ledger/equity reconciliation failed')
        if any(not math.isclose(t['gross_pnl_before_costs']-t['fees'],t['pnl'],rel_tol=1e-12,abs_tol=1e-8) for t in p.trades):
            raise ValueError('Gross/net/fee reconciliation failed')
        split = (dict(directional_expectancy=directional_expectancy(p.trades, start_day, end_day,
                     months=metadata.get('directional_block_months', 3)))
                 if metadata.get('report_directional_expectancy') else {})
        if p.position is not None:
            position = p.position
            identity['open_position_at_cutoff'] = {
                name: position[name] for name in
                ('sample_id', 'direction', 'entry_timestamp_ms', 'entry_price', 'stop', 'quantity')
            }
        jobs.append(dict(**identity,status='complete',metrics=summary,loss_reward=loss_reward_summary(p.trades),closed_trades=len(p.trades),**split,
            position_blocks=sum(k==key for k,b in session.blocks),open_positions_omitted=int(p.position is not None),
            insolvent=key in session.insolvent))
    metadata.update(status='complete',jobs=jobs,config=asdict(session.config),
        closed_trades=sum(j['closed_trades'] for j in jobs),position_blocks=len(session.blocks),
        open_positions_omitted=sum(j['open_positions_omitted'] for j in jobs),event_counts=session.event_counts,
        pending_entries=len(session.model.pending),
        censoring='No forced terminal close; open and partly realized positions excluded from closed metrics',
        equity_basis='Independent net closed-complete-position accounts; not MTM or a pooled account',
        cost_limits='Fee-only next-print proxy; no funding, bid/ask, impact, liquidation or venue quantity rounding')
    if len(jobs)==1:
        metadata['metrics']=jobs[0]['metrics']
        metadata['loss_reward']=jobs[0]['loss_reward']
    schema=result_schema()
    if sink is not None:
        return sink.append(rows,jobs,session,metadata,start_day,end_day,end_ms,schema)
    pq.write_table(pa.Table.from_pylist(rows,schema=schema),output/'trades.parquet',compression='zstd')
    if len(jobs)==1:
        save_equity_chart(output,next(iter(session.portfolios.values())),start_day,end_timestamp_ms=end_ms)
        metadata['metrics'] = jobs[0]['metrics']
    else:
        plot_accounts(session,output,start_day,end_ms)
    (output/'run.json').write_text(json.dumps(metadata,indent=2,allow_nan=False),encoding='utf-8')
    return metadata


def plot_accounts(session, output, start_day, end_ms):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    groups=getattr(session,'groups',{'quote units':session.portfolios})
    fig, axes=plt.subplots(2*len(groups),1,figsize=(12,6*len(groups)),squeeze=False)
    for index,(unit,portfolios) in enumerate(groups.items()):
        equity_axis,dd_axis=axes[2*index,0],axes[2*index+1,0]
        for key,p in portfolios.items():
            origin=getattr(p,'start_timestamp_ms',None)
            initial=(datetime.fromtimestamp(origin/1000,timezone.utc) if origin is not None
                     else datetime.combine(start_day,datetime.min.time(),timezone.utc))
            times=[datetime.fromtimestamp(x['timestamp_ms']/1000,timezone.utc) if x['timestamp_ms'] is not None
                   else initial for x in p.equity_curve]
            values=[x['equity'] for x in p.equity_curve]
            end=datetime.fromtimestamp(getattr(p,'end_timestamp_ms',end_ms)/1000,timezone.utc)
            if end>times[-1]:times.append(end);values.append(values[-1])
            peak=values[0];dd=[]
            for value in values:
                peak=max(peak,value);dd.append(100*(value/peak-1))
            equity_axis.step(times,values,where='post',label=str(key))
            dd_axis.step(times,dd,where='post',label=str(key))
        dd_axis.sharex(equity_axis)
        equity_axis.set_ylabel(f'Closed equity ({unit})');dd_axis.set_ylabel('Drawdown %')
        dd_axis.set_xlabel('UTC exit time');equity_axis.legend(fontsize=7)
        equity_axis.grid(alpha=.2);dd_axis.grid(alpha=.2)
    fig.tight_layout();fig.savefig(output/'equity_curve.png',dpi=140);plt.close(fig)
