"""Generic parameter/asset batch orchestration with exact cached-path replay."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from itertools import product
import json
import math
from copy import deepcopy
from pathlib import Path
import sys
import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

from model_development.observation_cache import candle_cache, aggregate_candles, RawPrints, source_identity


LEGACY_ORCHESTRATORS = {
    'observe.py': {'3a469c9e076e461c468ccfb27e5f837da4215e9e74fc459a6b27b54d968fb727', 'e192abc87c884a84dd4f47680f5cc68302fedf8855e6fdf38d49e528040badb8'},
    'observe_batch.py': {'bdf499550107cbf7079b2887769325ca23e174b8cb5f401716a7ef7ca90af227', 'affc6feb783f530c567471316f89186ac3b63366fc56ced0b4739ebc1a514fe5', 'e8e6373f02be033898ca4a54cfabc185eec1b627ea44439b5167519e11cfda69'},
    'observation_cache.py': {'315bfa84cec3856c837359199634fb59052fba30204457de36fcdc3905c3d440', 'df01ceda77bccf4a22d3cc3828c1bf3695a1decf9d36a3a55855fc6f151a6686'},
}


def resume_plan(args, grid, base, groups, schema):
    """Carry complete jobs only; parent run stays immutable and partial jobs rerun."""
    from model_development.phase_1 import observe
    if not getattr(args,'resume_from',None):
        return None,pa.Table.from_pylist([],schema=schema),[]
    parent=args.resume_from.resolve()
    if parent==args.output.resolve():
        raise ValueError('Resume into a NEW output directory; preserve the parent run')
    saved=json.loads((parent/'run.json').read_text(encoding='utf-8'))
    if saved.get('status') not in ('failed','complete'):
        raise ValueError('Parent run must be finalized failed/complete, not possibly still running')
    if saved.get('grid')!=grid or saved.get('base_configuration')!=base:
        raise ValueError('Resume requires the same parameter grid and model configuration')
    for name,value in [('fixed_risk',args.risk),('entry_fee_bps',args.entry_fee_bps),
                       ('exit_fee_bps',args.exit_fee_bps),('iteration_id',args.iteration),('checkpoint_id',args.checkpoint)]:
        if saved.get(name)!=value:
            raise ValueError(f'Resume mismatch: {name}')
    original=observe.parser().parse_args(saved['command'][1:])
    if (original.start,original.end,original.venue)!=(args.start,args.end,args.venue):
        raise ValueError('Resume date/venue mismatch')
    if original.model.resolve()!=args.model.resolve() or original.shared_core.resolve()!=args.shared_core.resolve():
        raise ValueError('Resume model/shared-core path mismatch')
    for path,expected in saved['dependencies'].items():
        target=Path(path)
        if not target.exists():
            raise ValueError(f'Missing original dependency: {path}')
        if observe.sha(target)!=expected and expected not in LEGACY_ORCHESTRATORS.get(target.name,set()):
            raise ValueError(f'Changed model/execution dependency prevents safe resume: {path}')
    jobs=deepcopy([job for job in saved['jobs'] if job['status']=='complete'])
    keys={(j['instrument'],j['configuration_id']) for j in jobs}
    if len(keys)!=len(jobs):
        raise ValueError('Duplicate completed jobs in parent')
    for instrument in {j['instrument'] for j in jobs}:
        if instrument not in groups:
            raise ValueError(f'Include the original completed asset {instrument} in --input')
        expected=saved['sources'][instrument]['identity']['sources']
        if source_identity(groups[instrument])!=expected:
            raise ValueError(f'Completed asset source changed: {instrument}')
    table=pq.read_table(parent/'trades.parquet')
    if not table.schema.equals(schema,check_metadata=False):
        raise ValueError('Parent master schema is incompatible')
    names=pc.binary_join_element_wise(table['instrument'],table['configuration_id'],'|')
    keep=pa.array([f'{a}|{b}' for a,b in keys],type=pa.string())
    table=table.filter(pc.is_in(names,value_set=keep))
    # Reconcile counts before carrying data; an incomplete write cannot masquerade as completed work.
    for job in jobs:
        part=table.filter(pc.and_(pc.equal(table['instrument'],job['instrument']),
                                 pc.equal(table['configuration_id'],job['configuration_id'])))
        counts=job['counts']
        for label,field in [('closed_trade','closed_trades'),('position_block','position_blocks')]:
            if pc.sum(pc.cast(pc.equal(part['record_type'],label),pa.int64())).as_py()!=counts[field]:
                # Arrow returns None on an empty array.
                actual=part.filter(pc.equal(part['record_type'],label)).num_rows
                if actual!=counts[field]:
                    raise ValueError(f'Parent master/count mismatch: {job["instrument"]}/{field}')
        job['carried_from_run']=saved['run_id']
    table=table.set_column(table.schema.get_field_index('run_id'),'run_id',pa.array([args.output.name]*len(table),type=pa.string()))
    provenance=dict(parent=str(parent),parent_run_id=saved['run_id'],parent_status=saved['status'],
        parent_master_sha256=observe.sha(parent/'trades.parquet'),parent_metadata_sha256=observe.sha(parent/'run.json'),
        carried_jobs=len(jobs),carried_rows=len(table),policy='complete matching jobs only; partial jobs discarded and recomputed')
    return dict(saved=saved,provenance=provenance),table,jobs


class Lane:
    """Same shared next-print engine, advancing directly to relevant raw events."""
    def __init__(self, model, reader, identity, args, folds, emit):
        from utils.engine.adapters.paper_execution import Portfolio
        self.model,self.reader,self.identity,self.args,self.folds,self.emit = model,reader,identity,args,folds,emit
        self.portfolios = {k:Portfolio(str(k),[],1000.) for k in model.submodels}
        self.pending_fills = {}
        self.cursor = None
        self.closed = self.blocks = 0

    def common(self,key,signal):
        names=('setup_id','direction','feature_as_of_ms','feature_available_at_ms',
               'decision_timestamp_ms','entry_eligible_timestamp_ms')
        return dict(self.identity,submodel_id=str(key),**{n:signal.get(n) for n in names})

    def exit(self,key,tick):
        portfolio=self.portfolios[key]
        trade=portfolio.trades.pop()
        signal=self.model.positions[key]
        row=self.common(key,signal)
        row.update(record_type='closed_trade',trade_id=f"{self.identity['instrument']}:{self.identity['configuration_id']}:{signal['setup_id']}",
            entry_price=trade['entry_price'],exit_price=trade['exit_price'],
            entry_timestamp_ms=trade['entry_timestamp_ms'],exit_timestamp_ms=tick['timestamp_ms'],
            entry_agg_trade_id=trade['entry_agg_trade_id'],exit_agg_trade_id=tick['agg_trade_id'],
            stop=trade['stop'],target=trade['target_price'],quantity=trade['quantity'],
            initial_risk=self.args.risk,gross_pnl=trade['gross_pnl_before_costs'],
            entry_fee=trade['entry_fee'],exit_fee=trade['exit_fee'],fees=trade['fees'],
            net_pnl=trade['pnl'],gross_R=trade['gross_pnl_before_costs']/self.args.risk,
            net_R=trade['pnl']/self.args.risk,exit_reason=trade['exit_reason'])
        row['test_fold']=next((f['fold'] for f in self.folds if f['complete'] and
            f['test_start_ms']<=row['entry_timestamp_ms']<f['test_end_ms']),None)
        self.emit(row)
        self.model.record_exit(key,price=tick['price'],timestamp_ms=tick['timestamp_ms'],reason=trade['exit_reason'])
        self.model.records[key].clear()
        portfolio.equity=1000.
        portfolio.orders.clear(); portfolio.equity_curve.clear()
        self.pending_fills.pop(key,None)
        self.closed+=1

    def on_print(self,key,ref):
        from utils.engine.adapters.paper_execution import Config,on_tick
        portfolio=self.portfolios[key]
        tick=self.reader.tick(ref)
        on_tick(portfolio,tick,Config(fee_bps=self.args.exit_fee_bps))
        if portfolio.position is None:
            self.exit(key,tick)
        elif portfolio.stop_trigger or portfolio.target_trigger:
            self.pending_fills[key]=self.reader.next(ref)

    def advance(self,limit):
        if self.cursor is None:
            return
        for key,portfolio in self.portfolios.items():
            if portfolio.position is None:
                continue
            if key in self.pending_fills:
                fill=self.pending_fills[key]
                if fill<limit:
                    self.on_print(key,fill)
                continue
            position=portfolio.position
            sign=1 if position['direction']=='long' else -1
            trigger=self.reader.crossing(self.reader.next(self.cursor),limit,position['stop'],
                position['target_price'],sign,position.get('force_exit_timestamp_ms'))
            if trigger is not None:
                self.on_print(key,trigger)
                fill=self.pending_fills.get(key)
                if fill is not None and fill<limit:
                    self.on_print(key,fill)

    def decision(self,opening_ms,end_ms):
        ref=self.reader.first(end_ms)
        tick=self.reader.tick(ref)
        self.advance(ref)
        self.model.evaluate_prepared(opening_ms,decision_timestamp_ms=tick['timestamp_ms'])
        for event in self.model.position_blocks:
            row=self.common(event['submodel_key'],event)
            row.update(record_type='position_block',blocking_setup_id=event['blocking_setup_id'],
                       blocking_entry_timestamp_ms=event['blocking_entry_timestamp_ms'])
            self.emit(row); self.blocks+=1
        self.model.position_blocks.clear()
        for key,portfolio in self.portfolios.items():
            if portfolio.position is not None:
                self.on_print(key,ref)
                continue
            if key not in self.model.pending:
                continue
            position=self.model.accept_fill(key,price=tick['price'],timestamp_ms=tick['timestamp_ms'])
            if position is None:
                continue
            distance=abs(tick['price']-position['stop'])
            quantity=self.args.risk/distance
            if not math.isfinite(quantity) or quantity<=0:
                raise ValueError('Invalid fixed-risk quantity')
            portfolio.position=dict(sample_id=position['setup_id'],session_day=str(pd.to_datetime(tick['timestamp_ms'],unit='ms',utc=True).date()),
                strategy=self.identity['model_id'],route=str(key),direction=position['direction'],
                entry_timestamp_ms=tick['timestamp_ms'],entry_agg_trade_id=tick['agg_trade_id'],entry_price=tick['price'],
                entry_reference=tick['price'],entry_reference_deviation_bps=0.,quantity=quantity,
                entry_fee=quantity*tick['price']*self.args.entry_fee_bps/10000,actual_initial_risk=self.args.risk,
                equity_before_entry=1000.,leverage_at_entry=quantity*tick['price']/1000.,stop_risk_pct=distance/tick['price'],
                stop=position['stop'],target_price=position['target'],
                force_exit_timestamp_ms=position.get('force_exit_timestamp_ms',sys.maxsize))
        self.cursor=ref


def run_batch(args):
    from model_development.phase_1 import observe
    if args.cache_dir is None or args.grid_file is None:
        raise ValueError('Batch requires --cache-dir and --grid-file')
    if args.instrument:
        raise ValueError('Batch infers each instrument from its own input filenames')
    if args.risk<=0 or not math.isfinite(args.risk):
        raise ValueError('Risk must be positive and finite')
    if any(not math.isfinite(v) or not 0<=v<10000 for v in (args.entry_fee_bps,args.exit_fee_bps)):
        raise ValueError('Invalid fee')
    sys.path.insert(0,str(args.shared_core.resolve()))
    from model_development.research_dependencies import bind_clock
    bind_clock(args.shared_core)
    grid=json.loads(args.grid_file.read_text(encoding='utf-8-sig'))
    if not isinstance(grid,dict) or not grid or any(not isinstance(v,list) or not v for v in grid.values()):
        raise ValueError('Grid must map parameter names to nonempty lists')
    keys=list(grid)
    configurations=[dict(zip(keys,values)) for values in product(*(grid[k] for k in keys))]
    encoded=[json.dumps(c,sort_keys=True) for c in configurations]
    if len(set(encoded))!=len(encoded):
        raise ValueError('Duplicate grid configurations')
    base=json.loads(args.model_config)
    if not isinstance(base,dict):
        raise ValueError('model-config must be an object')
    groups={}
    for path in args.input:
        instrument=observe.infer_instrument([path])
        groups.setdefault(instrument,[]).append(path)
    factory=observe.load_model(args.model,dict(base,**configurations[0]))
    if not all(hasattr(factory,name) for name in ('prepare_observation_window','prepare_observation','evaluate_prepared')):
        raise ValueError('Selected model must implement cached observation/window interface')
    schema=observe.SCHEMA
    for name in ('instrument','venue','timeframe','anchor_period'):
        schema=schema.append(pa.field(name,pa.string()))
    resumed,carried,completed_jobs=resume_plan(args,grid,base,groups,schema)
    args.output.mkdir(parents=True,exist_ok=False)
    metadata=dict(run_id=args.output.name,status='running',schema_version=3,protocol='global_observe_v3',
        created_at=datetime.now(timezone.utc).isoformat(),command=sys.argv,grid=grid,base_configuration=base,
        iteration_id=args.iteration,checkpoint_id=args.checkpoint,prior_test_inspection=args.inspection_status,
        fixed_risk=args.risk,entry_fee_bps=args.entry_fee_bps,exit_fee_bps=args.exit_fee_bps,
        sizing='fixed initial money risk; independent asset/variant/SD experiments; no combined account',
        product='linear',units='base_quantity_quote_pnl',open_trade_policy='omit open outcomes; counts only; no terminal close',
        selection='full predeclared grid; no test-driven winner selection; repeated inspected tests are development history',
        train_membership='train_start <= entry < test_start and exit < test_start; otherwise purge outcome',
        execution='shared next-print engine; raw first crossing then actual next print; decisions before same-boundary print',
        cache_directory=str(args.cache_dir.resolve()),id_policy=getattr(args,'id_policy','error'),sources={},jobs=completed_jobs,
        dependencies=observe.dependency_fingerprints(args.shared_core,args.model))
    metadata['dependencies'].update({str(Path(__file__).resolve()):observe.sha(Path(__file__)),
        str((Path(__file__).resolve().parents[2]/'model_development/observation_cache.py').resolve()):observe.sha((Path(__file__).resolve().parents[2]/'model_development/observation_cache.py'))})
    if resumed:
        metadata['resume']=resumed['provenance']
        metadata['sources']=deepcopy(resumed['saved']['sources'])
    buffer=[]
    writer=pq.ParquetWriter(args.output/'trades.parquet',schema,compression='zstd')
    def save():
        (args.output/'run.json').write_text(json.dumps(metadata,indent=2,allow_nan=False),encoding='utf-8')
    def flush():
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer,schema=schema)); buffer.clear()
    def emit(row):
        buffer.append(row)
        if len(buffer)>=4096:
            flush()
    save()
    try:
        for part in carried.to_batches(max_chunksize=4096):
            writer.write_batch(part)
        completed_keys={(j['instrument'],j['configuration_id']) for j in completed_jobs}
        for instrument,paths in groups.items():
            if sum(j['instrument']==instrument for j in completed_jobs)==len(configurations):
                print(f'Resume: {instrument} already complete; carrying existing results without replay',flush=True)
                continue
            metadata['active_asset']=instrument;save()
            print(f'Asset {instrument}: prepare/reuse candle cache',flush=True)
            minutes,cache=candle_cache(paths,args.cache_dir/instrument,id_policy=getattr(args,'id_policy','error'))
            metadata['sources'][instrument]=cache
            reader=RawPrints(paths)
            for parameters in configurations:
                model=observe.load_model(args.model,dict(base,**parameters))
                window=model.prepare_observation_window(args.start,args.end,cache['source_start_ms'],cache['source_end_ms'])
                start=int(pd.Timestamp(window['effective_start_utc']).timestamp()*1000)
                end=int(pd.Timestamp(window['effective_end_exclusive_utc']).timestamp()*1000)
                if getattr(model, "needs_observation_prints", False):
                    if cache.get("discontinuities"):
                        raise ValueError("Raw-print observation requires a contiguous source")
                    schedule=model.prepare_observation_from_cache(minutes,start_ms=start,as_of_ms=end,prints=reader)
                else:
                    bars=aggregate_candles(minutes,model.config.base_timeframe,start,end)
                    schedule=model.prepare_observation(bars,as_of_ms=end)
                configuration=asdict(model.config)
                config_id=hashlib.sha256(json.dumps(configuration,sort_keys=True).encode()).hexdigest()
                if (instrument,config_id) in completed_keys:
                    print(f'Resume: {instrument} {parameters} already complete; skipping',flush=True)
                    continue
                folds=observe.fold_schedule(start,end)
                identity=dict(run_id=args.output.name,model_id=args.model.parent.name,model_version=observe.sha(args.model),
                    configuration_id=config_id,iteration_id=args.iteration,checkpoint_id=args.checkpoint,instrument=instrument,
                    venue=args.venue,timeframe=model.config.evaluation_timeframe,anchor_period=configuration.get('anchor_period'))
                job=dict(instrument=instrument,configuration_id=config_id,configuration=configuration,window=window,
                    status='running',source_continuity=cache.get('source_continuity','contiguous_ids_observed'),
                    folds=folds,fold_counts={str(f['fold']):dict(closed_train=0,purged_train=0,closed_test=0) for f in folds})
                if hasattr(model, 'observation_diagnostics'):
                    job['diagnostics'] = model.observation_diagnostics
                metadata['jobs'].append(job); save()
                def emit_job(row):
                    if row['record_type']=='closed_trade':
                        for fold in folds:
                            c=job['fold_counts'][str(fold['fold'])]
                            if fold['train_start_ms']<=row['entry_timestamp_ms']<fold['test_start_ms']:
                                c['closed_train' if row['exit_timestamp_ms']<fold['test_start_ms'] else 'purged_train']+=1
                            if fold['complete'] and fold['test_start_ms']<=row['entry_timestamp_ms']<fold['test_end_ms']:
                                c['closed_test']+=1
                    emit(row)
                lane=Lane(model,reader,identity,args,folds,emit_job)
                last_progress=time.monotonic()
                for opening,closing in schedule:
                    ref=reader.first(closing)
                    if reader.tick(ref)['timestamp_ms']>=end:
                        break
                    lane.decision(opening,closing)
                    if time.monotonic()-last_progress>=20:
                        print(f"{instrument} {parameters}: through {pd.to_datetime(closing,unit='ms',utc=True)}, closed={lane.closed}",flush=True)
                        last_progress=time.monotonic()
                lane.advance(reader.first(end))
                job.update(status='complete',counts=dict(closed_trades=lane.closed,position_blocks=lane.blocks,
                    omitted_open_positions={str(k):int(p.position is not None) for k,p in lane.portfolios.items()}))
                if source_identity(paths)!=cache['identity']['sources']:
                    raise ValueError('Raw source changed during observation')
                flush(); save()
                print(f"Complete {instrument} {parameters}: {lane.closed} closed trades",flush=True)
            reader.close()
        flush(); writer.close(); metadata['status']='complete'
    except BaseException as error:
        if 'reader' in locals():
            reader.close()
        metadata.update(status='failed',error=f'{type(error).__name__}: {error}')
        if 'job' in locals() and job.get('status')=='running':
            job['status']='failed'
        raise
    finally:
        try:
            if metadata['status']!='complete':
                flush(); writer.close()
        finally:
            metadata['finished_at']=datetime.now(timezone.utc).isoformat();save()
    print(f'Batch complete: {args.output}',flush=True)
    return metadata
