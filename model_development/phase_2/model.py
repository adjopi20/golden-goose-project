"""Dynamic prepared-intention adapter; existing geometric replay owns execution.

First capability: single-source linear stop/time/optional-TP1 models exposing
prepare_backtest(request). Prepared intentions and prepared-candle models use the same shared lifecycle.
Model capability negotiation rejects unsupported entry/exit protocols explicitly.
"""
from model_development.research_dependencies import bind_clock, clock_source
import argparse
from dataclasses import asdict, dataclass
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Request:
    input: Path
    output_dir: Path
    shared_core: Path
    cache_dir: Path | None
    initial_equity: float
    risk_fraction: float
    fee_bps: float
    max_leverage: float
    model_config: dict
    id_policy: str = 'error'


def load_model(path):
    path = path.resolve()
    if not path.is_file() or path.suffix != '.py':
        raise ValueError('Model must be an existing Python source file')
    if path.is_relative_to(ROOT):
        parts = path.relative_to(ROOT).with_suffix('').parts
        if all(part.isidentifier() for part in parts):
            module = importlib.import_module('.'.join(parts))
            if Path(module.__file__).resolve() != path:
                raise ImportError('Selected model is shadowed by a different checkout')
            return module
    name = '_phase2_model_' + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ModelParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        values=sys.argv[1:] if args is None else args
        batch='--batch' in values
        for action in self._actions:
            if action.dest in ('venue','product','quantity_unit','pnl_unit'):
                action.required=not batch
        result=super().parse_args(args,namespace)
        if isinstance(result.input,list) and len(result.input)==1:
            result.input=result.input[0]
        if not batch and (isinstance(result.input,list) or result.grid_file is not None or result.batch_layout != 'master'):
            self.error('Multiple input assets, --grid-file and --batch-layout require --batch')
        return result


def parser():
    p = ModelParser(description=__doc__)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--input', type=Path, nargs='+', required=True, help='Batch: one raw Parquet per instrument; single mode: one file')
    p.add_argument('--batch',action='store_true',help='Run independent assets and model/execution grid into one compact master')
    p.add_argument('--grid-file',type=Path,help='Versioned model/execution/assets JSON grid; batch only')
    p.add_argument('--batch-layout',choices=['master','folders'],default='master',
                   help='Batch: compact master only, or master plus a full result folder per configuration')
    p.add_argument('--output-dir', '--output', dest='output_dir', type=Path, required=True)
    p.add_argument('--shared-core', type=Path, default=ROOT, help='Default: main project; explicit legacy clock remains supported')
    p.add_argument('--cache-dir', type=Path)
    p.add_argument('--id-policy', choices=['error', 'record'],
                   help='Prepared development records chronological ID discontinuities; other runs default to error')
    p.add_argument('--model-config', default='{}')
    p.add_argument('--initial-equity', type=float, default=1000.)
    p.add_argument('--risk-fraction', type=float, default=.005)
    p.add_argument('--fee-bps', type=float, default=4.)
    p.add_argument('--max-leverage', type=float, default=5.)
    p.add_argument('--venue', required=True)
    p.add_argument('--product', choices=['linear_quote_margined'], required=True)
    p.add_argument('--quantity-unit', required=True)
    p.add_argument('--pnl-unit', required=True)
    p.add_argument('--iteration', required=True)
    p.add_argument('--checkpoint')
    p.add_argument('--inspection-status', choices=['development', 'unknown', 'uninspected'], required=True)
    return p


