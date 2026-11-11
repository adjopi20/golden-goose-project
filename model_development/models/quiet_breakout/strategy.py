"""Quiet Breakout research setup for the shared Phase 2 replay."""

import sys
from functools import partial
from pathlib import Path

import pandas as pd

from model_development.models.burst_breakout import strategy as burst
from model_development.observation_cache import candle_cache, digest
from model_development.phase_2.backtest import Config

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MINUTE_MS = 60_000
FLOW_PAIRS = {"1m_5m": (1, 5), "3m_12m": (3, 12), "5m_10m": (5, 10)}
ENTRY_FILTERS = {
    "risk_p85": ("E", 0.85), "risk_p90": ("E", 0.90), "risk_p95": ("E", 0.95),
    "path_p15": ("F", 0.15), "path_p10": ("F", 0.10), "path_p5": ("F", 0.05),
    "path_p2": ("F", 0.02), "path_p1": ("F", 0.01),
}


def breakout_setups(features, minutes, lookback_hours, start_ms, end_ms, *, bar_ms=HOUR_MS):
    """Use the same preceding completed candles for the profile and its price bounds."""
    hourly = minutes.groupby(minutes.timestamp_ms // bar_ms).agg(
        high=("high", "max"), low=("low", "min"),
    )
    hourly = hourly.reindex(range(hourly.index.min(), hourly.index.max() + 1))
    previous = hourly.shift()
    highs = previous.high.rolling(lookback_hours).max()
    lows = previous.low.rolling(lookback_hours).min()
    signals = []
    for row in features.itertuples(index=False):
        closing = int(row.closing)
        if not start_ms <= closing < end_ms:
            continue
        opening_hour = closing // bar_ms - 1
        high, low = highs.at[opening_hour], lows.at[opening_hour]
        if not low <= row.open <= high:
            continue
        if row.close > high:
            direction = "long"
        elif row.close < low:
            direction = "short"
        else:
            continue
        signals.append(dict(
            sample_id=f"quiet-{closing}", direction=direction,
            stop=float(row.poc), entry_reference=float(row.close),
            feature_available_at_ms=closing,
            profile_high=float(high), profile_low=float(low),
            breakout_body=float(row.body), breakout_volume=float(row.volume),
        ))
    return signals


def qualify_hourly_setups(setups, minutes, hourly_volume, volume_thresholds, replay_end, continuation):
    """Advance a breakout's flow anchor only after its continuation bar has closed."""
    prices = minutes.set_index("timestamp_ms")
    qualified = []
    for setup in setups:
        breakout = setup["feature_available_at_ms"]
        sign = 1 if setup["direction"] == "long" else -1
        first = prices.loc[breakout - HOUR_MS:breakout - 1]
        horizons = (15, 60) if continuation == "structure_2_candles" else (60,)
        for horizon in horizons:
            closing = breakout + horizon * MINUTE_MS
            if closing >= replay_end:
                break
            second = prices.loc[breakout:closing - 1]
            if len(second) != horizon:
                continue
            high, low = second.high.max(), second.low.min()
            stop_touched = low <= setup["stop"] if sign == 1 else high >= setup["stop"]
            if stop_touched:
                break
            close = float(second.close.iloc[-1])
            if continuation == "volume_p75_twice":
                hour = breakout // HOUR_MS
                if not hourly_volume.at[hour] >= volume_thresholds.at[hour]:
                    continue
                route = "second_hour_volume"
            else:
                if sign * (high - first.high.max()) <= 0 or sign * (low - first.low.min()) <= 0:
                    continue
                if horizon == 15:
                    body = sign * (close - second.open.iloc[0])
                    if body <= 0 or body < setup["breakout_body"]:
                        continue
                route = "first_15m_structure" if horizon == 15 else "second_hour_structure"
            qualified.append(dict(
                setup, feature_available_at_ms=closing, entry_reference=close,
                first_breakout_close_ms=breakout, setup_qualified_at_ms=closing, continuation_route=route,
            ))
            break
    return qualified


