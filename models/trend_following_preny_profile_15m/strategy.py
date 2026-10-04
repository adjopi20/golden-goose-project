"""Daily-profile signals using completed 15-minute price and delta observations.

Run on raw aggTrades or pass prepared 15-minute bars and completed profiles.
This module emits entry intentions; the execution engine owns actual fills/costs.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from trading_core.profiles import value_area

from trading_core.session import NY, clock_ms
from .runtime.baseline import BAR_MS, STRATEGY, PROFILE_WINDOWS, number, validate_bar, evaluate_session


def to_path_audit_signal(signal: dict, entry_tick: dict) -> dict:
    """Bind an engine-selected eligible raw fill to backtest_engine.path_audit.

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


if __name__ == "__main__":
    main()
