"""Generic prepared-candle model driver; models own all features and geometry."""
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import sys

from model_development.lifecycle import Session, PreparedModel, VERSION, validate_model
from model_development.phase_2.lifecycle_output import write_result
from model_development.observation_cache import RawPrints, aggregate_candles


from model_development.research_dependencies import bind_clock


def observer(module, request, minutes, cache):
    bind_clock(request.shared_core)
    namespace=getattr(module,'SHARED_INDICATOR_NAMESPACE',None)
    if namespace is not None:
        from model_development.research_dependencies import bind_indicator_namespace
        bind_indicator_namespace(namespace)
    else:
        # The main execution package stays bound; only research indicator lookup uses
        # the declared checkout. Never overwrite an already-loaded indicator package.
        existing=sys.modules.get('indicator')
        if existing is not None and Path(existing.__file__).resolve().parent != (request.shared_core/'indicator').resolve():
            raise ImportError('Mutable/legacy indicator package already loaded; use a clean research process')
        if existing is None:
            init=request.shared_core/'indicator/__init__.py'
            spec=importlib.util.spec_from_file_location('indicator',init,submodule_search_locations=[str(init.parent)])
            package=importlib.util.module_from_spec(spec);sys.modules['indicator']=package;spec.loader.exec_module(package)
    model=module.create_observer(request.model_config)
    validate_model(model)
    for name in ('prepare_observation_window','prepare_observation','evaluate_prepared'):
        if not callable(getattr(model,name,None)):
            raise ValueError(f'Unsupported prepared-candle lifecycle capability: {name}')
    window=model.prepare_observation_window(None,None,cache['source_start_ms'],cache['source_end_ms'])
    import pandas as pd
    start=int(pd.Timestamp(window['effective_start_utc']).timestamp()*1000)
    end=int(pd.Timestamp(window['effective_end_exclusive_utc']).timestamp()*1000)
    bars=aggregate_candles(minutes,model.config.base_timeframe,start,end)
    schedule=model.prepare_observation(bars,as_of_ms=end)
    previous=None
    for opening,closing in schedule:
        if not start <= opening < closing <= end or (previous is not None and closing <= previous):
            raise ValueError('Invalid/unsorted model decision schedule')
        previous=closing
    return model,schedule,start,end,window


def drive(session, reader, schedule, start_ms, end_ms, evaluate, *, skip_inert_prints=False):
    """Decisions see pre-print positions; fills/callbacks consume ordered prints."""
    cursor=reader.first(start_ms);limit=reader.first(end_ms)
    iterator=iter(schedule); upcoming=next(iterator,None)
    can_skip = (
        skip_inert_prints and type(session.model) is PreparedModel
        and isinstance(reader, RawPrints) and len(session.portfolios) == 1
    )
    portfolio = next(iter(session.portfolios.values())) if can_skip else None
    processed_prints = 0
    while cursor<limit and cursor[0]<len(reader.groups):
        processed_prints += 1
        tick=reader.tick(cursor);ts=tick['timestamp_ms']
        while upcoming is not None and upcoming[1] <= ts:
            opening,closing=upcoming
            session.decide(lambda:evaluate(opening,closing,ts),ts)
            upcoming=next(iterator,None)
        session.tick(tick)
        cursor=reader.next(cursor)
        if not can_skip or session.model.pending:
            continue
        boundary = min(limit, reader.first(upcoming[1])) if upcoming is not None else limit
        if cursor >= boundary:
            continue
        position = portfolio.position
        if position is None:
            cursor = boundary
        elif portfolio.stop_trigger is None and portfolio.target_trigger is None and portfolio.partial_trigger is None:
            sign = 1 if position["direction"] == "long" else -1
            target = position.get("target_price")
            if target is None:
                target = float("inf") if sign == 1 else -float("inf")
            risk_trigger = position.get("risk_trigger_price")
            if risk_trigger is not None and risk_trigger > 0:
                target = min(target, risk_trigger) if sign == 1 else max(target, risk_trigger)
            partial_target = position.get("tp1_target_price")
            if partial_target is not None and not position.get("tp1_filled", False):
                target = min(target, partial_target) if sign == 1 else max(target, partial_target)
            deadline = position["force_exit_timestamp_ms"]
            if session.timed_scale_out is not None and not position.get("tp1_filled", False):
                deadline = min(deadline, position["entry_timestamp_ms"] + session.timed_scale_out[0])
            hit = reader.crossing(cursor, boundary, position["stop"], target, sign,
                                  force_exit=deadline)
            cursor = hit if hit is not None else boundary
    return dict(skip_inert_prints=can_skip, processed_prints=processed_prints)


def finish(session, reader, schedule, start, end, evaluate, request, metadata, *, dates=None, sink=None):
    try:
        metadata["replay"] = drive(
            session,reader,schedule,start,end,evaluate,
            skip_inert_prints=metadata.get("adapter") == "prepared_linear_single_source_v1",
        )
    finally:
        reader.close()
    from model_development.observation_cache import source_identity
    if source_identity([request.input]) != metadata['source']:
        raise ValueError('Raw source changed during lifecycle replay')
    metadata['lifecycle']=VERSION
    metadata['execution_dependency']=str(Path(sys.modules['utils.engine.adapters.paper_execution'].__file__).resolve())
    metadata['code'].update({str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (Path(__file__),Path(__file__).with_name('lifecycle_output.py'),Path(__file__).parents[1]/'lifecycle.py',
                  Path(metadata['execution_dependency']))})
    dates=dates or [datetime.fromtimestamp(t/1000,timezone.utc).date() for t in (start,end)]
    return write_result(session,request.output_dir,metadata,*dates,end,sink=sink)
