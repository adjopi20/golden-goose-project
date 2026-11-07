"""Daily-profile signals using completed 15-minute price and delta observations.

Run on raw aggTrades or pass prepared 15-minute bars and completed profiles.
This module emits entry intentions; the execution engine owns actual fills/costs.
"""
from __future__ import annotations
from model_development.research_dependencies import bind_clock, clock_source

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from indicator.value_area import value_area

from utils.market_session import NY, clock_ms
from model_development.models.trend_following_preny_profile_15m.legacy.runtime.baseline import BAR_MS, STRATEGY, PROFILE_WINDOWS, number, validate_bar, evaluate_session


def to_path_audit_signal(signal: dict, entry_tick: dict) -> dict:
    """Bind an engine-selected eligible raw fill to model_development.phase_2.backtest.

    Engine must select the first executable tick, not a favorable later price.
    This function validates its time/risk geometry and does not simulate fills.
    """
    timestamp = int(entry_tick["timestamp_ms"])
    entry = number(entry_tick, "price")
    side = 1 if signal["direction"] == "long" else -1
    if not signal["entry_eligible_timestamp_ms"] <= timestamp <= signal["entry_deadline_timestamp_ms"]:
        raise ValueError("Entry fill falls outside the entry window")
    if entry <= 0 or side * (entry - signal["stop"]) <= 0:
        raise ValueError("Actual fill has invalid POC stop geometry")
    return {**signal, "entry_timestamp_ms":timestamp, "entry":entry,
            "entry_fill_agg_trade_id":int(entry_tick["agg_trade_id"]),
            "entry_price_is_reference":False}


