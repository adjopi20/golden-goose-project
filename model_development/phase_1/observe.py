"""Shared fixed-risk raw-aggTrade observation. No consolidated reports.

Model interface: create_observer(config), config timeframes, submodels,
evaluate, pending/positions/position_blocks, accept_fill and record_exit.
Execution reuses utils.engine.adapters.paper_execution, including next-print exits.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import re
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


from model_development.research_dependencies import bind_clock, clock_source

DAY = 86400000
STRINGS = ('record_type run_id trade_id setup_id model_id submodel_id model_version '
           'configuration_id iteration_id checkpoint_id direction exit_reason blocking_setup_id').split()
INTS = ('feature_as_of_ms feature_available_at_ms decision_timestamp_ms entry_eligible_timestamp_ms '
        'entry_timestamp_ms exit_timestamp_ms entry_agg_trade_id exit_agg_trade_id '
        'blocking_entry_timestamp_ms test_fold').split()
FLOATS = ('entry_price exit_price stop target quantity initial_risk gross_pnl entry_fee '
          'exit_fee fees net_pnl gross_R net_R').split()
SCHEMA = pa.schema([(n, pa.string()) for n in STRINGS] + [(n, pa.int64()) for n in INTS]
                   + [(n, pa.float64()) for n in FLOATS])


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def infer_instrument(paths, override=None):
    symbols = set()
    for path in paths:
        match = re.fullmatch(r'([A-Za-z0-9]+)-aggTrades-.+\.parquet', path.name)
        if match:
            symbols.add(match.group(1).upper())
        elif override is None:
            raise ValueError(f'Cannot infer instrument from filename: {path.name}')
    if len(symbols) > 1:
        raise ValueError('All source files must represent the same instrument')
    if override is not None:
        if symbols and symbols != {override.upper()}:
            raise ValueError('Instrument override conflicts with source filename')
        return override.upper()
    return next(iter(symbols))


def dependency_fingerprints(core, model_path):
    # Discover model-imported shared modules; never require a particular indicator.
    root = core.resolve()
    project = Path(__file__).resolve().parents[2]
    paths = {Path(__file__).resolve(), model_path.resolve(),
             clock_source(root), project/'utils/engine/adapters/paper_execution.py'}
    for module in tuple(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename:
            path = Path(filename).resolve()
            if path.suffix == '.py' and not path.is_relative_to(project/'tmp') and (path.is_relative_to(root) or path.is_relative_to(project)):
                paths.add(path)
    return {str(path): sha(path) for path in sorted(paths)}


def timestamp(value):
    t = pd.Timestamp(value)
    if t.tz is None:
        t = t.tz_localize('UTC')
    t = t.tz_convert('UTC')
    if t != t.normalize() or t.day != 1:
        raise ValueError('Run boundaries must be first-of-month UTC midnight')
    return int(t.timestamp() * 1000)


def fold_schedule(start, end):
    origin = pd.to_datetime(start, unit='ms', utc=True)
    folds = []
    step = 0
    while True:
        train = origin + pd.DateOffset(months=2*step)
        test = origin + pd.DateOffset(months=8+2*step)
        finish = origin + pd.DateOffset(months=10+2*step)
        folds.append(dict(fold=step+1, train_start_ms=int(train.timestamp()*1000),
            test_start_ms=int(test.timestamp()*1000), test_end_ms=int(finish.timestamp()*1000),
            complete=int(finish.timestamp()*1000) <= end))
        if int(finish.timestamp()*1000) >= end:
            break
        step += 1
    return folds


def load_model(path, config):
    name = 'observe_model_' + sha(path)[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    namespace = getattr(module, 'SHARED_INDICATOR_NAMESPACE', None)
    if namespace is not None:
        project = str(Path(__file__).resolve().parents[2])
        if project not in sys.path:
            sys.path.append(project)
        from model_development.research_dependencies import bind_indicator_namespace
        bind_indicator_namespace(namespace)
    model = module.create_observer(config)
    for name in ('submodels', 'pending', 'positions', 'position_blocks', 'evaluate',
                 'accept_fill', 'record_exit', 'config', 'records'):
        if not hasattr(model, name):
            raise ValueError(f'Model missing observer interface: {name}')
    return model


def ticks(paths, start, end, manifest):
    """Streaming variant of existing raw_ticks: add quantities and cross-file checks."""
    previous = None
    covered_start = covered_end = False
    for path in paths:
        source = pq.ParquetFile(path)
        fields = ('timestamp', 'price', 'qty', 'agg_trade_id')
        if not set(fields).issubset(source.schema_arrow.names):
            raise ValueError(f'{path}: require {fields}')
        for name in ('timestamp', 'agg_trade_id'):
            if not pa.types.is_integer(source.schema_arrow.field(name).type):
                raise ValueError(f'{path}: {name} must be integer milliseconds/IDs')
        # Metadata fingerprint is explicit; not misrepresented as a full data hash.
        manifest.append(dict(path=str(path.resolve()), bytes=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns, rows=source.metadata.num_rows,
            schema=str(source.schema_arrow), fingerprint_kind='size_mtime_schema_rowcount'))
        source_info = manifest[-1]
        stamp_column = source.schema_arrow.get_field_index('timestamp')
        stats = [source.metadata.row_group(i).column(stamp_column).statistics
                 for i in range(source.num_row_groups)]
        if not stats or any(s is None or not s.has_min_max for s in stats):
            raise ValueError('Timestamp row-group statistics required to check coverage')
        source_info.update(source_min_ms=int(min(s.min for s in stats)),
                           source_max_ms=int(max(s.max for s in stats)))
        covered_start |= source_info['source_min_ms'] <= start
        # Existing observer/replay row-group selection avoids scanning earlier years.
        groups = [i for i,s in enumerate(stats) if s.max >= start and s.min <= end]
        # End may lie between groups: read the first later group for coverage only.
        following = next((i for i,s in enumerate(stats) if s.min > end), None)
        if following is not None:
            groups.append(following)
        for batch in source.iter_batches(row_groups=groups, columns=list(fields), batch_size=65536):
            for ts, price, qty, ident in zip(*(batch.column(i).to_pylist() for i in range(4))):
                if any(x is None for x in (ts, price, qty, ident)):
                    raise ValueError('Null aggTrade field')
                if (ts < 0 or ident < 0 or not math.isfinite(price) or price <= 0
                        or not math.isfinite(qty) or qty <= 0):
                    raise ValueError('Invalid aggTrade timestamp/ID/price/quantity')
                if previous is not None:
                    if ts < previous[0] or ident != previous[1]+1:
                        raise ValueError('Unordered, duplicate or missing aggregate IDs')
                previous = (ts, ident)
                source_info.setdefault('observed_first_ms',ts)
                source_info['observed_last_ms'] = ts
                covered_start |= ts <= start
                covered_end |= ts >= end
                if start <= ts < end:
                    yield dict(timestamp_ms=ts, price=float(price), qty=float(qty), agg_trade_id=ident)
                if ts >= end:
                    if not covered_start:
                        raise ValueError('Source does not cover requested start')
                    return
    if not covered_start or not covered_end:
        raise ValueError('Source must cover both requested boundaries, including an end-boundary tick')


def execute(model, stream, *, start, end, risk, entry_bps, exit_bps, identity, folds, emit):
    from utils.closed_candle_context import timeframe_ms
    from utils.engine.adapters.paper_execution import Config, Portfolio, on_tick
    duration = timeframe_ms(model.config.base_timeframe)
    evaluation_ms = timeframe_ms(model.config.evaluation_timeframe)
    calculation_ms = timeframe_ms(model.config.calculation_timeframe)
    if any(value < duration or value % duration for value in (evaluation_ms, calculation_ms)):
        raise ValueError('Calculation/evaluation timeframe must be a multiple of base timeframe')
    portfolios = {key: Portfolio(str(key), [], 1000.) for key in model.submodels}
    history, current = [], None
    closed = blocks = 0
    last_progress = time.monotonic()

    def common(key, signal):
        row = dict(identity, submodel_id=str(key))
        row.update({n: signal.get(n) for n in ('setup_id', 'direction', 'feature_as_of_ms',
            'feature_available_at_ms', 'decision_timestamp_ms', 'entry_eligible_timestamp_ms')})
        return row

    for tick in stream:
        ts, price = tick['timestamp_ms'], tick['price']
        bucket = ts // duration * duration
        if current is None:
            if bucket != start:
                raise ValueError('Missing first research candle')
            current = dict(timestamp_ms=bucket, open=price, high=price, low=price, close=price, volume=0.)
        elif bucket != current['timestamp_ms']:
            if bucket != current['timestamp_ms'] + duration:
                raise ValueError('Missing candle coverage; no fabricated zero-volume bars')
            history.append(current)
            if bucket % evaluation_ms == 0:
                frame = pd.DataFrame(history)
                model.evaluate(frame, decision_timestamp_ms=ts)
            for event in model.position_blocks:
                key = event['submodel_key']
                row = common(key, event)
                row.update(record_type='position_block', blocking_setup_id=event['blocking_setup_id'],
                           blocking_entry_timestamp_ms=event['blocking_entry_timestamp_ms'])
                emit(row)
                blocks += 1
            model.position_blocks.clear()
            current = dict(timestamp_ms=bucket, open=price, high=price, low=price, close=price, volume=0.)
        # Decisions see pre-tick positions; existing exits happen before new fills.
        for key, portfolio in portfolios.items():
            if portfolio.position is not None:
                on_tick(portfolio, tick, Config(fee_bps=exit_bps))
                if portfolio.position is None:
                    trade = portfolio.trades.pop()
                    signal = model.positions[key]
                    row = common(key, signal)
                    row.update(record_type='closed_trade', trade_id=signal['setup_id'],
                        entry_price=trade['entry_price'], exit_price=trade['exit_price'],
                        entry_timestamp_ms=trade['entry_timestamp_ms'], exit_timestamp_ms=ts,
                        entry_agg_trade_id=trade['entry_agg_trade_id'], exit_agg_trade_id=tick['agg_trade_id'],
                        stop=trade['stop'], target=trade['target_price'], quantity=trade['quantity'],
                        initial_risk=risk, gross_pnl=trade['gross_pnl_before_costs'],
                        entry_fee=trade['entry_fee'], exit_fee=trade['exit_fee'], fees=trade['fees'],
                        net_pnl=trade['pnl'], gross_R=trade['gross_pnl_before_costs']/risk,
                        net_R=trade['pnl']/risk, exit_reason=trade['exit_reason'])
                    row['test_fold'] = next((f['fold'] for f in folds if f['complete'] and
                        f['test_start_ms'] <= row['entry_timestamp_ms'] < f['test_end_ms']), None)
                    emit(row)
                    model.record_exit(key, price=price, timestamp_ms=ts, reason=trade['exit_reason'])
                    model.records[key].clear()
                    closed += 1
                    portfolio.equity = 1000.  # Never used for quantity: constant money risk below.
                    portfolio.orders.clear()
                    portfolio.equity_curve.clear()
                continue
            if key not in model.pending:
                continue
            position = model.accept_fill(key, price=price, timestamp_ms=ts)
            if position is None:
                continue
            distance = abs(price-position['stop'])
            quantity = risk / distance
            if not math.isfinite(quantity) or quantity <= 0:
                raise ValueError('Invalid fixed-risk quantity')
            portfolio.position = dict(sample_id=position['setup_id'], session_day=str(pd.to_datetime(ts, unit='ms', utc=True).date()),
                strategy=identity['model_id'], route=str(key), direction=position['direction'],
                entry_timestamp_ms=ts, entry_agg_trade_id=tick['agg_trade_id'], entry_price=price,
                entry_reference=price, entry_reference_deviation_bps=0., quantity=quantity,
                entry_fee=quantity*price*entry_bps/10000, actual_initial_risk=risk,
                equity_before_entry=1000., leverage_at_entry=quantity*price/1000.,
                stop_risk_pct=distance/price, stop=position['stop'], target_price=position['target'],
                force_exit_timestamp_ms=position.get('force_exit_timestamp_ms', sys.maxsize))
        current.update(high=max(current['high'], price), low=min(current['low'], price), close=price,
                       volume=current['volume']+tick['qty'])
        if time.monotonic()-last_progress >= 20:
            print(f'Observed through {pd.to_datetime(ts, unit="ms", utc=True)}; closed={closed}; blocks={blocks}', flush=True)
            last_progress = time.monotonic()
    return dict(closed_trades=closed, position_blocks=blocks,
                omitted_open_positions={str(k): int(p.position is not None) for k,p in portfolios.items()})


def run(args):
    if getattr(args,'resume_from',None) or getattr(args,'id_policy','error')!='error':
        raise ValueError('--resume-from / --id-policy apply to --batch only')
    start, end = timestamp(args.start), timestamp(args.end)
    if end <= start or not math.isfinite(args.risk) or args.risk <= 0:
        raise ValueError('Require end > start and positive finite fixed risk')
    if any(not math.isfinite(v) or not 0 <= v < 10000 for v in (args.entry_fee_bps, args.exit_fee_bps)):
        raise ValueError('Explicit fee rates must be in [0,10000) bps')
    sys.path.insert(0, str(args.shared_core.resolve()))
    bind_clock(args.shared_core)
    config = json.loads(args.model_config)
    if not isinstance(config, dict):
        raise ValueError('model-config must be a JSON object')
    bounds = []
    for path in args.input:
        source = pq.ParquetFile(path)
        column = source.schema_arrow.get_field_index('timestamp')
        if column < 0:
            raise ValueError(f'{path}: missing timestamp column')
        stats = [source.metadata.row_group(i).column(column).statistics
                 for i in range(source.num_row_groups)]
        if not stats or any(s is None or not s.has_min_max for s in stats):
            raise ValueError('Timestamp row-group statistics required to check coverage')
        bounds.append((min(s.min for s in stats),max(s.max for s in stats)))
    if min(b[0] for b in bounds) > start or max(b[1] for b in bounds) < end:
        raise ValueError('Source metadata does not cover requested boundaries')
    instrument = infer_instrument(args.input, args.instrument)
    model = load_model(args.model, config)
    folds = fold_schedule(start,end)
    identity = dict(run_id=args.output.name, model_id=args.model.parent.name,
        model_version=sha(args.model), configuration_id=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
        iteration_id=args.iteration, checkpoint_id=args.checkpoint)
    # Never overwrite/reuse a run, including an earlier failed run.
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = dict(identity, status='running', protocol='global_observe_v3', schema_version=2,
        created_at=datetime.now(timezone.utc).isoformat(), command=sys.argv,
        configuration=asdict(model.config), requested_config=config, start_ms=start, end_ms=end,
        venue=args.venue, product='linear', instrument=instrument, units='base_quantity_quote_pnl',
        sizing='fixed_initial_quote_risk_no_compounding_fractional_quantity_no_rounding', fixed_risk=args.risk,
        entry_fee_bps=args.entry_fee_bps, exit_fee_bps=args.exit_fee_bps,
        execution='entry_first_print_after_decision; exit_next_print_after_cross; decision_before_same_timestamp_tick',
        candles='UTC closed base candles; historical availability assumed at close; decision at first following print on evaluation boundary',
        source_manifest=[], folds=folds, timezone='UTC', selection='predeclared fixed model/config; no fitted calibration or automatic winner selection',
        train_membership='entry >= train_start and entry < test_start and exit < test_start; otherwise purged',
        fold_carry='continuous state/config across folds; test attribution by entry; no terminal close',
        prior_test_inspection=args.inspection_status, open_trade_policy='omit outcomes, count only',
        shared_core=str(args.shared_core.resolve()), baseline_run=args.baseline_run,
        dependencies=dependency_fingerprints(args.shared_core, args.model))
    output = args.output/'run.json'
    def save():
        output.write_text(json.dumps(metadata,indent=2,allow_nan=False),encoding='utf-8')
    save()
    buffer=[]
    writer=pq.ParquetWriter(args.output/'trades.parquet',SCHEMA,compression='zstd')
    def flush():
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer,schema=SCHEMA))
            buffer.clear()
    fold_counts = {str(f['fold']): dict(closed_train=0, purged_train=0, closed_test=0) for f in folds}
    metadata['fold_counts'] = fold_counts
    def emit(row):
        if row['record_type'] == 'closed_trade':
            for fold in folds:
                counts = fold_counts[str(fold['fold'])]
                entry, exit_time = row['entry_timestamp_ms'], row['exit_timestamp_ms']
                if fold['train_start_ms'] <= entry < fold['test_start_ms']:
                    counts['closed_train' if exit_time < fold['test_start_ms'] else 'purged_train'] += 1
                if fold['complete'] and fold['test_start_ms'] <= entry < fold['test_end_ms']:
                    counts['closed_test'] += 1
        buffer.append(row)
        if len(buffer)>=4096:
            flush()
    try:
        metadata['counts'] = execute(model, ticks(args.input,start,end,metadata['source_manifest']),
            start=start,end=end,risk=args.risk,entry_bps=args.entry_fee_bps,exit_bps=args.exit_fee_bps,
            identity=identity,folds=folds,emit=emit)
        flush()
        writer.close()
        metadata['status']='complete'
    except BaseException as error:
        metadata.update(status='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        if metadata['status'] != 'complete':
            try:
                flush()
            finally:
                writer.close()
        metadata['finished_at']=datetime.now(timezone.utc).isoformat()
        save()
    print(f'Complete: {args.output}',flush=True)
    return metadata


class ObservationParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        batch = '--batch' in (sys.argv[1:] if args is None else args)
        for action in self._actions:
            if action.dest in ('start','end'):
                action.required = not batch
        return super().parse_args(args, namespace)


def parser():
    p=ObservationParser(description=__doc__)
    p.add_argument('--batch',action='store_true',help='Run assets and configuration grid into one master')
    p.add_argument('--resume-from',type=Path,help='Batch only: carry completed matching jobs from a finalized earlier run into a new output')
    p.add_argument('--id-policy',choices=('error','record'),default='error',help='Batch only: reject forward ID gaps, or record explicitly accepted source-continuity uncertainty')
    p.add_argument('--grid-file',type=Path)
    p.add_argument('--cache-dir',type=Path)
    p.add_argument('--model',type=Path,required=True)
    p.add_argument('--shared-core',type=Path,default=Path(__file__).resolve().parents[2], help='Default: main project research clock; explicit legacy checkout remains supported')
    p.add_argument('--input',type=Path,nargs='+',required=True,help='Chronological, non-overlapping files for one instrument')
    p.add_argument('--start',required=True,help='UTC first of month, inclusive')
    p.add_argument('--end',required=True,help='UTC first of month, exclusive')
    p.add_argument('--risk',type=float,required=True,help='Constant initial risk in quote units')
    p.add_argument('--entry-fee-bps',type=float,default=4.,help='Per-side fee in bps; default 4 = 0.04 percent')
    p.add_argument('--exit-fee-bps',type=float,default=4.,help='Per-side fee in bps; default 4 = 0.04 percent')
    p.add_argument('--venue',required=True)
    p.add_argument('--instrument',help='Default: SYMBOL from SYMBOL-aggTrades-....parquet; override must agree')
    p.add_argument('--iteration',required=True)
    p.add_argument('--checkpoint')
    p.add_argument('--baseline-run')
    p.add_argument('--inspection-status',required=True,choices=('uninspected','development','unknown'))
    p.add_argument('--model-config',default='{}')
    p.add_argument('--output',type=Path,required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    if args.batch:
        from model_development.phase_1.observe_batch import run_batch
        return run_batch(args)
    return run(args)


if __name__=='__main__':
    main()
