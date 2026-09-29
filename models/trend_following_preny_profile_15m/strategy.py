"""Daily-profile signals using completed 15-minute price and delta observations.

Run on raw aggTrades or pass prepared 15-minute bars and completed profiles.
This module emits entry intentions; the execution engine owns actual fills/costs.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BAR_MS = 15 * 60_000
STRATEGY = "trend_following_preny_profile_15m_v1"
PROFILE_WINDOWS = ("pre_ny",)


def clock_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(day, time(hour, minute), NY).timestamp() * 1000)


def number(row: dict, key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f"Non-finite {key}")
    return value


def validate_bar(row: dict) -> dict:
    bar = dict(row)
    for key in ("open", "high", "low", "close", "buy_volume", "sell_volume"):
        bar[key] = number(row, key)
    start, end = int(row["open_timestamp_ms"]), int(row["close_timestamp_ms"])
    local = datetime.fromtimestamp(start / 1000, NY)
    if end - start != BAR_MS or local.minute % 15 or local.second or local.microsecond:
        raise ValueError("Bars must span exactly 15 minutes, with exclusive end timestamps")
    if not (0 < bar["low"] <= min(bar["open"], bar["close"]) <=
            max(bar["open"], bar["close"]) <= bar["high"]):
        raise ValueError("Invalid OHLC")
    if min(bar["buy_volume"], bar["sell_volume"]) < 0:
        raise ValueError("Negative volume")
    bar.update(open_timestamp_ms=start, close_timestamp_ms=end)
    return bar


def evaluate_session(profile: dict, bars: list[dict], *, symbol: str,
                     confirmation_bars: int = 2) -> tuple[list[dict], list[dict]]:
    """Return (signals, decisions), using only completed bars up to 12:00 NY.

    Profile is today's completed 01:00-09:00 NY session only.
    Bars may include later data, which is ignored.
    At most one entry intention per session. No outcome fields are consumed.
    """
    if confirmation_bars not in (2, 3):
        raise ValueError("confirmation_bars must be 2 or 3")
    day = date.fromisoformat(profile["session_day"])
    start, cutoff = clock_ms(day, 9), clock_ms(day, 12)
    window = str(profile["profile_window"])
    if window not in PROFILE_WINDOWS:
        raise ValueError("Unknown profile_window")
    expected_start = clock_ms(day, 1)
    if (int(profile["profile_start_timestamp_ms"]) != expected_start
            or int(profile["profile_end_timestamp_ms"]) != start):
        raise ValueError("Profile window must end at today's 09:00 NY open")
    val, poc, vah = (number(profile, key) for key in ("val", "poc", "vah"))
    if not 0 < val <= poc <= vah:
        raise ValueError("Expected 0 < VAL <= POC <= VAH")
    selected = sorted((validate_bar(b) for b in bars
                       if start <= int(b["open_timestamp_ms"]) < cutoff),
                      key=lambda b: b["open_timestamp_ms"])
    stamps = [b["open_timestamp_ms"] for b in selected]
    if len(stamps) != len(set(stamps)):
        raise ValueError("Duplicate 15-minute bars")
    signals, decisions, run = [], [], []
    previous_end, previous_side = start, 0
    for b in selected:
        side = 1 if b["close"] > vah else -1 if b["close"] < val else 0
        gap = b["open_timestamp_ms"] != previous_end
        if gap or side == 0 or side != previous_side:
            run = []
        previous_end, previous_side = b["close_timestamp_ms"], side
        delta = b["buy_volume"] - b["sell_volume"]
        if side:
            run.append(b)
        deltas = [side * (r["buy_volume"] - r["sell_volume"]) for r in run]
        progress = side * (run[-1]["close"] - run[-2]["close"]) if len(run) > 1 else 0.
        body = side * (b["close"] - b["open"])
        fading = (len(run) >= 3 and deltas[-3] > deltas[-2] > deltas[-1] > 0)
        old_progress = side * (run[-2]["close"] - run[-3]["close"]) if len(run) >= 3 else 0.
        deteriorating = fading and 0 < progress < old_progress
        supported = (side != 0 and len(run) >= confirmation_bars and
                     sum(deltas) > 0 and deltas[-1] > 0 and body > 0 and
                     progress > 0 and not deteriorating)
        if not side:
            state, reason = "WAIT", "inside_value"
        elif len(run) < confirmation_bars:
            state, reason = "WAIT", "await_persistence"
        elif deteriorating:
            state, reason = "WAIT", "fading_delta_and_price_progress"
        elif not supported:
            state, reason = "WAIT", "effort_result_not_supported"
        else:
            state, reason = "ENTRY_READY", "supported_outside_value_progress"
        decision = dict(session_day=str(day), profile_window=window,
                        timestamp_ms=b["close_timestamp_ms"],
                        state=state, reason=reason, direction={1:"long", -1:"short", 0:None}[side],
                        close=b["close"], delta=delta, buy_volume=b["buy_volume"],
                        sell_volume=b["sell_volume"], consecutive_outside_closes=len(run),
                        directional_cumulative_delta=sum(deltas), directional_progress=progress,
                        directional_body=body, fading_directional_delta=fading,
                        deteriorating_result=deteriorating, data_gap_before_bar=gap)
        decisions.append(decision)
        if supported:
            entry_ms = b["close_timestamp_ms"]
            signals.append(dict(
                sample_id=f"{symbol}:{day}:{window}:{STRATEGY}", strategy=STRATEGY, symbol=symbol,
                session_day=str(day), profile_window=window, route="trend_following_preny_profile_15m",
                direction=decision["direction"],
                signal_timestamp_ms=entry_ms, feature_as_of_ms=entry_ms,
                entry_timestamp_ms=entry_ms, entry_eligible_timestamp_ms=entry_ms,
                entry_deadline_timestamp_ms=cutoff, entry_reference=b["close"],
                entry_price_is_reference=True, stop=poc, source_stop=poc,
                stop_model=f"{window}_poc", stops={"baseline":poc},
                force_exit_timestamp_ms=clock_ms(day + timedelta(days=1), 8, 59),
                exit_policy="initial_stop_or_time", context=decision))
            break
    if not signals:
        decisions.append(dict(session_day=str(day), profile_window=window,
                              timestamp_ms=cutoff, state="NO_TRADE",
                              reason="no_supported_breakout_by_cutoff"))
    return signals, decisions


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
            low, high = min(prices), max(prices)
            weights = [0.] * profile_bins
            width = (high-low) / profile_bins
            for price, qty in prices.items():
                index = min(profile_bins - 1, int((price-low)/width)) if width else 0
                weights[index] += qty
            poc_bin = max(range(profile_bins), key=lambda i: weights[i])
            left = right = poc_bin
            held = weights[poc_bin]
            target = sum(weights) * value_fraction
            while held < target and (left > 0 or right < profile_bins-1):
                if left == 0 or (right < profile_bins-1 and weights[right+1] > weights[left-1]):
                    right += 1
                    held += weights[right]
                else:
                    left -= 1
                    held += weights[left]
            profile_sets[window].append(dict(
                session_day=str(day), profile_window=window,
                profile_start_timestamp_ms=clock_ms(day, 1),
                profile_end_timestamp_ms=clock_ms(day, 9),
                val=low + left*width, poc=low + (poc_bin+.5)*width if width else low,
                vah=low + (right+1)*width if width else high,
                profile_trade_volume=sum(weights), profile_bins=profile_bins,
                value_fraction=value_fraction))
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
