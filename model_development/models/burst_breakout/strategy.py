"""Burst breakout research with hourly expansion and minute-level flow confirmation."""

from pathlib import Path

import numpy as np
import pandas as pd

# Historical cache namespaces and checkpoint IDs survive model renaming.

def detect_breakout_entries(candles: pd.DataFrame, *, lookback_hours=12) -> pd.DataFrame:
    """Signal at candle close, with entry at the next observed hourly open."""
    if type(lookback_hours) is not int or lookback_hours < 2:
        raise ValueError("lookback_hours must be an integer of at least 2")
    index = candles.index
    if not isinstance(index, pd.DatetimeIndex) or index.hasnans:
        raise ValueError("Expected a DatetimeIndex")
    if not index.to_series().diff().iloc[1:].eq(pd.Timedelta(hours=1)).all():
        raise ValueError("Expected an hourly grid with explicit empty hours")

    prices = candles[["open", "high", "low", "close"]].astype(float)
    observed = prices.notna().any(axis=1)
    opens, high, low, close = (prices[name] for name in prices)
    if not np.isfinite(prices.loc[observed].to_numpy()).all():
        raise ValueError("Expected finite OHLC for measured candles")
    if ((low <= 0) | (opens < low) | (opens > high) | (close < low) | (close > high)).any():
        raise ValueError("Expected positive OHLC with open and close within high/low")

    volume = candles["volume"].astype(float).where(observed)
    if not np.isfinite(volume[observed]).all() or (volume < 0).any():
        raise ValueError("Expected finite, nonnegative volume")
    signed_body = close - opens
    body = signed_body.abs()
    body_limit = body.shift().rolling(lookback_hours).quantile(0.85)
    volume_limit = volume.shift().rolling(lookback_hours).quantile(0.85)
    confirmed = (body > body_limit) & (volume > volume_limit)
    side = np.sign(signed_body.where(confirmed, 0)).astype(int)

    # Prior highs and lows set the protective stop only.
    prior_low = low.shift().rolling(lookback_hours).min()
    prior_high = high.shift().rolling(lookback_hours).max()
    entry_side = side.shift(fill_value=0).where(observed, 0)
    return pd.DataFrame({
        "breakout_side": side,
        "breakout_stop": prior_low.where(side.eq(1), prior_high.where(side.eq(-1))),
        "entry_side": entry_side,
        "entry_price": opens.where(entry_side.ne(0)),
    })


def simulate_trades(candles: pd.DataFrame) -> pd.DataFrame:
    """Replay one position at a time using hourly OHLC and fixed range stops."""
    signals = detect_breakout_entries(candles)
    bars = candles[["open", "high", "low"]].astype(float)
    entries = signals.assign(
        entry_stop=signals["breakout_stop"].shift(),
    )
    trades = []
    position = None
    for bar, entry in zip(bars.itertuples(), entries.itertuples()):
        if pd.isna(bar.open):
            continue
        if position is None and entry.entry_side:
            stop = entry.entry_stop
            position = dict(
                entry_time=bar.Index, side=entry.entry_side,
                entry_price=entry.entry_price, stop_price=stop,
                exit_time=pd.NaT, exit_price=np.nan, exit_reason=None,
            )
            trades.append(position)

        if position is None:
            continue
        stop = position["stop_price"]
        if bar.Index - position["entry_time"] >= pd.Timedelta(hours=24):
            price, reason = bar.open, "time"
        elif position["side"] == 1 and bar.low <= stop:
            price, reason = min(bar.open, stop), "stop"
        elif position["side"] == -1 and bar.high >= stop:
            price, reason = max(bar.open, stop), "stop"
        else:
            continue
        position.update(exit_time=bar.Index, exit_price=price, exit_reason=reason)
        position = None

    return pd.DataFrame(trades, columns=[
        "entry_time", "side", "entry_price", "stop_price",
        "exit_time", "exit_price", "exit_reason",
    ])