def run_model(args, *, owner=None):
    from model_development.phase_2 import backtest as engine
    from model_development.observation_cache import candle_cache, source_identity
    from model_development.phase_1.observe import infer_instrument

    config = json.loads(args.model_config)
    if not isinstance(config, dict):
        raise ValueError('model-config must be a JSON object')
    engine.validate_config(engine.Config(args.initial_equity, args.risk_fraction,
                                         args.fee_bps, args.max_leverage))
    module = load_model(args.model)
    is_prepared = callable(getattr(module, 'prepare_backtest', None))
    id_policy = getattr(args, 'id_policy', None)
    if id_policy is None:
        id_policy = 'record' if is_prepared and args.inspection_status == 'development' else 'error'
    if id_policy == 'record' and (not is_prepared or args.inspection_status != 'development'):
        raise ValueError('Recording ID discontinuities requires a prepared development backtest')
    if not is_prepared and not callable(getattr(module, 'create_observer', None)):
        raise ValueError('Unsupported model capability: require prepare_backtest(request); '
                         'stateful create_observer alone is not a prepared-intention adapter')
    bind_clock(args.shared_core)
    instrument = infer_instrument([args.input])
    identity = source_identity([args.input])
    cache_dir = args.cache_dir or ROOT/'tmp/cache/observe'/instrument
    if (cache_dir.resolve().is_relative_to(args.output_dir.resolve())
            or args.output_dir.resolve().is_relative_to(cache_dir.resolve())):
        raise ValueError('Cache and run output must be separate directories')
    request = Request(args.input.resolve(), args.output_dir.resolve(), args.shared_core.resolve(),
                      cache_dir.resolve(), args.initial_equity, args.risk_fraction,
                      args.fee_bps, args.max_leverage, config, id_policy)
    # Reserve even failed attempts. Do not reuse a run.json-only directory.
    if owner is None:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    metadata = dict(run_id=args.output_dir.name, phase='backtest', status='running',
                    adapter='prepared_linear_single_source_v1', source=identity,
                    requested_model_config=config, id_policy=id_policy, requested_execution=dict(
                        initial_equity=args.initial_equity, risk_fraction=args.risk_fraction,
                        fee_bps=args.fee_bps, max_leverage=args.max_leverage))
    if owner is not None:
        metadata['batch_job_id']=owner.current['job_id']
    def save():
        if owner is not None:
            owner.update(metadata)
        if owner is None or owner.per_job:
            (args.output_dir/'run.json').write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding='utf-8')
    save()
    try:
        _, cache = candle_cache([request.input], request.cache_dir, id_policy=id_policy)
        gaps = cache.get('discontinuities', [])
        if cache.get('source_continuity') != 'contiguous_ids_observed' or gaps:
            # Cache validation rejects backwards time, adjacent duplicate IDs and ambiguous same-time resets.
            if (id_policy != 'record' or not gaps
                    or any(gap.get('kind') not in ('forward_id_gap', 'backward_id_transition') for gap in gaps)):
                raise ValueError('Phase 2 requires contiguous IDs or recorded chronological ID discontinuities')
            metadata['raw_gap_note'] = (
                'Exploratory replay of chronological observed prints; forward ID gaps and backward ID '
                'transitions are recorded with original timestamps/IDs and source segments. '
                'Source continuity remains unverified; missing prints are not reconstructed and may affect signals and fills.')
            counts = {kind: sum(gap['kind'] == kind for gap in gaps)
                      for kind in ('forward_id_gap', 'backward_id_transition')}
            metadata['raw_id_discontinuity_counts'] = counts
            print(f'Phase 2: replaying available prints with {len(gaps)} recorded ID discontinuity(s): '
                  f'{counts["forward_id_gap"]} forward gap(s), '
                  f'{counts["backward_id_transition"]} backward transition(s)', flush=True)
        from model_development.lifecycle import Session, PreparedModel
        from model_development.phase_2.stateful import observer, finish
        from model_development.observation_cache import RawPrints
        if not is_prepared:
            minutes, _ = candle_cache([request.input], request.cache_dir, id_policy='error')
            model, schedule, start, end, window = observer(module,request,minutes,cache)
            cfg = engine.Config(request.initial_equity,request.risk_fraction,request.fee_bps,request.max_leverage)
            metadata.update(model_id=args.model.parent.name,model_version=hashlib.sha256(args.model.read_bytes()).hexdigest(),
                model_path=str(args.model.resolve()),instrument=instrument,venue=args.venue,product=args.product,
                quantity_unit=args.quantity_unit,pnl_unit=args.pnl_unit,iteration_id=args.iteration,
                checkpoint_id=args.checkpoint,prior_inspection=args.inspection_status,
                configuration=asdict(model.config),window=window,cache=cache,source=identity,
                adapter='prepared_candle_lifecycle_v1',
                availability='CandleContext gates model snapshots; historical cache availability assumed at close',
                supported_scope='Independent submodel accounts; fixed absolute brackets, optional time exits, no grid/trailing/limit queue')
            metadata['configuration_id']=hashlib.sha256(json.dumps(dict(model=asdict(model.config),execution=asdict(cfg)),sort_keys=True).encode()).hexdigest()
            metadata['code']={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                (Path(__file__),args.model,clock_source(request.shared_core),Path(engine.__file__),
                 ROOT/'utils/engine/adapters/paper_execution.py',ROOT/'model_development/observation_cache.py')}
            for imported in tuple(sys.modules.values()):
                file=getattr(imported,'__file__',None)
                if file and Path(file).suffix=='.py' and (Path(file).resolve().is_relative_to(request.shared_core) or Path(file).resolve().is_relative_to(ROOT)):
                    metadata['code'][str(Path(file).resolve())]=hashlib.sha256(Path(file).read_bytes()).hexdigest()
            save()
            session=Session(model,cfg)
            return finish(session,RawPrints([request.input]),schedule,start,end,
                lambda opening,closing,ts:model.evaluate_prepared(opening,decision_timestamp_ms=ts),request,metadata,sink=owner)
        prepared = module.prepare_backtest(request)
        required = {'signals', 'config', 'start_day', 'end_day', 'metadata', 'timing_validator'}
        if not isinstance(prepared, dict) or not required.issubset(prepared):
            raise ValueError('Incomplete prepared-intention contract')
        signals = prepared['signals']
        lanes = signals if isinstance(signals, dict) else {'prepared': signals}
        if (not lanes or any(not isinstance(key, str) or not key or not isinstance(rows, list)
                            for key, rows in lanes.items()) or not callable(prepared['timing_validator'])):
            raise ValueError('Require prepared signals as a list or named lists and a callable timing validator')
        all_signals = [signal for rows in lanes.values() for signal in rows]
        cfg = prepared['config']
        engine.validate_config(cfg)
        for signal in all_signals:
            fields = ('feature_as_of_ms', 'feature_available_at_ms', 'decision_timestamp_ms',
                      'entry_eligible_timestamp_ms', 'entry_deadline_timestamp_ms', 'force_exit_timestamp_ms')
            if any(type(signal.get(k)) is not int or signal[k] < 0 for k in fields):
                raise ValueError('Require complete integer timing evidence on every signal')
            if not (signal['feature_as_of_ms'] <= signal['feature_available_at_ms']
                    <= signal['decision_timestamp_ms'] <= signal['entry_eligible_timestamp_ms']
                    <= signal['entry_deadline_timestamp_ms'] < signal['force_exit_timestamp_ms']):
                raise ValueError('Signal timing violates availability/decision/eligibility order')
            from datetime import date
            import math
            if (not cache['source_start_ms'] <= signal['entry_eligible_timestamp_ms'] <= cache['source_end_ms']
                    or signal.get('direction') not in ('long','short')
                    or not prepared['start_day'] <= date.fromisoformat(signal['session_day']) <= prepared['end_day']
                    or any(not math.isfinite(float(signal[k])) or float(signal[k]) <= 0 for k in ('stop','entry_reference'))):
                raise ValueError('Prepared signal outside source/session or invalid geometry')
            prepared['timing_validator'](signal)
        for key in ('initial_equity', 'risk_fraction', 'fee_bps', 'max_leverage'):
            if getattr(cfg, key) != getattr(request, key):
                raise ValueError(f'Model changed execution request: {key}')
        if source_identity([request.input]) != identity:
            raise ValueError('Source changed during model preparation')
        metadata.update(prepared['metadata'])
        metadata.update(run_id=args.output_dir.name, phase='backtest', status='running',
            adapter='prepared_linear_single_source_v1', model_path=str(args.model.resolve()),
            requested_model_config=config, instrument=instrument, venue=args.venue,
            product=args.product, quantity_unit=args.quantity_unit, pnl_unit=args.pnl_unit,
            iteration_id=args.iteration, checkpoint_id=args.checkpoint,
            prior_inspection=args.inspection_status,
            venue_product_provenance='Caller-declared; filename identifies instrument only',
            source=identity, cache=cache, command=sys.argv,
            supported_scope='One independent portfolio, complete prepared candidate population, linear stop/time/optional TP1, symmetric per-side fees; no grid or stateful observer support')
        execution = asdict(cfg)
        metadata['configuration_id'] = hashlib.sha256(json.dumps(dict(
            model=config, execution=execution, strategy=metadata.get('strategy_config'),
            calibration=metadata.get('calibration')), sort_keys=True).encode()).hexdigest()
        code = dict(metadata.get('code', {}))
        code_paths = [Path(__file__), Path(__file__).with_name('backtest.py'), args.model,
                      Path(engine.__file__), ROOT/'model_development/phase_2/backtest.py',
                      ROOT/'model_development/observation_cache.py', ROOT/'model_development/phase_1/observe.py',
                      ROOT/'utils/engine/adapters/paper_execution.py', clock_source(args.shared_core)]
        code.update({str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_paths})
        metadata['code'] = code
        save()
        normalized=PreparedModel(signals)
        session=Session(normalized,cfg,timed_scale_out=prepared.get("timed_scale_out"),
                        one_entry_per_utc_day=prepared.get("one_entry_per_utc_day", False),
                        risk_manager=prepared.get("risk_manager"))
        schedule=[(stamp,stamp) for stamp in sorted({s['entry_eligible_timestamp_ms'] for s in all_signals})]
        lower, upper = cache['source_start_ms'], cache['source_end_ms'] + 1
        replay_start, replay_end = prepared.get('replay_window', (lower, upper))
        if (type(replay_start) is not int or type(replay_end) is not int
                or not lower <= replay_start < replay_end <= upper
                or any(not replay_start <= stamp < replay_end for stamp, _ in schedule)):
            raise ValueError('Prepared replay window must contain every decision and lie within source bounds')
        metadata['replay_window_ms'] = [replay_start, replay_end]
        metadata['supported_scope']='Shared lifecycle; single-source prepared intentions; no grid/limit/trailing/size-dependent costs'
        return finish(session,RawPrints([request.input]),schedule,replay_start,replay_end,
            lambda opening,closing,ts:normalized.evaluate_at(closing,ts),request,metadata,
            dates=[prepared['start_day'],prepared['end_day']],sink=owner)
    except BaseException as exc:
        saved = json.loads((args.output_dir/'run.json').read_text(encoding='utf-8'))
        if owner is not None or saved.get('status') != 'failed':
            metadata.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            save()
        raise
