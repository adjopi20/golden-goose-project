"""Internal Phase 2 grid orchestration; backtest.py is the sole public runner."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
from itertools import product
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from model_development.phase_2.backtest import Config, validate_config
from model_development.phase_2.lifecycle_output import result_schema, plot_accounts, write_result
from model_development.phase_1.observe import infer_instrument
from model_development.observation_cache import source_identity

EXECUTION = ('initial_equity','risk_fraction','fee_bps','max_leverage')
MARKET = ('venue','product','quantity_unit','pnl_unit')


def canonical(value):
    return json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))


def plan(args):
    grid = json.loads(args.grid_file.read_text(encoding='utf-8-sig')) if args.grid_file else {'schema_version':1}
    if (not isinstance(grid,dict) or type(grid.get('schema_version')) is not int or grid.get('schema_version')!=1
            or set(grid)-{'schema_version','model','execution','assets','model_cases'}):
        raise ValueError('Expected grid schema_version=1 with model/execution/assets/model_cases namespaces only')
    base = json.loads(args.model_config)
    if not isinstance(base,dict): raise ValueError('model-config must be an object')
    canonical(grid);canonical(base)
    cases=grid.get('model_cases',[{}])
    if (not isinstance(cases,list) or not cases
            or any(not isinstance(case,dict) or any(not isinstance(k,str) or not k for k in case) for case in cases)):
        raise ValueError('model_cases must be a nonempty list of model configuration objects')
    fields=[]
    for group in ('model','execution'):
        parameters=grid.get(group,{})
        if not isinstance(parameters,dict):raise ValueError(f'{group} grid must be an object')
        if group=='execution' and set(parameters)-set(EXECUTION):
            raise ValueError('Unsupported execution grid field')
        for name,values in sorted(parameters.items()):
            if (not isinstance(values,list) or not values
                    or any(value in values[:i] for i,value in enumerate(values))):
                raise ValueError(f'{group}.{name}: require a nonempty list of unique values')
            if group=='execution' and any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in values):
                raise ValueError(f'{name}: numeric execution values required')
            fields.append((group,name,values))
    assets=grid.get('assets',{})
    if not isinstance(assets,dict):raise ValueError('assets must map instruments to explicit market metadata')
    inputs=args.input if isinstance(args.input,list) else [args.input]
    sources={}
    for path in inputs:
        instrument=infer_instrument([path])
        if instrument in sources:
            raise ValueError('Batch currently requires one file per instrument; same-instrument split files are unsupported')
        sources[instrument]=path.resolve()
    if not sources:raise ValueError('At least one source instrument is required')
    if set(assets)-set(sources):raise ValueError('Asset metadata contains instruments absent from --input')
    jobs=[];identities=set()
    model_sha=hashlib.sha256(args.model.read_bytes()).hexdigest()
    for instrument,path in sources.items():
        market={k:getattr(args,k,None) for k in MARKET}
        overrides=assets.get(instrument,{})
        if not isinstance(overrides,dict) or set(overrides)-set(MARKET):
            raise ValueError('Unknown asset metadata field')
        market.update(overrides)
        if any(not isinstance(market[k],str) or not market[k].strip() for k in MARKET):
            raise ValueError(f'{instrument}: explicitly declare venue/product/quantity_unit/pnl_unit')
        if market['product']!='linear_quote_margined':raise ValueError('Unsupported product convention')
        for values,case in product(product(*(v for _,_,v in fields)),cases):
            model=deepcopy(base);execution={k:getattr(args,k) for k in EXECUTION}
            for (group,name,_),value in zip(fields,values):
                (model if group=='model' else execution)[name]=value
            model.update(deepcopy(case))
            execution={k:float(v) for k,v in execution.items()}
            validate_config(Config(**execution))
            effective=dict(instrument=instrument,model_sha256=model_sha,model_config=model,execution=execution,**market)
            job_id=instrument+'_'+hashlib.sha256(canonical(effective).encode()).hexdigest()[:16]
            if job_id in identities:raise ValueError('Duplicate effective job configuration')
            identities.add(job_id)
            jobs.append(dict(job_id=job_id,status='pending',input=str(path),**effective))
    return grid,jobs,{key:source_identity([path]) for key,path in sources.items()}


class Output:
    def __init__(self,args,grid,jobs,sources):
        from datetime import datetime,timezone
        self.args=args;self.curves={};self.groups={};self.current=None
        self.per_job=args.batch_layout=='folders'
        self.metadata=dict(run_id=args.output_dir.name,phase='backtest',status='running',
            orchestration='phase2_batch_v1',batch_layout=args.batch_layout,grid=grid,base_model_config=json.loads(args.model_config),
            iteration_id=args.iteration,checkpoint_id=args.checkpoint,prior_inspection=args.inspection_status,
            source_manifest=sources,jobs=jobs,account_policy='Independent asset/configuration/submodel accounts; no pooled equity',
            created_at=datetime.now(timezone.utc).isoformat(),command=list(sys.argv),schema='phase2_lifecycle_batch_v1',
            reserved_oos=None,evidence_limits='No automatic final-OOS isolation or outcome-based selection',
            selection='All declared variants; no automatic winner selection; inspected outcomes remain development evidence')
        paths=[Path(__file__),*[Path(__file__).with_name(n) for n in ('backtest.py','model.py','stateful.py','lifecycle_output.py')],
               Path(__file__).parents[1]/'lifecycle.py',args.model]
        self.metadata['code']={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        args.output_dir.mkdir(parents=True,exist_ok=False)
        self.writer=pq.ParquetWriter(args.output_dir/'trades.parquet',result_schema(),compression='zstd')
        self.closed=False
        self.save()

    def save(self):
        (self.args.output_dir/'run.json').write_text(json.dumps(self.metadata,indent=2,allow_nan=False),encoding='utf-8')

    def update(self,metadata):
        self.current['metadata']=deepcopy(metadata)
        self.current['status']=metadata['status']
        self.save()

    def append(self,rows,accounts,session,metadata,start_day,end_day,end_ms,schema):
        if not schema.equals(result_schema()):raise ValueError('Inconsistent master schema across batch jobs')
        for offset in range(0,len(rows),4096):
            self.writer.write_table(pa.Table.from_pylist(rows[offset:offset+4096],schema=schema))
        # Retain equity points only, not model state, raw prints or candle tables.
        for account,(key,p) in zip(accounts,session.portfolios.items()):
            label=f"{metadata['instrument']} / {metadata['batch_job_id']} / {account['submodel_id']}"
            from datetime import datetime, timezone
            start_ms=int(datetime.combine(start_day,datetime.min.time(),timezone.utc).timestamp()*1000)
            view=SimpleNamespace(equity_curve=deepcopy(p.equity_curve),end_timestamp_ms=end_ms,start_timestamp_ms=start_ms)
            self.curves[label]=view
            self.groups.setdefault(metadata['pnl_unit'],{})[label]=view
        if self.per_job:
            write_result(session,Path(self.current['output_dir']),deepcopy(metadata),start_day,end_day,end_ms)
        metadata['accounts']=accounts
        metadata.pop('jobs',None)
        metadata['status']='complete'
        self.update(metadata)
        return metadata

    def close(self):
        if not self.closed:
            self.writer.close();self.closed=True

    def complete(self):
        from datetime import datetime,timezone
        self.close()
        if any(j['status']!='complete' for j in self.metadata['jobs']):
            raise ValueError('Cannot complete a batch with unfinished jobs')
        # Independently reconcile saved master counts and P&L to every account.
        import math
        accounts=[a for job in self.metadata['jobs'] for a in job['metadata']['accounts']]
        totals={a['portfolio_id']:dict(closed=0,blocks=0,net_pnl=0.,gross_pnl=0.,fees=0.) for a in accounts}
        seen=set()
        source=pq.ParquetFile(self.args.output_dir/'trades.parquet')
        try:
            for batch in source.iter_batches(columns=['portfolio_id','record_type','trade_id','net_pnl','gross_pnl','fees']):
                for row in batch.to_pylist():
                    if row['portfolio_id'] not in totals:raise ValueError('Unknown saved account identity')
                    total=totals[row['portfolio_id']]
                    if row['record_type']=='closed_trade':
                        if row['trade_id'] in seen:raise ValueError('Closed-trade identities collide across batch accounts')
                        seen.add(row['trade_id']);total['closed']+=1
                        for k in ('net_pnl','gross_pnl','fees'):total[k]+=row[k]
                    elif row['record_type']=='position_block':
                        total['blocks']+=1
                        if any(row[k] is not None for k in ('net_pnl','gross_pnl','fees')):
                            raise ValueError('Position block has an invented monetary outcome')
                    else:raise ValueError('Unknown master record type')
        finally:
            source.close()
        for account in accounts:
            total=totals[account['portfolio_id']]
            if (total['closed']!=account['closed_trades'] or total['blocks']!=account['position_blocks']):
                raise ValueError('Saved master/account count mismatch')
            for column,metric in [('net_pnl','net_pnl_after_fees_slippage'),('gross_pnl','gross_pnl_before_fees_slippage'),('fees','total_fees')]:
                if not math.isclose(total[column],account['metrics'][metric],rel_tol=1e-12,abs_tol=1e-8):
                    raise ValueError('Saved master/account monetary reconciliation failed')
        if len(seen)!=sum(a['closed_trades'] for a in accounts):
            raise ValueError('Closed-trade identities collide across batch accounts')
        for path,expected in self.metadata['code'].items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:
                raise ValueError('Orchestration/model source changed before completion')
        from datetime import date
        plot_accounts(SimpleNamespace(portfolios=self.curves,groups=self.groups),self.args.output_dir,date(1970,1,1),0)
        expected={'trades.parquet','run.json','equity_curve.png'}
        if self.per_job:
            expected.update(j['job_id'] for j in self.metadata['jobs'])
        if {p.name for p in self.args.output_dir.iterdir()}!=expected:
            raise ValueError('Unexpected batch artifacts outside declared output layout')
        self.metadata.update(status='complete',closed_trades=sum(a['closed_trades'] for a in accounts),
            finished_at=datetime.now(timezone.utc).isoformat(),
            position_blocks=sum(a['position_blocks'] for a in accounts),open_positions_omitted=sum(a['open_positions_omitted'] for a in accounts))
        self.save()
        return self.metadata


def run_batch(args):
    from model_development.phase_2.model import run_model
    grid,jobs,sources=plan(args)
    owner=Output(args,grid,jobs,sources)
    try:
        for job in jobs:
            owner.current=job;job['status']='running';owner.save()
            if source_identity([Path(job['input'])])!=sources[job['instrument']]:
                raise ValueError('Source identity changed between batch jobs')
            if hashlib.sha256(args.model.read_bytes()).hexdigest()!=job['model_sha256']:
                raise ValueError('Model code changed during batch')
            child=deepcopy(args)
            if owner.per_job:
                child.output_dir=args.output_dir/job['job_id']
                child.output_dir.mkdir(exist_ok=False)
                job['output_dir']=str(child.output_dir.resolve())
            print(f"Batch {jobs.index(job)+1}/{len(jobs)}: {job['instrument']} {job['model_config']}",flush=True)
            child.input=Path(job['input']);child.model_config=canonical(job['model_config'])
            for k in MARKET:setattr(child,k,job[k])
            for k,v in job['execution'].items():setattr(child,k,v)
            if args.cache_dir:child.cache_dir=args.cache_dir/job['instrument']
            run_model(child,owner=owner)
        for instrument,identity in sources.items():
            path=next(Path(j['input']) for j in jobs if j['instrument']==instrument)
            if source_identity([path])!=identity:raise ValueError('Source changed before batch completion')
        return owner.complete()
    except BaseException as error:
        from datetime import datetime,timezone
        owner.metadata.update(status='failed',finished_at=datetime.now(timezone.utc).isoformat(),error=f'{type(error).__name__}: {error}')
        if owner.current is not None and owner.current['status']!='complete':
            owner.current.update(status='failed',error=f'{type(error).__name__}: {error}')
        owner.save()
        raise
    finally:
        owner.close()