def _profile_signals(path, minutes, start, end, *, burst=False, cache_dir=None, cache_info=None, lookback_hours=None):
    """Freeze the profile and aggression thresholds at each hour boundary."""
    from collections import deque
    from functools import lru_cache

    from indicator.volume_profile import build_basic_volume_profile
    from model_development.observation_cache import RawPrints

    lookback = (24 if burst else 12) if lookback_hours is None else lookback_hours
    if type(lookback) is not int or (lookback not in (12, 16, 24) if burst else lookback != 12):
        raise ValueError("Profile burst requires 12/16/24 hours; profile aggression requires 12 hours")
    feature_cache = None
    if burst and cache_dir is not None:
        import inspect
        import json
        import hashlib
        import sys
        from model_development.observation_cache import digest, source_identity
        from indicator import volume_profile

        identity = dict(
            source=source_identity([path]), start=start, end=end,
            builder=hashlib.sha256(inspect.getsource(_profile_signals).encode()).hexdigest(),
            profile_source=digest(volume_profile.__file__),
            raw_reader_source=digest(sys.modules[RawPrints.__module__].__file__),
            numpy=np.__version__, pandas=pd.__version__,
            lookback_hours=lookback, bins=50, value_fraction=0.70,
        )
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        feature_cache = cache_dir / "breakout_kontol" / key
        metadata_path = feature_cache / "cache.json"
        if metadata_path.exists():
            saved = json.loads(metadata_path.read_text(encoding="utf-8"))
            data_path = feature_cache / "profile_features.parquet"
            if saved["identity"] != identity or digest(data_path) != saved["sha256"]:
                raise ValueError("Profile feature cache identity/checksum mismatch")
            if cache_info is not None:
                cache_info.update(path=str(feature_cache), reused=True, rows=saved["rows"])
            return _burst_signals_from_features(pd.read_parquet(data_path), end)

    hour = 3_600_000
    minute = 60_000
    baseline_minutes = 30 * 24 * 60
    columns = ["buy_volume", "sell_volume"]
    if not burst:
        if not set(columns).issubset(minutes.columns):
            raise ValueError("Profile breakout requires Binance aggressor-side data")
        grid = np.arange(start - baseline_minutes * minute, end, minute)
        flow = minutes.set_index("timestamp_ms")[columns].reindex(grid)
        thresholds = flow.shift().rolling(
            baseline_minutes, min_periods=27 * 24 * 60
        ).quantile(0.85)

    import time
    last_progress = time.monotonic()
    reader = RawPrints([path])
    signals = []
    features = []
    first_signal_hour = start - hour if burst else start
    first_profile_hour = first_signal_hour - lookback * hour
    history = deque(maxlen=lookback)
    last_eligibility = None

    @lru_cache(maxsize=2)
    def arrays(group):
        source, index, _, _ = reader.groups[group]
        names = ["timestamp", "price", "qty"]
        if not burst:
            names.append("is_buyer_maker")
        table = source.read_row_group(index, columns=names)
        return tuple(table[name].combine_chunks().to_numpy(zero_copy_only=False) for name in names)

    try:
        group = int(np.searchsorted(reader.ends, first_profile_hour))
        for opening in range(first_profile_hour, end, hour):
            if feature_cache is not None and time.monotonic() - last_progress >= 20:
                print(f"Profile cache: {len(features):,} hourly profiles through "
                      f"{pd.Timestamp(opening, unit='ms', tz='UTC')}", flush=True)
                last_progress = time.monotonic()
            closing = opening + hour
            parts = []
            while group < len(reader.groups) and reader.groups[group][2] < closing:
                data = arrays(group)
                lo, hi = np.searchsorted(data[0], [opening, closing])
                if lo < hi:
                    parts.append(tuple(values[lo:hi] for values in data))
                if reader.ends[group] >= closing:
                    break
                group += 1
            current = tuple(np.concatenate(values) for values in zip(*parts)) if parts else None
            if first_signal_hour <= opening and current is not None and len(history) == lookback and all(
                previous is not None for previous in history
            ):
                profile = build_basic_volume_profile(pd.DataFrame({
                    "price": np.concatenate([previous[1] for previous in history]),
                    "qty": np.concatenate([previous[2] for previous in history]),
                }), n_bins=50, value_area_pct=0.70)
                if burst:
                    features.append(dict(
                        closing=closing, open=float(current[1][0]), close=float(current[1][-1]),
                        body=float(abs(current[1][-1] - current[1][0])),
                        volume=float(current[2].sum()),
                        body_p85=float(np.quantile([abs(bar[1][-1] - bar[1][0]) for bar in history], 0.85)),
                        volume_p85=float(np.quantile([bar[2].sum() for bar in history], 0.85)),
                        val=profile["val"], vah=profile["vah"], poc=profile["poc_price"],
                    ))
                    history.append(current)
                    continue
                limits = thresholds.loc[opening]
                for stamp, price, side, flow_qty, limit in _profile_crossings(
                    current, history[-1], profile["val"], profile["vah"], limits
                ):
                    eligible = stamp + 1
                    if eligible >= end or eligible == last_eligibility:
                        continue
                    last_eligibility = eligible
                    signals.append(dict(
                        sample_id=f"profile-{eligible}",
                        session_day=pd.Timestamp(eligible, unit="ms", tz="UTC").date().isoformat(),
                        direction="long" if side > 0 else "short",
                        stop=profile["poc_price"],
                        entry_reference=price,
                        feature_as_of_ms=stamp, feature_available_at_ms=stamp,
                        decision_timestamp_ms=stamp, entry_eligible_timestamp_ms=eligible,
                        entry_deadline_timestamp_ms=eligible + minute - 1,
                        force_exit_timestamp_ms=eligible + 48 * hour,
                        profile_val=profile["val"], profile_vah=profile["vah"], profile_poc=profile["poc_price"],
                        aggressive_qty_60s=flow_qty, aggressive_qty_p85=limit,
                    ))
            history.append(current)
    finally:
        arrays.cache_clear()
        reader.close()
    if not burst:
        return signals
    table = pd.DataFrame(features, columns=[
        "closing", "open", "close", "body", "volume", "body_p85", "volume_p85",
        "val", "vah", "poc",
    ])
    if feature_cache is not None:
        import json
        import os
        import uuid
        from model_development.observation_cache import digest, source_identity

        if source_identity([path]) != identity["source"]:
            raise ValueError("Source changed during profile cache build")
        feature_cache.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        temporary_data = feature_cache / (token + ".parquet")
        temporary_meta = feature_cache / (token + ".json")
        try:
            table.to_parquet(temporary_data, index=False)
            saved = dict(identity=identity, rows=len(table), sha256=digest(temporary_data))
            temporary_meta.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            os.replace(temporary_data, feature_cache / "profile_features.parquet")
            os.replace(temporary_meta, feature_cache / "cache.json")
        finally:
            temporary_data.unlink(missing_ok=True)
            temporary_meta.unlink(missing_ok=True)
        if cache_info is not None:
            cache_info.update(path=str(feature_cache), reused=False, rows=len(table))
    return _burst_signals_from_features(table, end)


def _burst_signals_from_features(features, end):
    """Apply current setup rules to reusable, source-backed profile features."""
    signals = []
    hour = 3_600_000
    for row in features.itertuples():
        side = int(row.close > row.vah) - int(row.close < row.val)
        if (row.val >= row.vah or not side or row.closing >= end
                or row.body <= row.body_p85 or row.volume <= row.volume_p85):
            continue
        closing = int(row.closing)
        signals.append(dict(
            sample_id=f"profile-burst-{closing}",
            session_day=pd.Timestamp(closing, unit="ms", tz="UTC").date().isoformat(),
            direction="long" if side > 0 else "short",
            stop=float(row.poc), entry_reference=float(row.close),
            feature_as_of_ms=closing, feature_available_at_ms=closing,
            decision_timestamp_ms=closing, entry_eligible_timestamp_ms=closing,
            entry_deadline_timestamp_ms=closing + hour - 1,
            force_exit_timestamp_ms=closing + 24 * hour, exit_after_fill_ms=24 * hour,
        ))
    return signals


