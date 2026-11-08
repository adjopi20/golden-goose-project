"""Frozen Burst signal calculations shared by the live packages."""
import numpy as np
import pandas as pd


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