def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq
        return pq.read_table(path).to_pylist()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def prepare_aggtrades(path: Path, start_day: date, end_day: date,
                      *, profile_bins: int = 50, value_fraction: float = .70
                      ) -> tuple[dict[str, list[dict]], list[dict], list[dict]]:
    """Stream raw aggTrades into Pre-NY profiles and 15-minute bars.

    This reads only Parquet row groups overlapping [first day 01:00,
    final day 12:00). It does not load the entire file into memory.
    """
    import pyarrow.parquet as pq

    if end_day < start_day or profile_bins < 2 or not 0 < value_fraction < 1:
        raise ValueError("Invalid date range or profile parameters")
    raw_start = clock_ms(start_day, 1)
    raw_end = clock_ms(end_day, 12)
    source = pq.ParquetFile(path)
    required = {"timestamp", "price", "qty", "is_buyer_maker", "agg_trade_id"}
    if not required.issubset(source.schema_arrow.names):
        raise ValueError(f"Raw aggTrades need columns: {sorted(required)}")
    profile_prices: dict[tuple[str, date], dict[float, float]] = defaultdict(lambda: defaultdict(float))
    bars_15m: dict[int, dict] = {}

    def add_trade(target: dict[int, dict], bar_start: int, bar_end: int,
                  stamp: int, trade_id: int, price: float, qty: float, buyer_maker: bool) -> None:
        bar = target.get(bar_start)
        order = (stamp, trade_id)
        if bar is None:
            bar = target[bar_start] = dict(
                open_timestamp_ms=bar_start, close_timestamp_ms=bar_end,
                open=price, high=price, low=price, close=price,
                buy_volume=0., sell_volume=0., _first=order, _last=order)
        if order < bar["_first"]:
            bar["open"], bar["_first"] = price, order
        if order >= bar["_last"]:
            bar["close"], bar["_last"] = price, order
        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["sell_volume" if buyer_maker else "buy_volume"] += qty

    def finish_bars(target: dict[int, dict]) -> list[dict]:
        result = []
        for bar in sorted(target.values(), key=lambda b: b["open_timestamp_ms"]):
            bar.pop("_first")
            bar.pop("_last")
            result.append(bar)
        return result
    processed = 0
    for rg in range(source.num_row_groups):
        stats = source.metadata.row_group(rg).column(source.schema_arrow.get_field_index("timestamp")).statistics
        if stats is not None and stats.has_min_max and (stats.max < raw_start or stats.min >= raw_end):
            continue
        for batch in source.iter_batches(row_groups=[rg], columns=["timestamp", "price", "qty", "is_buyer_maker", "agg_trade_id"], batch_size=100_000):
            data = batch.to_pydict()
            ids = data["agg_trade_id"]
            for stamp, price, qty, buyer_maker, trade_id in zip(
                    data["timestamp"], data["price"], data["qty"],
                    data["is_buyer_maker"], ids):
                if stamp is None or not raw_start <= stamp < raw_end:
                    continue
                if (price is None or qty is None or buyer_maker is None or trade_id is None
                        or price <= 0 or qty <= 0):
                    raise ValueError("Invalid aggTrade inside requested period")
                processed += 1
                local = datetime.fromtimestamp(stamp / 1000, NY)
                day = local.date()
                if start_day <= day <= end_day and 1 <= local.hour < 9:
                    profile_prices[("pre_ny", day)][float(price)] += float(qty)
                if start_day <= day <= end_day and 9 <= local.hour < 12:
                    quarter_start = clock_ms(day, local.hour, local.minute // 15 * 15)
                    add_trade(bars_15m, quarter_start, quarter_start + BAR_MS,
                              stamp, int(trade_id), float(price), float(qty), buyer_maker)
    if not processed:
        raise ValueError("No aggTrades in requested interval")
    profile_sets: dict[str, list[dict]] = {window: [] for window in PROFILE_WINDOWS}
    for window in PROFILE_WINDOWS:
        for day in (start_day + timedelta(days=i) for i in range((end_day-start_day).days + 1)):
            prices = profile_prices.get((window, day))
            if not prices:
                continue
            profile_sets[window].append(dict(
                session_day=str(day), profile_window=window,
                profile_start_timestamp_ms=clock_ms(day, 1),
                profile_end_timestamp_ms=clock_ms(day, 9),
                **value_area(prices, profile_bins, value_fraction)))
    # Preserve the helper's tuple contract; hourly context is no longer produced.
    return profile_sets, finish_bars(bars_15m), []


def main() -> None:
    import sys
    if "--backtest" in sys.argv:
        phase2_main()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Raw Binance aggTrades Parquet; prepares Pre-NY profiles and 15m bars")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--profile-bins", type=int, default=50)
    parser.add_argument("--value-fraction", type=float, default=.70)
    parser.add_argument("--bars-15m", type=Path, help="Prepared 15-minute OHLC/orderflow bars: parquet or JSONL")
    parser.add_argument("--profiles", type=Path, help="Prepared profile rows: parquet or JSONL")
    parser.add_argument("--profile-window", choices=PROFILE_WINDOWS, default="pre_ny")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--confirmation-bars", type=int, choices=(2, 3), default=2)
    args = parser.parse_args()
    if args.input:
        if args.bars_15m or args.profiles or not args.start_date or not args.end_date:
            parser.error("--input needs --start-date and --end-date, without prepared inputs")
        profile_sets, bars, candles_1h = prepare_aggtrades(
            args.input, args.start_date, args.end_date,
            profile_bins=args.profile_bins, value_fraction=args.value_fraction)
    else:
        if not args.bars_15m or not args.profiles:
            parser.error("Provide either --input with dates, or --bars-15m and --profiles")
        bars, profiles = read_rows(args.bars_15m), read_rows(args.profiles)
        profile_sets = {window: [p for p in profiles if p.get("profile_window") == window]
                        for window in PROFILE_WINDOWS}
        candles_1h = []
    grouped = defaultdict(list)
    for bar in bars:
        local = datetime.fromtimestamp(int(bar["open_timestamp_ms"]) / 1000, NY)
        if 9 <= local.hour < 12:
            grouped[str(local.date())].append(bar)
    if (args.output_dir / "prior_24h").exists():
        raise ValueError("Output contains legacy prior_24h results; use a fresh Pre-NY-only output directory")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.input:
        prepared_profiles = [p for window in PROFILE_WINDOWS for p in profile_sets[window]]
        for name, rows in (("prepared_profiles", prepared_profiles),
                           ("prepared_bars_15m", bars)):
            (args.output_dir / f"{name}.jsonl").write_text(
                "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
    selected_windows = PROFILE_WINDOWS
    summaries = {}
    for window in selected_windows:
        profiles = profile_sets[window]
        days = [p["session_day"] for p in profiles]
        if len(days) != len(set(days)):
            raise ValueError(f"Duplicate {window} profile session_day")
        signals, decisions = [], []
        for profile in sorted(profiles, key=lambda p: p["session_day"]):
            s, d = evaluate_session(profile, grouped[profile["session_day"]],
                                    symbol=args.symbol, confirmation_bars=args.confirmation_bars)
            signals.extend(s)
            decisions.extend(d)
        output = args.output_dir / window
        output.mkdir(parents=True, exist_ok=True)
        for name, rows in (("signals", signals), ("decisions", decisions)):
            (output / f"{name}.jsonl").write_text(
                "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
        summary = dict(strategy=STRATEGY, symbol=args.symbol, profile_window=window,
                       sessions=len(profiles), signals=len(signals),
                       confirmation_bars=args.confirmation_bars,
                       observation_bar_minutes=15, execution_simulated=False,
                       timezone="America/New_York")
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        summaries[window] = summary
    print(json.dumps(summaries, indent=2))


def c1_calibrated_signals(candidates: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Frozen historical 180d/60d C1 schedule; no outcomes and no unknown fallback."""
    import math
    from .evaluate_plugs import _thresholds
    from .runtime.gates import _gate

    rows = sorted(candidates, key=lambda r: (r["session_day"], r["sample_id"]))
    counts = dict(candidates=len(rows), warmup_or_uncalibrated=0, unavailable_features=0, rejected_c1=0)
    if not rows:
        return [], [], counts
    if len({r["sample_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate C1 candidate")
    first, last = (date.fromisoformat(rows[i]["session_day"]) for i in (0, -1))
    cursor = first + timedelta(days=180)
    accepted, folds, covered = [], [], set()
    while cursor + timedelta(days=1) <= last:
        train_start, test_start = cursor-timedelta(days=180), cursor+timedelta(days=1)
        test_end = test_start+timedelta(days=60)
        keys = ("directional_delta_imbalance", "directional_result_atr")
        valid = lambda r: all(isinstance(r["features"].get(k), (int, float))
                              and math.isfinite(r["features"][k]) for k in keys)
        train = [r for r in rows if train_start <= date.fromisoformat(r["session_day"]) < cursor and valid(r)]
        thresholds = _thresholds(train, "delta_without_result") if len(train) >= 25 else None
        fold = dict(train_start=str(train_start), train_end_exclusive=str(cursor),
            test_start=str(test_start), test_end_exclusive=str(test_end),
            train_candidates=len(train), thresholds=thresholds, taken=0)
        for row in rows:
            if not test_start <= date.fromisoformat(row["session_day"]) < test_end:
                continue
            covered.add(row["sample_id"])
            if thresholds is None:
                counts["warmup_or_uncalibrated"] += 1
            elif not valid(row):
                counts["unavailable_features"] += 1
            elif _gate(row["features"], "delta_without_result", thresholds):
                accepted.append({**row["signal"], "c1_fold": len(folds)+1})
                fold["taken"] += 1
            else:
                counts["rejected_c1"] += 1
        folds.append(fold)
        cursor += timedelta(days=60)
    counts["warmup_or_uncalibrated"] += sum(r["sample_id"] not in covered for r in rows)
    return accepted, folds, counts


def prepare_backtest(a):
    """Model-owned causal candidates/calibration; global runner owns execution/output."""
    import hashlib
    import importlib.util
    import sys
    from dataclasses import asdict
    import numpy as np
    import pandas as pd
    from model_development.phase_2.backtest import Config, run_phase2
    from model_development.observation_cache import candle_cache, source_identity, RawPrints
    from model_development.phase_1.observe import infer_instrument
    from indicator.legacy_preny import hourly_indicators

    if a.model_config:
        raise ValueError("Frozen C1 has no model-config overrides")
    source_path = a.input.resolve()
    if infer_instrument([source_path]) != "HYPEUSDT":
        raise ValueError("This frozen Phase 2 configuration is HYPEUSDT only")
    core = clock_source(a.shared_core)
    module = bind_clock(a.shared_core)
    cfg = Config(a.initial_equity, a.risk_fraction, a.fee_bps, a.max_leverage,
                 300_000, tp1_r=1., tp1_fraction=.1)
    from utils.engine.adapters.paper_execution import validate_config
    validate_config(cfg)
    metadata = dict(run_id=a.output_dir.name, model_id="trend_following_preny_profile_15m",
        model_version="phase2_hype_c1_v1", submodel_id="c1_delta", iteration_id="ITER-001",
        checkpoint_id=None, legacy_checkpoint="TF-PRENY-PAPER-001", phase="backtest", status="running",
        venue="binance", product="linear_usdt_margined_futures", instrument="HYPEUSDT",
        quantity_unit="HYPE", pnl_unit="USDT",
        venue_product_provenance="User-declared historical dataset; filename alone does not prove venue/product",
        config=asdict(cfg), selection="Frozen contract; no parameter/asset search",
        prior_inspection="Historical sample used in prior model selection; not unseen OOS",
        reserved_oos=None, calibration="180 calendar days, minimum 25 valid candidates; 1d embargo; 60d blocks",
        strategy_config=dict(timezone="America/New_York", profile_window="01:00-09:00",
            profile_bins=50, value_fraction=.70, confirmation_bars=2, entry_window="09:00-12:00 inclusive",
            stop="frozen Pre-NY POC", tp1_r=1., tp1_fraction=.1,
            remainder="original stop or next-day 08:59 NY; no trail/breakeven"),
        calibration_unknown_policy="NO_TRADE; no full-sample/future thresholds or baseline fallback",
        timeframes=dict(base="1m measured", model_evaluation="15m", c1_atr="1h legacy formula"),
        availability="Historical completed-candle-close assumption; no measured receipt latency",
        source=source_identity([source_path]))
    print("Phase 2: validating/reusing raw-derived minute data", flush=True)
    cache_root = a.cache_dir or Path(__file__).resolve().parents[4] / "tmp/cache/observe/HYPEUSDT"
    minutes, cache = candle_cache([source_path], cache_root)
    if cache.get("source_continuity") != "contiguous_ids_observed":
        raise ValueError("Full-history Phase 2 requires validated contiguous raw IDs")
    if not {"buy_volume", "sell_volume", "delta"}.issubset(minutes):
        raise ValueError("C1 needs measured aggressor-side volume")
    lower, upper = cache["source_start_ms"], cache["source_end_ms"]
    start, finish = (datetime.fromtimestamp(t / 1000, NY).date() for t in (lower, upper))
    if clock_ms(start, 1) < lower:
        start += timedelta(days=1)
    print(f"Phase 2: exact profiles and baseline candidates {start} through {finish}", flush=True)
    profile_sets, bars, _ = prepare_aggtrades(source_path, start, finish)
    profiles = [r for r in profile_sets["pre_ny"] if r["profile_start_timestamp_ms"] >= lower
                and r["profile_end_timestamp_ms"] <= upper]
    grouped = defaultdict(list)
    for bar in bars:
        if bar["close_timestamp_ms"] <= upper:
            grouped[str(datetime.fromtimestamp(bar["open_timestamp_ms"]/1000, NY).date())].append(bar)
    # Keep the frozen C1 ATR formula, not the newer Wilder seed convention.
    hourly = hourly_indicators(minutes)
    ends = hourly.end_ms.to_numpy(dtype="int64")
    hour_bars = minutes.set_index(pd.to_datetime(minutes.timestamp_ms, unit="ms", utc=True)).resample("1h").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"))
    hour_bars = hour_bars.dropna(subset=["open", "high", "low", "close"])
    # Initial partial source hour is not a complete historical candle.
    hour_bars = hour_bars[hour_bars.index >= pd.to_datetime((lower+3_599_999)//3_600_000*3_600_000, unit="ms", utc=True)]
    candidates, coverage_blocked, unavailable_atr, invalid_fill = [], 0, 0, 0
    raw = RawPrints([source_path])
    try:
        for profile in profiles:
            day, session = profile["session_day"], grouped[profile["session_day"]]
            signals, _ = evaluate_session(profile, session, symbol="HYPEUSDT")
            if not signals:
                continue
            signal = signals[0]
            asof = signal["feature_as_of_ms"]
            recent = [b for b in session if b["close_timestamp_ms"] <= asof]
            frame = pd.DataFrame([{**{k:b[k] for k in ("open", "high", "low", "close")},
                "timestamp_ms":b["open_timestamp_ms"], "volume":b["buy_volume"]+b["sell_volume"]} for b in recent])
            try:
                ctx = module.CandleContext(frame, as_of_ms=asof, base_timeframe="15m")
                ctx.candles("15m")
                if len(recent) != (asof-clock_ms(date.fromisoformat(day), 9))//BAR_MS:
                    raise module.CandleNotReady("Incomplete NY evaluation coverage")
                module.CandleContext(hour_bars.loc[:pd.to_datetime(asof, unit="ms", utc=True)],
                    as_of_ms=asof, base_timeframe="1h").candles("1h")
            except module.CandleNotReady:
                coverage_blocked += 1
                continue
            i = int(np.searchsorted(ends, asof, side="right"))-1
            atr = float(hourly.iloc[i].atr14) if i >= 0 else float("nan")
            # No unfinished/old hour and no unavailable ATR can enter C1.
            if not np.isfinite(atr) or atr <= 0 or not 0 <= asof-int(ends[i]) < 3_600_000:
                unavailable_atr += 1
                continue
            ref = raw.first(asof)
            if ref[0] >= len(raw.groups):
                invalid_fill += 1
                continue
            tick = raw.tick(ref)
            sign = 1 if signal["direction"] == "long" else -1
            if (tick["timestamp_ms"] > min(signal["entry_deadline_timestamp_ms"], asof+300_000)
                    or sign*(tick["price"]-signal["stop"]) <= 0):
                invalid_fill += 1
                continue
            last = recent[-4:]
            volume = sum(b["buy_volume"]+b["sell_volume"] for b in last)
            features = dict(directional_delta_imbalance=sign*sum(b["buy_volume"]-b["sell_volume"] for b in last)/volume if volume else None,
                directional_result_atr=sign*(last[-1]["close"]-last[0]["open"])/atr)
            signal.update(feature_available_at_ms=asof, decision_timestamp_ms=asof)
            candidates.append(dict(sample_id=signal["sample_id"], session_day=day, features=features, signal=signal))
    finally:
        raw.close()
    selected, folds, counts = c1_calibrated_signals(candidates)
    metadata.update(start_date=str(start), end_date=str(finish), calibration_folds=folds,
        counts={**counts, "coverage_blocked":coverage_blocked, "unavailable_atr":unavailable_atr,
                "invalid_first_fill":invalid_fill, "selected_signals":len(selected)},
        cache=cache)
    metadata['configuration_id'] = hashlib.sha256(json.dumps({k:metadata[k] for k in
        ('config','strategy_config','calibration','calibration_unknown_policy')}, sort_keys=True).encode()).hexdigest()
    dependencies = {Path(__file__), core}
    for name in ('model_development.phase_2.backtest','model_development.phase_2.backtest','model_development.phase_1.observe',
                 'model_development.observation_cache','utils.engine.adapters.paper_execution','indicator.legacy_preny',
                 'indicator.value_area','utils.market_session',
                 'model_development.models.trend_following_preny_profile_15m.legacy.runtime.baseline',
                 'model_development.models.trend_following_preny_profile_15m.legacy.runtime.gates',
                 'model_development.models.trend_following_preny_profile_15m.legacy.evaluate_plugs'):
        dependencies.add(Path(sys.modules[name].__file__))
    metadata['code'] = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(dependencies)}
    if source_identity([source_path]) != metadata["source"]:
        raise ValueError("Source changed during preparation")

    return dict(signals=selected, config=cfg, start_day=start, end_day=finish,
                metadata=metadata, timing_validator=module.validate_signal_timing)


def phase2_main() -> None:
    """Historical CLI compatibility; orchestration/output now belongs to Phase 2."""
    p = argparse.ArgumentParser(description="Phase 2 HYPE C1 full-history geometric backtest (no selection)")
    p.add_argument("--backtest", action="store_true", required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--shared-core", type=Path, required=True, help="Existing executor checkout containing CandleContext")
    p.add_argument("--cache-dir", type=Path, help="Shared validated minute cache, outside runs")
    p.add_argument("--initial-equity", type=float, default=1000.)
    p.add_argument("--risk-fraction", type=float, default=.005)
    p.add_argument("--fee-bps", type=float, default=4.)
    p.add_argument("--max-leverage", type=float, default=5.)
    a = p.parse_args()
    from model_development.phase_2.model import run_model
    a.model = Path(__file__)
    a.model_config = "{}"
    a.venue, a.product = "binance", "linear_quote_margined"
    a.quantity_unit, a.pnl_unit = "HYPE", "USDT"
    a.iteration, a.checkpoint, a.inspection_status = "ITER-001", None, "development"
    result = run_model(a)
    print(json.dumps({"output":str(a.output_dir.resolve()), "status":result["status"],
                      "metrics":result["metrics"], "open_positions_omitted":result["open_positions_omitted"]}, indent=2))



if __name__ == "__main__":
    main()