def _profile_crossings(current, previous, val, vah, limits):
    """Yield the first supported print in each observed excursion."""
    ts, prices, qty, maker = current
    if val >= vah:
        return
    sides = (prices > vah).astype(int) - (prices < val).astype(int)
    last_price = previous[1][-1]
    previous_side = int(last_price > vah) - int(last_price < val)
    changes = np.flatnonzero(sides != np.r_[previous_side, sides[:-1]])
    if not len(changes):
        return

    tail = np.searchsorted(previous[0], ts[0] - 60_000, side="right")
    stamps = np.r_[previous[0][tail:], ts]
    quantities = np.r_[previous[2][tail:], qty]
    sellers = np.r_[previous[3][tail:], maker]
    left = np.searchsorted(stamps, ts - 60_000, side="right")
    right = len(stamps) - len(ts) + np.arange(1, len(ts) + 1)
    flow = {}
    for side, mask in ((1, ~sellers), (-1, sellers)):
        cumulative = np.r_[0., np.cumsum(np.where(mask, quantities, 0.))]
        flow[side] = cumulative[right] - cumulative[left]

    for begin, end in zip(changes, np.r_[changes[1:], len(ts)]):
        side = sides[begin]
        if not side:
            continue
        limit = limits["buy_volume" if side > 0 else "sell_volume"]
        hits = np.flatnonzero(flow[side][begin:end] > limit)
        if len(hits):
            i = begin + hits[0]
            yield int(ts[i]), float(prices[i]), int(side), float(flow[side][i]), float(limit)


def assess_breakout_flow(minutes, signals, *, horizon_minutes=60, step_minutes=1, window_minutes=5):
    """Assess completed bars and rolling price/flow windows causally."""
    if type(horizon_minutes) is not int or not 5 <= horizon_minutes <= 60:
        raise ValueError("horizon_minutes must be an integer from 5 through 60")
    if (step_minutes, window_minutes) not in ((1, 5), (5, 10), (3, 12)):
        raise ValueError("Expected 1m/5m, 5m/10m or 3m/12m flow timeframes")
    if horizon_minutes < window_minutes or horizon_minutes % step_minutes:
        raise ValueError("Observation horizon must contain complete evaluation bars and a full window")
    required = ["timestamp_ms", "open", "high", "low", "close", "buy_volume", "sell_volume"]
    frame = minutes[required].copy()
    stamps = frame.pop("timestamp_ms")
    if (not pd.api.types.is_integer_dtype(stamps) or stamps.isna().any()
            or not stamps.is_monotonic_increasing or stamps.duplicated().any()
            or (stamps % 60_000).ne(0).any()):
        raise ValueError("Expected sorted, unique minute-open timestamps")
    if frame.empty:
        raise ValueError("Empty minute source")
    if (not np.isfinite(frame.to_numpy(dtype=float)).all()
            or frame[["buy_volume", "sell_volume"]].lt(0).any().any()
            or frame["low"].le(0).any()
            or frame["low"].gt(frame[["open", "close"]].min(axis=1)).any()
            or frame["high"].lt(frame[["open", "close"]].max(axis=1)).any()):
        raise ValueError("Invalid minute OHLC or aggressor quantities")
    frame.index = stamps.to_numpy()
    frame = frame.reindex(range(int(stamps.iloc[0]), int(stamps.iloc[-1]) + 60_000, 60_000))
    step_ms = step_minutes * 60_000
    if step_minutes != 1:
        grouped = frame.groupby(frame.index // step_ms * step_ms)
        counts = grouped.close.count()
        frame = grouped.agg(dict(
            open="first", high="max", low="min", close="last",
            buy_volume="sum", sell_volume="sum",
        ))
        frame.loc[counts.ne(step_minutes), :] = np.nan
    bars_per_window = window_minutes // step_minutes
    windows = pd.DataFrame({
        "open": frame.open.shift(bars_per_window - 1),
        "close": frame.close,
        "high": frame.high.rolling(bars_per_window).max(),
        "low": frame.low.rolling(bars_per_window).min(),
        "buy": frame.buy_volume.rolling(bars_per_window).sum(),
        "sell": frame.sell_volume.rolling(bars_per_window).sum(),
    })
    windows["complete"] = frame.close.rolling(bars_per_window).count().eq(bars_per_window)
    windows["return"] = windows.close / windows.open - 1
    windows["volume"] = windows.buy + windows.sell
    expected_samples = 24 * 60 // step_minutes
    rows = []
    for signal in signals:
        start = signal["feature_available_at_ms"]
        direction = signal["direction"]
        anchor_price = float(signal["entry_reference"])
        if start % step_ms or direction not in ("long", "short") or not np.isfinite(anchor_price) or anchor_price <= 0:
            raise ValueError("Expected bar-aligned setup time, direction and positive anchor")
        sign = 1 if direction == "long" else -1
        baseline = windows.loc[start - 24 * 3_600_000:start - step_ms]
        baseline = baseline[baseline.complete]
        historical_return = sign * baseline["return"].to_numpy()
        historical_volume = baseline.volume.to_numpy()
        post = frame.reindex(range(start, start + horizon_minutes * 60_000, step_ms))
        favorable_extreme = post.high.cummax() if sign == 1 else post.low.cummin()
        stop = float(signal["stop"])
        poc_touched = post.low.le(stop).cummax() if sign == 1 else post.high.ge(stop).cummax()
        for elapsed in range(window_minutes, horizon_minutes + 1, step_minutes):
            opening = start + (elapsed - step_minutes) * 60_000
            window = windows.reindex([opening]).iloc[0]
            previous = windows.reindex([opening - window_minutes * 60_000]).iloc[0]
            complete = bool(window.complete) if pd.notna(window.complete) else False
            bars_elapsed = elapsed // step_minutes
            before = post.iloc[:bars_elapsed]
            path_complete = bool(before.close.notna().all())
            row = dict(
                sample_id=signal["sample_id"], direction=direction,
                setup_timestamp_ms=int(start), assessment_timestamp_ms=int(opening + step_ms),
                elapsed_minutes=elapsed, anchor_price=anchor_price,
                window_complete=complete, path_complete=path_complete,
                poc_touched=bool(poc_touched.iloc[bars_elapsed - 1]),
                baseline_samples=len(baseline), baseline_expected_samples=expected_samples,
                interpretation="unavailable",
            )
            if not complete:
                rows.append(row)
                continue
            favorable = window.buy if sign == 1 else window.sell
            opposing = window.sell if sign == 1 else window.buy
            previous_favorable = previous.buy if sign == 1 else previous.sell
            previous_opposing = previous.sell if sign == 1 else previous.buy
            progress = sign * (window.close / window.open - 1)
            anchor_progress = sign * (window.close / anchor_price - 1)
            delta = (favorable - opposing) / window.volume if window.volume > 0 else np.nan
            previous_complete = pd.notna(previous.complete) and bool(previous.complete)
            favorable_change = favorable - previous_favorable if previous_complete else np.nan
            opposing_change = opposing - previous_opposing if previous_complete else np.nan
            enough_history = len(baseline) >= (expected_samples * 9 + 9) // 10
            return_rank = float(100 * ((historical_return < progress).sum()
                                + 0.5 * (historical_return == progress).sum()) / len(baseline)) if enough_history else np.nan
            volume_rank = float(100 * ((historical_volume < window.volume).sum()
                                + 0.5 * (historical_volume == window.volume).sum()) / len(baseline)) if enough_history else np.nan
            previous_extreme = favorable_extreme.iloc[bars_elapsed - 2] if path_complete else np.nan
            extreme = window.high if sign == 1 else window.low
            interpretation = "advancing" if progress > 0 else "retreating" if progress < 0 else "stalled"
            row.update(
                interpretation=interpretation, close=float(window.close),
                progress_from_anchor_pct=100 * anchor_progress,
                progress_window_pct=100 * progress,
                progress_bar_pct=100 * sign * (frame.at[opening, "close"] / frame.at[opening, "open"] - 1),
                **{
                    f"progress_{window_minutes}m_pct": 100 * progress,
                    f"progress_{step_minutes}m_pct": 100 * sign * (
                        frame.at[opening, "close"] / frame.at[opening, "open"] - 1
                    ),
                },
                urgency_percentile=return_rank, volume_percentile=volume_rank,
                favorable_aggressive_qty=float(favorable), opposing_aggressive_qty=float(opposing),
                delta_aligned_fraction=float(delta),
                favorable_qty_change=float(favorable_change), opposing_qty_change=float(opposing_change),
                new_favorable_extreme=bool(sign * (extreme - previous_extreme) > 0) if path_complete else None,
                pullback_from_extreme_pct=100 * sign * (favorable_extreme.iloc[bars_elapsed - 1] - window.close) / anchor_price if path_complete else np.nan,
                initiative_candidate=bool(progress > 0 and delta > 0 and favorable_change > 0),
                absorption_candidate=bool(progress > 0 and delta < 0 and opposing_change >= 0),
                exhaustion_candidate=bool(progress > 0 and opposing_change < 0),
                opposite_takeover_candidate=bool(progress < 0 and delta < 0 and opposing_change > 0),
                low_volume_expansion=bool(progress > 0 and volume_rank < 50),
            )
            if step_minutes != 1:
                bar = frame.loc[opening]
                bar_favorable = bar.buy_volume if sign == 1 else bar.sell_volume
                bar_opposing = bar.sell_volume if sign == 1 else bar.buy_volume
                bar_volume = bar_favorable + bar_opposing
                row.update(
                    bar_favorable_aggressive_qty=float(bar_favorable),
                    bar_opposing_aggressive_qty=float(bar_opposing),
                    bar_delta_aligned_fraction=float(
                        (bar_favorable - bar_opposing) / bar_volume
                    ) if bar_volume > 0 else np.nan,
                )
            rows.append(row)
    result = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["sample_id"])
    flags = [
        "window_complete", "path_complete", "poc_touched", "new_favorable_extreme",
        "initiative_candidate", "absorption_candidate", "exhaustion_candidate",
        "opposite_takeover_candidate", "low_volume_expansion",
    ]
    for name in flags:
        if name in result:
            result[name] = result[name].astype("boolean")
    return result