def filter_confirmed_entries(entries, trace, minutes, window, entry_filter, *, audit_prefix="entry_filter"):
    """Apply one filter to the baseline's first confirmation for each setup."""
    entries = sorted(entries, key=lambda entry: entry["decision_timestamp_ms"])
    family, quantile = ENTRY_FILTERS[entry_filter]
    risk_filter = family == "E"
    if risk_filter:
        scores = [
            abs(entry["entry_reference"] - entry["stop"]) / entry["entry_reference"]
            for entry in entries
        ]
    else:
        prices = minutes.set_index("timestamp_ms")[["open", "close"]]
        scores = []
        for entry in entries:
            end = entry["decision_timestamp_ms"]
            path = prices.loc[end - window * MINUTE_MS:end - MINUTE_MS]
            if len(path) != window:
                raise ValueError("Confirmed flow window is missing minute prices")
            start = path.open.iloc[0]
            travel = abs(path.close.iloc[0] - start) + path.close.diff().abs().sum()
            scores.append(abs(path.close.iloc[-1] - start) / travel if travel else 0.0)
    thresholds = pd.Series(scores, dtype=float).shift().rolling(100, min_periods=30).quantile(quantile)
    audit = {}
    accepted = []
    for entry, score, threshold in zip(entries, scores, thresholds):
        warming_up = pd.isna(threshold)
        passes = score <= threshold if risk_filter else score >= threshold
        if warming_up or passes:
            accepted.append(entry)
            if risk_filter and not warming_up:
                entry["max_entry_stop_fraction"] = float(threshold)
        status = "warmup" if warming_up else "passed" if passes else "rejected"
        audit[entry["sample_id"], entry["decision_timestamp_ms"]] = (score, threshold, status)
    evidence = pd.DataFrame.from_dict(
        audit, orient="index", columns=[f"{audit_prefix}_{field}" for field in ("score", "threshold", "status")],
    )
    evidence.index = pd.MultiIndex.from_tuples(audit, names=["sample_id", "assessment_timestamp_ms"])
    trace = trace.join(evidence, on=["sample_id", "assessment_timestamp_ms"]) if audit else trace
    return accepted, trace, dict(
        name=entry_filter, historical_confirmations=100, minimum_history=30,
        warmup_passes=min(30, len(entries)), passed=len(accepted), rejected=len(entries) - len(accepted),
        quantile=quantile,
    )