def _flow_cache_location(cache_dir, source_digest, signals, horizon_minutes, step_minutes=1, window_minutes=5):
    import hashlib
    import inspect
    import json

    identity = dict(
        minute_source_sha256=source_digest,
        assessor_sha256=hashlib.sha256(inspect.getsource(assess_breakout_flow).encode()).hexdigest(),
        setups_sha256=hashlib.sha256(json.dumps(signals, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        horizon_minutes=horizon_minutes, step_minutes=step_minutes, window_minutes=window_minutes,
        numpy=np.__version__, pandas=pd.__version__,
    )
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return cache_dir / "breakout_kontol_flow" / key, identity


def _cached_flow_assessment(minutes, signals, horizon_minutes, cache_dir, source_digest,
                            *, step_minutes=1, window_minutes=5):
    import json
    import os
    import uuid
    from model_development.observation_cache import digest

    folder, identity = _flow_cache_location(
        cache_dir, source_digest, signals, horizon_minutes, step_minutes, window_minutes
    )
    metadata_path = folder / "cache.json"
    if metadata_path.exists():
        saved = json.loads(metadata_path.read_text(encoding="utf-8"))
        data_path = folder / "assessment.parquet"
        if saved["identity"] != identity or digest(data_path) != saved["sha256"]:
            raise ValueError("Flow cache identity/checksum mismatch")
        return pd.read_parquet(data_path), dict(path=str(folder), reused=True, rows=saved["rows"])
    trace = assess_breakout_flow(
        minutes, signals, horizon_minutes=horizon_minutes,
        step_minutes=step_minutes, window_minutes=window_minutes,
    )
    folder.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    data = folder / (token + ".parquet")
    metadata = folder / (token + ".json")
    try:
        trace.to_parquet(data, index=False)
        saved = dict(identity=identity, rows=len(trace), sha256=digest(data))
        metadata.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        os.replace(data, folder / "assessment.parquet")
        os.replace(metadata, metadata_path)
    finally:
        data.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
    return trace, dict(path=str(folder), reused=False, rows=len(trace))


def interpret_dynamic_flow(trace, *, window_minutes=5):
    """Compare relative aggressor composition while keeping price response primary."""
    if window_minutes not in (5, 10, 12):
        raise ValueError("Dynamic flow requires a 5, 10 or 12-minute window")
    result = trace.copy()
    names = (
        "dynamic_initiative_candidate", "dynamic_absorption_candidate",
        "dynamic_exhaustion_candidate", "dynamic_low_volume_expansion",
        "dynamic_support_withdrawing", "dynamic_opposite_takeover_candidate",
    )
    if result.empty:
        for name in names:
            result[name] = pd.Series(index=result.index, dtype="boolean")
        return result
    window_column = "progress_window_pct" if "progress_window_pct" in result else f"progress_{window_minutes}m_pct"
    if result.duplicated(["sample_id", "assessment_timestamp_ms"]).any():
        raise ValueError("Dynamic flow requires one assessment per setup and timestamp")
    favorable = result.favorable_aggressive_qty
    opposing = result.opposing_aggressive_qty
    previous_favorable = favorable - result.favorable_qty_change
    previous_opposing = opposing - result.opposing_qty_change
    total, previous_total = favorable + opposing, previous_favorable + previous_opposing
    valid = (previous_favorable.ge(0) & previous_opposing.ge(0)
             & total.gt(0) & previous_total.gt(0)
             & result.window_complete.fillna(False) & result.path_complete.fillna(False))
    share = favorable.div(total.where(total.gt(0)))
    previous_share = previous_favorable.div(previous_total.where(previous_total.gt(0)))
    composition_change = share - previous_share
    progress = result[window_column]
    lookup = result.set_index(["sample_id", "assessment_timestamp_ms"])[window_column]
    previous_keys = pd.MultiIndex.from_arrays([
        result.sample_id, result.assessment_timestamp_ms - window_minutes * 60_000,
    ], names=lookup.index.names)
    previous_progress = pd.Series(lookup.reindex(previous_keys).to_numpy(), index=result.index)
    extreme = result.new_favorable_extreme.fillna(False)
    delta = result.delta_aligned_fraction
    result["previous_favorable_aggressive_qty"] = previous_favorable
    result["previous_opposing_aggressive_qty"] = previous_opposing
    result["favorable_qty_change_pct"] = 100 * result.favorable_qty_change.div(previous_favorable.where(previous_favorable.gt(0)))
    result["opposing_qty_change_pct"] = 100 * result.opposing_qty_change.div(previous_opposing.where(previous_opposing.gt(0)))
    result["favorable_share_pct"] = (100 * share).where(valid)
    result["previous_favorable_share_pct"] = (100 * previous_share).where(valid)
    result["favorable_share_change_pp"] = (100 * composition_change).where(valid)
    result["delta_aligned_change_pp"] = (200 * composition_change).where(valid)
    result["previous_window_progress_pct"] = previous_progress
    result["dynamic_composition_available"] = valid.astype("boolean")
    result["dynamic_initiative_candidate"] = (
        valid & progress.gt(0) & delta.gt(0)
        & result.favorable_qty_change.ge(0) & composition_change.ge(0)
    )
    result["dynamic_exhaustion_candidate"] = (
        valid & progress.gt(0) & result.opposing_qty_change.lt(0) & composition_change.gt(0)
    )
    result["dynamic_absorption_candidate"] = (
        valid & progress.gt(0) & delta.lt(0) & result.opposing_qty_change.ge(0) & extreme
    )
    # Candidate price efficiency, not a measurement of resting book liquidity.
    result["dynamic_low_volume_expansion"] = (
        valid & progress.gt(previous_progress) & previous_progress.gt(0)
        & total.lt(previous_total) & result.volume_percentile.lt(50) & extreme
    )
    result["dynamic_support_withdrawing"] = (
        valid & composition_change.lt(0) & result.favorable_qty_change.lt(0)
    )
    result["dynamic_opposite_takeover_candidate"] = (
        valid & progress.lt(0) & delta.lt(0) & composition_change.lt(0)
    )
    for name in names:
        result[name] = result[name].fillna(False).astype("boolean")
    return result


def confirm_flow_entries(trace, signals, end_ms, *, entry_gate_minutes=0,
                         flow_interpretation="static", window_minutes=5):
    """Confirm immediately, or revalidate at the strictly next window boundary."""
    if type(entry_gate_minutes) is not int or entry_gate_minutes not in (0, 5, 10, 12):
        raise ValueError("Entry gate must be disabled or use a 5, 10 or 12-minute boundary")
    if flow_interpretation not in ("static", "dynamic"):
        raise ValueError("flow_interpretation must be static or dynamic")
    confirmed = []
    trace = (interpret_dynamic_flow(trace, window_minutes=window_minutes)
             if flow_interpretation == "dynamic" else trace.copy())
    mechanism_names = (
        ("dynamic_initiative_candidate", "dynamic_absorption_candidate",
         "dynamic_exhaustion_candidate", "dynamic_low_volume_expansion")
        if flow_interpretation == "dynamic" else
        ("initiative_candidate", "absorption_candidate", "exhaustion_candidate", "low_volume_expansion")
    )
    trace["decision"] = "waiting"
    trace["decision_reason"] = ""
    if entry_gate_minutes:
        trace["provisional_confirmation_timestamp_ms"] = pd.Series(pd.NA, index=trace.index, dtype="Int64")
        trace["entry_gate_timestamp_ms"] = pd.Series(pd.NA, index=trace.index, dtype="Int64")
    for signal in signals:
        state, reason = "waiting", ""
        gate_due = provisional = None
        indices = trace.index[trace.sample_id.eq(signal["sample_id"])]
        states, reasons = ["waiting"] * len(indices), [""] * len(indices)
        provisional_times, gate_times = [None] * len(indices), [None] * len(indices)
        for offset, row in enumerate(trace.loc[indices].itertuples()):
            state, reason = "waiting", ""
            if row.assessment_timestamp_ms >= end_ms:
                state, reason = "unavailable", "outside_replay_window"
            elif not row.path_complete:
                state, reason = "cancelled", "missing_minute_in_setup_path"
            elif row.poc_touched:
                state, reason = "cancelled", "poc_touched_before_confirmation"
            elif row.window_complete:
                if hasattr(row, "progress_window_pct"):
                    window_progress, bar_progress = row.progress_window_pct, row.progress_bar_pct
                else:
                    window_progress = getattr(row, "progress_10m_pct", row.progress_5m_pct)
                    bar_progress = getattr(row, "progress_1m_pct", row.progress_5m_pct)
                price_ready = (
                    row.progress_from_anchor_pct > 0 and window_progress > 0
                    and row.urgency_percentile > 75 and bar_progress > 0
                )
                mechanisms = [
                    name for name in mechanism_names
                    if pd.notna(getattr(row, name)) and bool(getattr(row, name))
                ]
                ready = bool(price_ready and mechanisms)
                enter = ready
                if entry_gate_minutes:
                    if gate_due is None and ready:
                        provisional = int(row.assessment_timestamp_ms)
                        next_gate = (int(row.elapsed_minutes) // entry_gate_minutes + 1) * entry_gate_minutes
                        gate_due = int(signal["feature_available_at_ms"]) + next_gate * 60_000
                    enter = ready and gate_due == row.assessment_timestamp_ms
                    if gate_due is not None:
                        state, reason = "buffering", "awaiting_gate_revalidation"
                    if gate_due is not None and row.assessment_timestamp_ms >= gate_due and not enter:
                        state, reason = "waiting", "gate_revalidation_failed"
                        gate_due = provisional = None
                if enter:
                    eligible = int(row.assessment_timestamp_ms)
                    entry = dict(
                        signal,
                        session_day=pd.Timestamp(eligible, unit="ms", tz="UTC").date().isoformat(),
                        breakout_timestamp_ms=signal["feature_available_at_ms"],
                        breakout_anchor=signal["entry_reference"],
                        entry_reference=float(row.close),
                        feature_as_of_ms=eligible, feature_available_at_ms=eligible,
                        decision_timestamp_ms=eligible, entry_eligible_timestamp_ms=eligible,
                        entry_deadline_timestamp_ms=eligible + 60_000 - 1,
                        force_exit_timestamp_ms=eligible + 24 * 3_600_000,
                        confirmation_mechanisms=mechanisms,
                    )
                    if entry_gate_minutes:
                        entry.update(
                            provisional_confirmation_timestamp_ms=provisional,
                            entry_gate_minutes=entry_gate_minutes,
                        )
                    confirmed.append(entry)
                    state, reason = "confirmed", "|".join(mechanisms)
            if state in ("waiting", "buffering") and row.elapsed_minutes == 60:
                state, reason = "expired", "no_confirmation_within_60_minutes"
            states[offset], reasons[offset] = state, reason
            provisional_times[offset], gate_times[offset] = provisional, gate_due
            if state in ("confirmed", "expired", "cancelled", "unavailable"):
                remaining = len(indices) - offset
                states[offset:] = [state] * remaining
                reasons[offset:] = [reason] * remaining
                provisional_times[offset:] = [provisional] * remaining
                gate_times[offset:] = [gate_due] * remaining
                break
        trace.loc[indices, "decision"] = states
        trace.loc[indices, "decision_reason"] = reasons
        if entry_gate_minutes:
            trace.loc[indices, "provisional_confirmation_timestamp_ms"] = pd.array(provisional_times, dtype="Int64")
            trace.loc[indices, "entry_gate_timestamp_ms"] = pd.array(gate_times, dtype="Int64")
    return confirmed, trace


def rm_a_stop(position, tick):
    """Lock entry at 1R, then half of each doubled reward milestone."""
    side = 1 if position["direction"] == "long" else -1
    entry = position["entry_price"]
    distance = abs(entry - position["initial_stop"])
    trigger = position.get("risk_trigger_r", 1.0)
    if "risk_trigger_price" in position and side * (tick["price"] - position["risk_trigger_price"]) < 0:
        return None
    locked = position.get("risk_lock_r", -1.0)
    while side * (tick["price"] - (entry + side * trigger * distance)) >= 0:
        locked = 0.0 if trigger == 1 else trigger / 2
        trigger *= 2
    return dict(
        stop=position["initial_stop"] if locked < 0 else entry + side * locked * distance,
        risk_lock_r=locked, risk_trigger_r=trigger,
        risk_trigger_price=entry + side * trigger * distance,
    )


def rm_e_stop(position, tick):
    """Keep POC until 2R; subsequent locks follow the same doubled ladder."""
    if "risk_trigger_r" not in position:
        position = dict(position, risk_trigger_r=2.0)
    return rm_a_stop(position, tick)


def prepare_backtest(request):
    """Prepare breakout intentions for the shared Phase 2 print replay."""
    from model_development.observation_cache import aggregate_candles, candle_cache
    from model_development.phase_2.backtest import Config

    start = pd.Timestamp(request.model_config["start_date"], tz="UTC")
    end = pd.Timestamp(request.model_config["end_date"], tz="UTC") + pd.Timedelta(days=1)
    if start != start.normalize() or end != end.normalize() or end <= start:
        raise ValueError("Expected ordered inclusive UTC calendar dates")
    hour_ms = 3_600_000
    start_ms = start.value // 1_000_000
    end_ms = end.value // 1_000_000
    mode = request.model_config.get("mode", "body_volume")
    if mode not in ("body_volume", "profile_aggression", "profile_burst"):
        raise ValueError("Unknown breakout mode")
    daily_limit = request.model_config.get("one_entry_per_utc_day", mode != "body_volume")
    if type(daily_limit) is not bool:
        raise ValueError("one_entry_per_utc_day must be boolean")
    setup_variant = request.model_config.get("setup_variant", "b")
    risk_management = request.model_config.get("risk_management", "none")
    if setup_variant not in ("a", "b") or risk_management not in ("none", "rm-a", "rm-b", "rm-c", "rm-d", "rm-e"):
        raise ValueError("Expected setup_variant a/b and risk_management none/rm-a/rm-b/rm-c/rm-d/rm-e")
    if mode != "profile_burst" and (setup_variant != "b" or risk_management != "none"):
        raise ValueError("Setup variants and risk management require profile_burst")
    default_exit_hours = 48 if risk_management in ("rm-b", "rm-c") else 24
    risk_exit_hours = request.model_config.get("risk_exit_hours", default_exit_hours)
    if (type(risk_exit_hours) is not int or risk_exit_hours not in (24, 48)
            or (risk_management not in ("rm-d", "rm-e") and risk_exit_hours != default_exit_hours)):
        raise ValueError("risk_exit_hours must be 24/48 for rm-d/rm-e; existing RM exit clocks are fixed")
    default_lookback = 24 if mode == "profile_burst" else 12
    lookback_hours = request.model_config.get("lookback_hours", default_lookback)
    if mode == "profile_burst" and (type(lookback_hours) is not int or lookback_hours not in (12, 16, 24)):
        raise ValueError("Profile burst uses a 12/16/24-hour profile and matching body/volume P85 baseline")
    if mode == "profile_aggression" and lookback_hours != 12:
        raise ValueError("Profile breakout uses a fixed 12-hour profile")
    if type(lookback_hours) is not int or lookback_hours < 2:
        raise ValueError("lookback_hours must be an integer of at least 2")
    warmup_hours = 30 * 24 if mode == "profile_aggression" else lookback_hours + 1
    warmup_ms = start_ms - warmup_hours * hour_ms
    minutes, cache = candle_cache([request.input], request.cache_dir, id_policy=request.id_policy)
    if cache["source_start_ms"] > warmup_ms or cache["source_end_ms"] < end_ms - 60_000:
        raise ValueError(f"Source must cover the UTC test window and {warmup_hours} hours of warm-up")
    timed_scale_out = (24 * hour_ms, 48 * hour_ms, 0.5) if mode == "profile_aggression" or risk_management == "rm-b" else None
    if mode != "body_volume":
        from indicator import volume_profile
        from model_development.observation_cache import digest

        replay_end = min(end_ms, cache["source_end_ms"] + 1)
        profile_cache = {}
        signals = _profile_signals(
            request.input, minutes, start_ms, replay_end, burst=mode == "profile_burst",
            cache_dir=request.cache_dir, cache_info=profile_cache, lookback_hours=lookback_hours,
        )
        signals = [signal for signal in signals if signal["entry_eligible_timestamp_ms"] < replay_end]
        if setup_variant == "a":
            features = pd.read_parquet(
                Path(profile_cache["path"]) / "profile_features.parquet"
            ).set_index("closing")
            stamps = minutes.timestamp_ms.to_numpy()
            selected = []
            for signal in signals:
                closing = signal["feature_available_at_ms"]
                index = np.searchsorted(stamps, closing - hour_ms, side="left") - 1
                if index < 0:
                    raise ValueError("Previous close unavailable for variation A")
                previous_close = float(minutes.close.iloc[index])
                profile = features.loc[closing]
                crosses = (previous_close <= profile.vah if signal["direction"] == "long"
                           else previous_close >= profile.val)
                if crosses:
                    selected.append(signal)
            signals = selected
        details = dict(
            profile_bins=50, value_area_fraction=0.70, aggression_window_seconds=60,
            stop_basis="frozen_profile_poc", partial_exit_hours=24, max_entries_per_utc_day=1 if daily_limit else None,
            partial_exit_fraction=0.5, final_exit_hours=48, exit_clock="actual_entry_fill",
            aggression_baseline_days=30, aggression_quantile=0.85,
            entry_clock="first print with timestamp strictly after trigger, within 60 seconds",
            code={str(volume_profile.__file__): digest(volume_profile.__file__)},
        )
        if mode == "profile_burst":
            details = dict(
                profile_bins=50, value_area_fraction=0.70, body_quantile=0.85,
                setup_variant=setup_variant,
                baseline_checkpoint=f"breakout_kontol_cp001_{setup_variant}",
                risk_management=risk_management, risk_exit_hours=risk_exit_hours,
                percentile_lookback_hours=lookback_hours,
                price_partial_r=1.0 if risk_management in ("rm-d", "rm-e") else None,
                price_partial_fraction=0.1 if risk_management in ("rm-d", "rm-e") else None,
                risk_lock_milestones=("1R:POC unchanged, 2R:1R, 4R:2R, 8R:4R, doubling onward" if risk_management == "rm-e"
                                      else "1R:0R, 2R:1R, 4R:2R, 8R:4R, doubling onward")
                                     if risk_management != "none" else None,
                setup_close_rule=("previous close inside crossing side; current close outside"
                                  if setup_variant == "a" else "close > VAH or close < VAL; previous close unrestricted"),
                profile_cache=profile_cache,
                volume_quantile=0.85, stop_basis="frozen_profile_poc",
                final_exit_hours=risk_exit_hours,
                partial_exit_hours=24 if risk_management == "rm-b" else None,
                partial_exit_fraction=0.5 if risk_management == "rm-b" else None,
                exit_clock="actual_entry_fill", max_entries_per_utc_day=1 if daily_limit else None,
                code=details["code"],
            )
    else:
        observed_hours = np.unique(
            minutes.loc[minutes.timestamp_ms.between(warmup_ms, end_ms - 1), "timestamp_ms"]
            // hour_ms
        )
        blocks = np.split(observed_hours, np.flatnonzero(np.diff(observed_hours) != 1) + 1)
        candles = pd.concat([
            aggregate_candles(minutes, "1h", int(block[0]) * hour_ms, int(block[-1] + 1) * hour_ms)
            for block in blocks if len(block)
        ]).reindex(pd.date_range(
            pd.Timestamp(warmup_ms, unit="ms", tz="UTC"), end, freq="h", inclusive="left"
        ))
        result = detect_breakout_entries(candles, lookback_hours=lookback_hours)
        signals = []
        for stamp, row in result[result["breakout_side"].ne(0)].iterrows():
            eligible = stamp.value // 1_000_000 + hour_ms
            if not start_ms <= eligible < end_ms:
                continue
            long = row.breakout_side == 1
            signals.append(dict(
                sample_id=f"body-volume-{eligible}",
                session_day=pd.Timestamp(eligible, unit="ms", tz="UTC").date().isoformat(),
                direction="long" if long else "short",
                stop=float(row.breakout_stop),
                entry_reference=float(candles.at[stamp, "close"]),
                feature_as_of_ms=eligible, feature_available_at_ms=eligible,
                decision_timestamp_ms=eligible, entry_eligible_timestamp_ms=eligible,
                entry_deadline_timestamp_ms=eligible + hour_ms - 1,
                force_exit_timestamp_ms=eligible + 24 * hour_ms,
            ))

        details = dict(
            body_quantile=0.85, volume_quantile=0.85,
            empty_hours=[stamp.isoformat() for stamp in candles.index[candles["close"].isna()]],
        )

    flow_confirmation = request.model_config.get("flow_entry_confirmation", False)
    if type(flow_confirmation) is not bool:
        raise ValueError("flow_entry_confirmation must be boolean")
    flow_interpretation = request.model_config.get("flow_interpretation", "static")
    if flow_interpretation not in ("static", "dynamic"):
        raise ValueError("flow_interpretation must be static or dynamic")
    if flow_interpretation == "dynamic" and not flow_confirmation:
        raise ValueError("Dynamic flow requires flow_entry_confirmation")
    flow_timeframes = request.model_config.get("flow_timeframes", "1m_5m")
    flow_pairs = {"1m_5m": (1, 5), "5m_10m": (5, 10), "3m_12m": (3, 12)}
    if flow_timeframes not in flow_pairs:
        raise ValueError("flow_timeframes must be 1m_5m, 5m_10m or 3m_12m")
    step_minutes, window_minutes = flow_pairs[flow_timeframes]
    entry_gate = request.model_config.get("entry_gate", False)
    if type(entry_gate) is not bool:
        raise ValueError("entry_gate must be boolean")
    if entry_gate and not flow_confirmation:
        raise ValueError("Entry gate requires flow_entry_confirmation")
    entry_gate_minutes = window_minutes if entry_gate else 0
    setup_count = len(signals)
    observation_minutes = request.model_config.get("flow_observation_minutes", 0)
    if type(observation_minutes) is not int or observation_minutes not in (0, *range(5, 61)):
        raise ValueError("flow_observation_minutes must be zero or an integer from 5 through 60")
    if flow_confirmation:
        observation_minutes = 60
    if observation_minutes:
        if mode != "profile_burst":
            raise ValueError("Minute flow observation requires profile_burst")
        trace, flow_cache = _cached_flow_assessment(
            minutes, signals, observation_minutes, request.cache_dir, cache["sha256"],
            step_minutes=step_minutes, window_minutes=window_minutes,
        )
        if flow_confirmation:
            signals, trace = confirm_flow_entries(
                trace, signals, min(end_ms, cache["source_end_ms"] + 1),
                entry_gate_minutes=entry_gate_minutes,
                flow_interpretation=flow_interpretation, window_minutes=window_minutes,
            )
            terminal = trace.groupby("sample_id", sort=False).tail(1)
            details["flow_entry_confirmation"] = dict(
                status_counts=terminal.decision.value_counts().to_dict(),
                urgency_min_percentile=75, deadline_minutes=60,
                require_anchor_reclaimed=True,
                **{f"require_positive_last_{step_minutes}m": True},
                cancel_on_poc_touch=True, cancel_on_missing_minute=True,
                setup_count=setup_count, confirmed_signals=len(signals),
                interpretation=flow_interpretation,
                dynamic_rules=(dict(
                    comparison="Current window versus immediately preceding nonoverlapping window",
                    initiative="Favorable aggression dominates, with quantity and share holding or increasing",
                    exhaustion="Opposing quantity falls and favorable share improves",
                    absorption="Opposing aggression dominates and does not fall while price prints a new favorable extreme",
                    low_volume="Volume falls below its historical median while positive window progress accelerates and price prints a new favorable extreme",
                    first_low_volume_assessment_minutes=2 * window_minutes,
                    resting_order_book_observed=False,
                ) if flow_interpretation == "dynamic" else None),
                entry_gate=dict(
                    enabled=entry_gate, interval_minutes=entry_gate_minutes,
                    strict_next_boundary=True, revalidate_at_gate=True,
                    origin="breakout close", failed_gate="wait for a new provisional confirmation",
                ),
            )
        trace.to_parquet(request.output_dir / "orderflow.parquet", index=False)
        details["flow_observation"] = dict(
            path="orderflow.parquet", horizon_minutes=observation_minutes,
            window_minutes=window_minutes, update_minutes=step_minutes, baseline_hours=24,
            timeframes=flow_timeframes,
            profile_refresh=f"Hourly rolling {lookback_hours} completed 1H candles; each new setup uses its own frozen profile",
            candidates=setup_count, rows=len(trace), observation_only=not flow_confirmation,
            cache=flow_cache,
            labels="Descriptive candidates; no fitted probability or combined score",
            effort_comparison=f"Current {window_minutes} minutes versus preceding nonoverlapping {window_minutes} minutes",
        )

    if risk_exit_hours == 48:
        for signal in signals:
            entry = signal["entry_eligible_timestamp_ms"]
            signal["force_exit_timestamp_ms"] = entry + 48 * hour_ms
            signal["exit_after_fill_ms"] = 48 * hour_ms

    def validate_timing(signal):
        entry = signal["entry_eligible_timestamp_ms"]
        if flow_confirmation:
            expected = entry % (step_minutes * 60_000) == 0 and entry == signal["decision_timestamp_ms"]
        else:
            expected = (entry % hour_ms == 0 if mode != "profile_aggression"
                        else entry == signal["decision_timestamp_ms"] + 1)
        if entry_gate:
            start = signal["breakout_timestamp_ms"]
            provisional = signal["provisional_confirmation_timestamp_ms"]
            gate_ms = entry_gate_minutes * 60_000
            expected_gate = start + ((provisional - start) // gate_ms + 1) * gate_ms
            if entry != expected_gate or not start < provisional < entry <= start + hour_ms:
                raise ValueError("Entry must follow its provisional confirmation at the strictly next gate")
        final_delay = timed_scale_out[1] if timed_scale_out is not None else risk_exit_hours * hour_ms
        if not expected or signal["force_exit_timestamp_ms"] != entry + final_delay:
            raise ValueError("Invalid breakout entry or exit deadline")

    return dict(
        signals=signals,
        config=Config(request.initial_equity, request.risk_fraction,
                      request.fee_bps, request.max_leverage,
                      tp1_r=1.0 if risk_management in ("rm-d", "rm-e") else None,
                      tp1_fraction=0.1 if risk_management in ("rm-d", "rm-e") else None),
        start_day=start.date(),
        end_day=(end - pd.Timedelta(days=1)).date(),
        replay_window=(start_ms, min(end_ms, cache["source_end_ms"] + 1)),
        timing_validator=validate_timing,
        timed_scale_out=timed_scale_out,
        risk_manager=rm_e_stop if risk_management == "rm-e" else rm_a_stop if risk_management != "none" else None,
        one_entry_per_utc_day=daily_limit,
        metadata=dict(model_id="burst_breakout", timezone="UTC", mode=mode,
                      warmup_hours=warmup_hours, lookback_hours=lookback_hours,
                      candidate_count=len(signals), one_entry_per_utc_day=daily_limit,
                      max_open_positions=1, **details),
    )