def pyramid_position_policy(minutes, start_ms, replay_end, step, window, request, source_digest, *, balance_mode):
    """Prepare independent continuation observations; bind them to an actual fill in replay."""
    bars = minutes.groupby(minutes.timestamp_ms // HOUR_MS * HOUR_MS).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        buy=("buy_volume", "sum"), sell=("sell_volume", "sum"),
    )
    bars = bars.reindex(range(int(bars.index.min()), int(bars.index.max()) + HOUR_MS, HOUR_MS))
    body = (bars.close - bars.open).abs()
    impulses = body.gt(0) & body.ge(body.shift().rolling(24).quantile(0.85))
    fast_trigger = balance_mode == "local_2h_fast"
    local_balance = balance_mode in ("local_2h", "local_2h_fast")
    if fast_trigger:
        step_ms = step * MINUTE_MS
        by_close = minutes.groupby((minutes.timestamp_ms // step_ms + 1) * step_ms).close
        trigger_closes = by_close.last().where(by_close.size().eq(step))
        trigger_closes = trigger_closes.reindex(
            range(int(trigger_closes.index.min()), int(trigger_closes.index.max()) + step_ms, step_ms)
        )
    else:
        step_ms = HOUR_MS
        trigger_closes = bars.close.rename(index=lambda opening: opening + HOUR_MS)
    balance_hours = 2 if local_balance else 3
    max_span = 1.0 if local_balance else 0.5
    candidates = []
    for opening in bars.index[impulses & (bars.index >= start_ms)]:
        impulse = bars.loc[opening]
        side = 1 if impulse.close > impulse.open else -1
        span = impulse.high - impulse.low
        balance = bars.loc[opening + HOUR_MS:opening + balance_hours * HOUR_MS]
        if len(balance) != balance_hours or balance.close.isna().any():
            continue
        high, low = float(balance.high.max()), float(balance.low.min())
        if local_balance:
            midpoint = (impulse.high + impulse.low) / 2
            in_zone = (side * (balance.close - midpoint)).ge(0).all()
            trigger, invalidation = (high, low) if side == 1 else (low, high)
        elif side == 1:
            in_zone = balance.close.between(impulse.high - span / 3, impulse.high).all()
            trigger, invalidation = max(high, impulse.high), low
        else:
            in_zone = balance.close.between(impulse.low, impulse.low + span / 3).all()
            trigger, invalidation = min(low, impulse.low), high
        if not in_zone or high - low > span * max_span or balance.low.max() > balance.high.min():
            continue
        first_continuation = opening + (balance_hours + 1) * HOUR_MS
        for timestamp, close in trigger_closes.loc[
            first_continuation + step_ms:first_continuation + 3 * HOUR_MS
        ].items():
            closing = int(timestamp)
            if closing >= replay_end or pd.isna(close) or side * (close - invalidation) < 0:
                break
            if side * (close - trigger) > 0:
                candidates.append(dict(
                    sample_id=f"pyramid-{opening}-{closing}", direction="long" if side == 1 else "short",
                    feature_available_at_ms=closing, entry_reference=float(close),
                    stop=float(invalidation), trigger_price=float(trigger),
                    impulse_open_ms=int(opening), impulse_close=float(impulse.close),
                    balance_aggressive_buy=float(balance.buy.sum()),
                    balance_aggressive_sell=float(balance.sell.sum()),
                ))
                break

    by_time = {}
    flow_cache = None
    if candidates:
        trace, flow_cache = burst._cached_flow_assessment(
            minutes, candidates, 60, request.cache_dir, source_digest,
            step_minutes=step, window_minutes=window,
        )
        confirmed, _ = burst.confirm_flow_entries(trace, candidates, replay_end, window_minutes=window)
        for signal in confirmed:
            by_time.setdefault(signal["decision_timestamp_ms"], []).append(signal)

    def decide(position, boundary, reader):
        side = 1 if position["direction"] == "long" else -1
        initial_distance = abs(position["entry_price"] - position["initial_stop"])
        for signal in reversed(by_time.get(boundary, ())):
            if (position["direction"] == signal["direction"]
                    and position["entry_timestamp_ms"] <= signal["impulse_open_ms"]
                    and side * (signal["impulse_close"] - position["entry_price"]) >= initial_distance):
                return {"add": dict(
                    signal, max_quantity_fraction=0.8, max_initial_risk_fraction=0.8,
                    evidence={field: signal[field] for field in (
                        "impulse_open_ms", "impulse_close", "balance_aggressive_buy", "balance_aggressive_sell",
                        "confirmation_mechanisms",
                    )},
                )}
        return None

    return dict(position_boundaries=by_time, position_decision=decide), dict(
        candidate_count=len(candidates), confirmed_signals=sum(map(len, by_time.values())),
        balance_mode=balance_mode, flow_cache=flow_cache,
    )


def stop_with_profit_add(position, tick, *, stop_policy):
    """Arm one add when the original position first locks at least +1R."""
    update = stop_policy(position, tick)
    if update is None or not position["risk_lock_r"] < 1 <= update["risk_lock_r"]:
        return update
    decision = tick["timestamp_ms"]
    force_exit = position["force_exit_timestamp_ms"]
    update["add"] = dict(
        sample_id=f'{position["sample_id"]}-lock-1r',
        feature_as_of_ms=decision, feature_available_at_ms=decision,
        decision_timestamp_ms=decision, entry_eligible_timestamp_ms=decision,
        entry_deadline_timestamp_ms=min(decision + MINUTE_MS - 1, force_exit - 1),
        force_exit_timestamp_ms=force_exit,
        entry_reference=tick["price"], trigger_price=update["stop"],
        max_quantity_fraction=0.8, max_initial_risk_fraction=0.8,
        evidence=dict(
            trigger_agg_trade_id=tick["agg_trade_id"], trigger_price=tick["price"],
            risk_lock_r=update["risk_lock_r"],
        ),
    )
    return update


def prepare_backtest(request):
    settings = request.model_config
    start = pd.Timestamp(settings["start_date"], tz="UTC")
    end = pd.Timestamp(settings["end_date"], tz="UTC") + pd.Timedelta(days=1)
    if start != start.normalize() or end != end.normalize() or end <= start:
        raise ValueError("Expected ordered inclusive UTC dates")
    start_ms, end_ms = start.value // 1_000_000, end.value // 1_000_000
    timeframe = settings.get("profile_timeframe", "1h")
    if timeframe not in ("1h", "1d"):
        raise ValueError("Quiet Breakout supports 1h or 1d profiles")
    daily = timeframe == "1d"
    position_exit = settings.get("position_exit", "baseline")
    if position_exit not in ("baseline", "hourly-atr") or (daily and position_exit == "hourly-atr"):
        raise ValueError("Hourly ATR exit requires a Quiet Breakout 1H setup")
    hourly_atr = position_exit == "hourly-atr"
    pyramid_add = settings.get("pyramid_add", False)
    if type(pyramid_add) is not bool or (pyramid_add and (daily or hourly_atr)):
        raise ValueError("Pyramiding requires the hourly setup with baseline exits")
    initiative_risk_2x = settings.get("initiative_risk_2x", False)
    risk_multiplier = settings.get("initiative_risk_multiplier", 2 if initiative_risk_2x else 1)
    if (type(initiative_risk_2x) is not bool or type(risk_multiplier) not in (int, float)
            or not 1 <= risk_multiplier or (initiative_risk_2x and risk_multiplier != 2)):
        raise ValueError("Invalid initiative risk multiplier")
    balance_mode = settings.get("pyramid_balance", "strict_3h")
    if balance_mode not in ("strict_3h", "local_2h", "local_2h_fast", "lock_1r") or (
        balance_mode != "strict_3h" and not pyramid_add
    ):
        raise ValueError("pyramid_balance requires pyramiding and a supported balance mode")
    profit_add = pyramid_add and balance_mode == "lock_1r"
    if risk_multiplier > 1 and (daily or hourly_atr or (pyramid_add and not profit_add)
                                or not 0 < request.risk_fraction * risk_multiplier < 1):
        raise ValueError("Initiative sizing requires the hourly baseline and a supported add mode")
    bar_ms = DAY_MS if daily else HOUR_MS
    lookback = settings["lookback_days" if daily else "lookback_hours"]
    flow = settings.get("flow_timeframes")
    risk = settings["risk_management"]

    exit_hours = settings.get("risk_exit_hours")
    if (lookback not in (12, 16, 24) or (flow is not None if daily else flow not in FLOW_PAIRS)
            or risk not in ("rm-a", "rm-d", "rm-e")
            or (exit_hours is not None if hourly_atr else exit_hours not in ((336,) if daily else (24, 48)))):
        raise ValueError("Unsupported Quiet Breakout grid combination")
    daily_limit = settings.get("one_entry_per_utc_day", False)
    if type(daily_limit) is not bool:
        raise ValueError("one_entry_per_utc_day must be boolean")
    require_volume_p75 = settings.get("require_volume_p75", False)
    if type(require_volume_p75) is not bool:
        raise ValueError("require_volume_p75 must be boolean")
    volume_percentile = settings.get("breakout_volume_percentile", 75 if require_volume_p75 else None)
    if volume_percentile is not None and (
        type(volume_percentile) is not int or volume_percentile not in (75, 90)
        or (volume_percentile != 75 and (require_volume_p75 or daily))
    ):
        raise ValueError("Volume P90 requires the hourly setup without the legacy P75 flag")
    if require_volume_p75 and volume_percentile != 75:
        raise ValueError("Conflicting breakout volume filters")
    require_volume_p75 = volume_percentile == 75
    continuation = settings.get("breakout_continuation")
    if continuation is not None and (
        daily or continuation not in ("volume_p75_twice", "structure_2_candles") or not require_volume_p75
    ):
        raise ValueError("Breakout continuation requires the hourly P75 baseline")
    entry_filter = settings.get("entry_filter")
    if entry_filter is not None and (not isinstance(entry_filter, str) or entry_filter not in ENTRY_FILTERS
                                     or daily or not daily_limit or not require_volume_p75):
        raise ValueError("Entry filter requires hourly variation C and a supported risk or path filter")
    additional_path_filter = settings.get("additional_path_filter")
    if additional_path_filter is not None and (
        not isinstance(additional_path_filter, str)
        or additional_path_filter not in ENTRY_FILTERS
        or ENTRY_FILTERS[additional_path_filter][0] != "F"
        or entry_filter is None
        or ENTRY_FILTERS[entry_filter][0] != "E"
    ):
        raise ValueError("Additional path filter requires a primary risk filter")
    filter_info = path_filter_info = None
    step, window = (0, 0) if daily else FLOW_PAIRS[flow]
    minutes, cache = candle_cache([request.input], request.cache_dir, id_policy=request.id_policy)
    if (cache["source_start_ms"] > start_ms - (lookback + 1) * bar_ms
            or cache["source_end_ms"] < end_ms - MINUTE_MS):
        raise ValueError("Source must cover the replay and profile warmup")
    replay_end = min(end_ms, cache["source_end_ms"] + 1)

    profile_cache = {}
    flow_cache = None
    if daily:
        def select(bars):
            prior = bars.shift()
            high = prior.high.rolling(lookback).max()
            low = prior.low.rolling(lookback).min()
            breakout = bars.open.between(low, high) & (bars.close.gt(high) | bars.close.lt(low))
            if require_volume_p75:
                breakout &= bars.volume.ge(prior.volume.rolling(lookback).quantile(0.75))
            return breakout

        features = burst._daily_profile_features(
            request, minutes, cache["source_start_ms"], start_ms, replay_end,
            lookback, profile_cache, select=select,
        )
        setups = breakout_setups(features, minutes, lookback, start_ms, replay_end, bar_ms=DAY_MS)
        entries = []
        for setup in setups:
            closing = setup["feature_available_at_ms"]
            entries.append(dict(
                setup, breakout_timestamp_ms=closing, feature_as_of_ms=closing,
                session_day=pd.Timestamp(closing, unit="ms", tz="UTC").date().isoformat(),
                decision_timestamp_ms=closing, entry_eligible_timestamp_ms=closing,
                entry_deadline_timestamp_ms=closing + DAY_MS - 1,
            ))
    else:
        burst._profile_signals(
            request.input, minutes, start_ms, replay_end, burst=True,
            cache_dir=request.cache_dir, cache_info=profile_cache, lookback_hours=lookback,
        )
        features = pd.read_parquet(Path(profile_cache["path"]) / "profile_features.parquet")
        setups = breakout_setups(features, minutes, lookback, start_ms, replay_end)
        flow_setups = setups
        if volume_percentile is not None:
            hourly_volume = minutes.groupby(minutes.timestamp_ms // HOUR_MS).volume.sum()
            hourly_volume = hourly_volume.reindex(range(hourly_volume.index.min(), hourly_volume.index.max() + 1))
            thresholds = hourly_volume.shift().rolling(lookback).quantile(volume_percentile / 100)
            setups = [
                setup for setup in setups
                if setup["breakout_volume"] >= thresholds.at[setup["feature_available_at_ms"] // HOUR_MS - 1]
            ]
        if continuation is not None:
            setups = qualify_hourly_setups(setups, minutes, hourly_volume, thresholds, replay_end, continuation)
            flow_setups = setups
        trace, flow_cache = burst._cached_flow_assessment(
            minutes, flow_setups, 60, request.cache_dir, cache["sha256"],
            step_minutes=step, window_minutes=window,
        )
        if setups is not flow_setups:
            trace = trace.loc[trace.sample_id.isin({setup["sample_id"] for setup in setups})].copy()
        entries, trace = burst.confirm_flow_entries(trace, setups, replay_end, window_minutes=window)
        if continuation is not None:
            evidence = {setup["sample_id"]: setup for setup in setups}
            for field in ("first_breakout_close_ms", "setup_qualified_at_ms", "continuation_route"):
                trace[field] = trace.sample_id.map({key: setup[field] for key, setup in evidence.items()})
            for entry in entries:
                entry["breakout_timestamp_ms"] = entry["first_breakout_close_ms"]
        confirmed_count = len(entries)
        if entry_filter is not None:
            confirmed = entries
            entries, trace, filter_info = filter_confirmed_entries(confirmed, trace, minutes, window, entry_filter)
            if additional_path_filter is not None:
                path_passed, trace, path_filter_info = filter_confirmed_entries(
                    confirmed, trace, minutes, window, additional_path_filter, audit_prefix="additional_path_filter",
                )
                allowed = {entry["sample_id"] for entry in path_passed}
                entries = [entry for entry in entries if entry["sample_id"] in allowed]
                path_filter_info["passed_both"] = len(entries)
        trace.to_parquet(request.output_dir / "orderflow.parquet", index=False)

    boosted_entries = 0
    if risk_multiplier > 1:
        for entry in entries:
            mechanisms = entry["confirmation_mechanisms"]
            if "initiative_candidate" in mechanisms and "exhaustion_candidate" not in mechanisms:
                entry["entry_risk_fraction"] = request.risk_fraction * risk_multiplier
                boosted_entries += 1

    def force_exit(eligible):
        return sys.maxsize if hourly_atr else eligible + exit_hours * HOUR_MS

    for entry in entries:
        if not hourly_atr:
            entry["exit_after_fill_ms"] = exit_hours * HOUR_MS
        entry["force_exit_timestamp_ms"] = force_exit(entry["entry_eligible_timestamp_ms"])


    def validate_timing(signal):
        eligible = signal["entry_eligible_timestamp_ms"]
        flow_anchor = signal.get("setup_qualified_at_ms", signal["breakout_timestamp_ms"])
        elapsed = eligible - flow_anchor
        valid_entry = (elapsed == 0 and eligible % DAY_MS == 0 if daily else
                       window * MINUTE_MS <= elapsed <= HOUR_MS and elapsed % (step * MINUTE_MS) == 0)
        if (not valid_entry
                or eligible != signal["decision_timestamp_ms"]
                or signal["force_exit_timestamp_ms"] != force_exit(eligible)):
            raise ValueError("Invalid Quiet Breakout decision or exit timing")

    partial_exit = risk in ("rm-d", "rm-e")
    variation = "D" if daily else ("C" if require_volume_p75 else ("B" if daily_limit else "A"))
    if entry_filter is not None:
        variation = ENTRY_FILTERS[entry_filter][0]
    if volume_percentile == 90:
        variation = "volume_p90"
    if continuation is not None:
        variation = continuation
    code_modules = [burst]
    exit_options = {}
    if hourly_atr:
        from model_development.models.burst_breakout_longer_trend import strategy as longer

        first_hour = max(start_ms - 30 * DAY_MS,
                         (cache["source_start_ms"] + HOUR_MS - 1) // HOUR_MS * HOUR_MS)
        if start_ms - first_hour < (longer.DAILY_ATR_WINDOW + 1) * HOUR_MS:
            raise ValueError("Hourly ATR requires at least 15 warmup hours")
        exit_actions = []
        exit_options = dict(
            position_boundaries=range(start_ms + HOUR_MS, replay_end, HOUR_MS),
            position_decision=longer._atr_exit_policy(
                minutes, first_hour, replay_end, exit_actions, bar_ms=HOUR_MS,
            ),
        )
        code_modules.extend((longer, longer.atr_indicator, longer.atr_common))
        atr_metadata = dict(
            atr_window_hours=longer.DAILY_ATR_WINDOW, atr_multiplier=longer.DAILY_ATR_MULTIPLIER,
            atr_seed_start_ms=first_hour, atr_convention=longer.atr_common.TECHNICAL_CONVENTION,
            position_exit_actions=exit_actions,
        )
    else:
        atr_metadata = {}

    risk_manager = None if hourly_atr else (burst.rm_e_stop if risk == "rm-e" else burst.rm_a_stop)
    pyramid = {}
    if profit_add:
        risk_manager = partial(stop_with_profit_add, stop_policy=risk_manager)
        pyramid = dict(balance_mode=balance_mode, trigger_locked_r=1,
                       max_quantity_fraction=0.8, max_initial_risk_fraction=0.8)
    elif pyramid_add:
        exit_options, pyramid = pyramid_position_policy(
            minutes, start_ms, replay_end, step, window, request, cache["sha256"],
            balance_mode=balance_mode,
        )

    return dict(
        **exit_options,
        signals=entries,
        config=Config(
            request.initial_equity, request.risk_fraction, request.fee_bps,
            request.max_leverage, tp1_r=1.0 if partial_exit else None,
            tp1_fraction=0.1 if partial_exit else None,
        ),
        start_day=start.date(), end_day=(end - pd.Timedelta(days=1)).date(),
        replay_window=(start_ms, replay_end),
        timing_validator=validate_timing,
        risk_manager=risk_manager,
        one_entry_per_utc_day=daily_limit,
        metadata=dict(
            model_id="quiet_breakout", strategy_config=settings, candidate_count=len(setups),
            variation=variation, entry_filter=filter_info,
            **(dict(additional_path_filter=path_filter_info) if path_filter_info is not None else {}),
            **(dict(initiative_risk_sizing=dict(multiplier=risk_multiplier, boosted_intentions=boosted_entries))
               if risk_multiplier > 1 else {}),
            **(dict(pyramid_add=pyramid) if pyramid_add else {}),
            one_entry_per_utc_day=daily_limit, require_volume_p75=require_volume_p75,
            **(dict(breakout_volume_percentile=volume_percentile) if "breakout_volume_percentile" in settings else {}),
            **(dict(breakout_continuation=continuation) if continuation is not None else {}),
            profile_timeframe=timeframe, risk_exit_hours=exit_hours,
            flow_entry_confirmation=None if daily else dict(
                baseline="burst_static", horizon_minutes=60, entry_gate=False,
                confirmed_signals=confirmed_count,
            ),
            profile_cache=profile_cache, flow_cache=flow_cache, position_exit=position_exit,
            exit_clock="hourly ATR stop only" if hourly_atr else "actual_entry_fill",
            **atr_metadata,
            code={str(Path(module.__file__).resolve()): digest(module.__file__) for module in code_modules},
        ),
    )
