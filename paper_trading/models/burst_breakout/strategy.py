"""Pinned HYPE burst B/16H, static 3m/12m, rm-e/48H paper package.

Uses the shared legacy next-print simulator, not the ETH Futures order contract.
No exchange-order adapter, credential loading, Telegram or research imports.
"""
import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import time

import numpy as np
import pandas as pd
from indicator.volume_profile import build_basic_volume_profile
from utils.engine.adapters.paper_execution import Config
from utils.engine.execution.paper_manager import PaperOrderManager, save_state
from utils.engine.state.base import StateStore, encode
from utils.model_registry import verify_model

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
ACCOUNT = "hype_burst_16h_b_3m12m_rm_e_48h_paper_v01"
EXECUTION = Config(initial_equity=1000., risk_fraction=.01, fee_bps=4.,
                   max_leverage=5., max_entry_delay_ms=60_000, tp1_r=1., tp1_fraction=.1)

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


class BurstPaperManager(PaperOrderManager):
    """Causal model/data adapter around the existing durable paper manager."""
    def __init__(self, store, config, contract):
        super().__init__(store)
        self.config = config
        self.contract = contract
        identity = encode(dict(owner="burst_breakout_paper_v1", config=config,
                               strategy=contract["strategy_sha256"],
                               engine=contract["engine_release"]["sha256"]))
        store.db.executescript("""
              CREATE TABLE IF NOT EXISTS burst_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS burst_minutes(
                stamp INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
                buy_volume REAL, sell_volume REAL);
              CREATE TABLE IF NOT EXISTS burst_prices(
                hour INTEGER, price REAL, qty REAL, PRIMARY KEY(hour,price));
        """)
        with store.transaction():
            prior = self.get("identity")
            if prior is not None and prior != identity:
                raise ValueError("Paper model/configuration identity changed; use a fresh account DB")
            self.put("identity", identity)
        self.register(ACCOUNT, "HYPEUSDT", EXECUTION, contract=contract["contract"])

    def get(self, key, default=None):
        r = self.store.db.execute("SELECT value FROM burst_meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key, value):
        self.store.db.execute("INSERT INTO burst_meta VALUES (?,?) ON CONFLICT(key) "
                              "DO UPDATE SET value=excluded.value", (key, encode(value)))

    def _record(self, kind, value):
        self.store.record(ACCOUNT, kind, value)

    def _blocked(self, stamp):
        _, p, _ = self._load(ACCOUNT)
        if p.position is not None or p.signals:
            return "position_or_entry_pending"
        if self.get("last_entry_day") == stamp // DAY:
            return "daily_entry_limit"
        if p.equity <= 0:
            return "insufficient_equity"
        return None

    def _queue(self, signal, received_ms):
        reason = self._blocked(received_ms)
        if reason:
            self._record("blocked_setup", dict(sample_id=signal["sample_id"], reason=reason))
            return False
        # A bar becomes available on receipt of the next print; never backdate a fill.
        signal = dict(signal, symbol="HYPEUSDT", strategy="burst_breakout", route="b_16h_3m12m_rm_e_48h",
                      feature_available_at_ms=received_ms, decision_timestamp_ms=received_ms,
                      entry_eligible_timestamp_ms=received_ms + 1,
                      entry_deadline_timestamp_ms=received_ms + MINUTE,
                      force_exit_timestamp_ms=received_ms + 48 * HOUR,
                      exit_after_fill_ms=48 * HOUR)
        _, p, cfg = self._load(ACCOUNT)
        self._record("candidate", signal)
        p.signals = [signal]
        p.next_signal = 0
        self.store.db.execute("UPDATE accounts SET state=? WHERE id=?", (save_state(p, cfg), ACCOUNT))
        return True

    def _execute(self, trade):
        _, before, cfg = self._load(ACCOUNT)
        if before.signals and self.get("last_entry_day") == trade["timestamp_ms"] // DAY:
            before.signals = []
            before.next_signal = 0
            self._record("decision", dict(reason="daily_entry_limit_at_fill"))
            self.store.db.execute("UPDATE accounts SET state=? WHERE id=?", (save_state(before, cfg), ACCOUNT))
        super().tick(ACCOUNT, trade)
        _, p, cfg = self._load(ACCOUNT)
        if p.position is None:
            if before.position is not None:
                previous = before.position
                row = self.store.db.execute("SELECT payload FROM journal WHERE account=? AND kind='trade' "
                                            "ORDER BY id DESC LIMIT 1", (ACCOUNT,)).fetchone()
                result = json.loads(row[0])
                reason = result["exit_reason"]
                if reason == "initial_stop_next_print" and previous.get("risk_lock_r", -1) >= 0:
                    reason = "risk_lock_stop_next_print"
                self._record("position_closed", dict(sample_id=previous["sample_id"],
                    exit_reason=reason, initial_stop=previous["initial_stop"], final_stop=previous["stop"],
                    risk_stop_updates=previous["risk_stop_updates"], pnl=result["pnl"], fees=result["fees"]))
            return
        position = p.position
        if before.position is None:
            position.update(initial_stop=position["stop"], risk_stop_updates=0)
            position["force_exit_timestamp_ms"] = position["entry_timestamp_ms"] + 48 * HOUR
            self.put("last_entry_day", position["entry_timestamp_ms"] // DAY)
            self._record("position_open", dict(position))
        if p.stop_trigger is None and p.target_trigger is None:
            update = rm_e_stop(position, trade)
            if update is not None:
                side = 1 if position["direction"] == "long" else -1
                if (not math.isfinite(update["stop"]) or update["stop"] <= 0
                        or side * (update["stop"] - position["stop"]) < 0
                        or side * (trade["price"] - update["stop"]) <= 0):
                    raise ValueError("Invalid rm-e stop update")
                if update["stop"] != position["stop"]:
                    position["risk_stop_updates"] += 1
                    self._record("stop_update", dict(timestamp_ms=trade["timestamp_ms"],
                        sample_id=position["sample_id"], **update))
                position.update(update)
        self.store.db.execute("UPDATE accounts SET state=? WHERE id=?", (save_state(p, cfg), ACCOUNT))

    def _minutes(self, start, end):
        rows = self.store.db.execute("SELECT * FROM burst_minutes WHERE stamp>=? AND stamp<? ORDER BY stamp",
                                     (start, end)).fetchall()
        return pd.DataFrame([dict(r) for r in rows]).rename(columns={"stamp":"timestamp_ms"})

    def _hour_setup(self, closing, received_ms):
        frame = self._minutes(closing - 17 * HOUR, closing)
        if len(frame) != 17 * 60:
            return None
        hourly = frame.groupby(frame.timestamp_ms // HOUR).agg(
            open=("open", "first"), close=("close", "last"),
            buy=("buy_volume", "sum"), sell=("sell_volume", "sum"))
        if len(hourly) != 17:
            return None
        historical, burst = hourly.iloc[:-1], hourly.iloc[-1]
        prices = self.store.db.execute("SELECT price,SUM(qty) AS qty FROM burst_prices "
            "WHERE hour>=? AND hour<? GROUP BY price ORDER BY price", (closing - 17 * HOUR, closing - HOUR)).fetchall()
        profile = build_basic_volume_profile(pd.DataFrame([dict(r) for r in prices]), n_bins=50, value_area_pct=.70)
        feature = dict(closing=closing, open=float(burst.open), close=float(burst.close),
            body=float(abs(burst.close - burst.open)), volume=float(burst.buy + burst.sell),
            body_p85=float(np.quantile(abs(historical.close - historical.open), .85)),
            volume_p85=float(np.quantile(historical.buy + historical.sell, .85)),
            val=profile["val"], vah=profile["vah"], poc=profile["poc_price"])
        self.put("latest_profile", feature)
        signals = _burst_signals_from_features(pd.DataFrame([feature]), closing + 1)
        if not signals:
            return None
        # Research blocks at confirmed entry time, not at the earlier burst close.
        self._record("setup", dict(signal=signals[0], profile=feature))
        return signals[0]

    def _evaluate(self, closing, received_ms):
        active = self.get("active_setup")
        if active and closing % (3 * MINUTE) == 0:
            elapsed = (closing - active["feature_available_at_ms"]) // MINUTE
            if 12 <= elapsed <= 60:
                frame = self._minutes(active["feature_available_at_ms"] - DAY, closing)
                if not frame.empty:
                    trace = assess_breakout_flow(frame, [active], step_minutes=3, window_minutes=12)
                    trace = trace[trace.assessment_timestamp_ms.le(closing)].copy()
                    confirmed, trace = confirm_flow_entries(trace, [active], closing + 1, window_minutes=12)
                    if not trace.empty:
                        last = json.loads(trace.iloc[-1].to_json())
                        self._record("flow_assessment", last)
                        if confirmed:
                            self._queue(confirmed[0], received_ms)
                            active = None
                        elif last["decision"] in ("cancelled", "expired", "unavailable"):
                            active = None
            if elapsed >= 60:
                active = None
        if closing % HOUR == 0:
            # The current 16H profile advances during open positions as well.
            active = self._hour_setup(closing, received_ms)
            self.store.db.execute("DELETE FROM burst_minutes WHERE stamp<?", (closing - 26 * HOUR,))
            self.store.db.execute("DELETE FROM burst_prices WHERE hour<?", (closing - 18 * HOUR,))
        self.put("active_setup", active)

    def on_trade(self, trade, received_ms):
        if (trade.get("symbol") != "HYPEUSDT" or type(trade.get("buy")) is not bool
                or any(type(trade.get(k)) is not int or trade[k] < 0 for k in ("timestamp_ms", "agg_trade_id"))
                or any(type(trade.get(k)) not in (int, float) or not math.isfinite(trade[k]) or trade[k] <= 0
                       for k in ("price", "quantity"))):
            raise ValueError("Invalid native HYPE public trade")
        if (type(received_ms) is not int or not trade["timestamp_ms"] <= received_ms
                or received_ms - trade["timestamp_ms"] > self.config["max_event_age_ms"]):
            raise ValueError("Stale/future native trade; paper feed halted")
        with self.store.transaction():
            previous = self.get("native_cursor")
            if previous:
                if trade["agg_trade_id"] == previous["agg_trade_id"]:
                    if trade != previous:
                        raise ValueError("Conflicting native duplicate")
                    return "duplicate"
                if trade["agg_trade_id"] != previous["agg_trade_id"] + 1:
                    raise ValueError("Unverified native trade ID gap/transition; repair before continuing")
                if trade["timestamp_ms"] < previous["timestamp_ms"] or received_ms < self.get("last_received_ms", 0):
                    raise ValueError("Native trade/receipt time regressed")
            stamp = trade["timestamp_ms"] // MINUTE * MINUTE
            if previous is None:
                self.put("complete_from_ms", stamp + MINUTE)
            elif stamp > previous["timestamp_ms"] // MINUTE * MINUTE:
                self._evaluate(stamp, received_ms)
            self._execute(trade)
            if stamp >= self.get("complete_from_ms"):
                buy, sell = (trade["quantity"], 0.) if trade["buy"] else (0., trade["quantity"])
                self.store.db.execute("INSERT INTO burst_minutes VALUES (?,?,?,?,?,?,?) ON CONFLICT(stamp) "
                    "DO UPDATE SET high=MAX(high,excluded.high),low=MIN(low,excluded.low),close=excluded.close,"
                    "buy_volume=buy_volume+excluded.buy_volume,sell_volume=sell_volume+excluded.sell_volume",
                    (stamp, trade["price"], trade["price"], trade["price"], trade["price"], buy, sell))
                # Retain quantity by native price, not OHLC-inferred volume allocation.
                self.store.db.execute("INSERT INTO burst_prices VALUES (?,?,?) ON CONFLICT(hour,price) "
                    "DO UPDATE SET qty=qty+excluded.qty", (stamp // HOUR * HOUR, trade["price"], trade["quantity"]))
            self.put("native_cursor", trade)
            self.put("last_received_ms", received_ms)
        return "processed"

    def status(self):
        result = dict(mode="paper", model="burst_breakout", real_orders=False,
            execution_contract="legacy_next_native_print_paper", account=super().status()[0],
            last_received_ms=self.get("last_received_ms"), active_setup=self.get("active_setup"),
            minute_count=self.store.db.execute("SELECT COUNT(*) FROM burst_minutes").fetchone()[0],
            latest_profile=self.get("latest_profile"),
            warmup="Native price/quantity profile: 16 prior complete hours; flow baseline: 24 hours",
            limitations=["Next-print fills, no orderbook/rounding/funding/liquidation simulation",
                         "Not yet bound to a supported live Futures lifecycle"])
        return result


def create_paper_manager(database, config):
    contract = verify_model(__file__)
    expected = dict(schema_version=1, mode="paper", venue="binance", symbol="HYPEUSDT",
                    account_id=ACCOUNT, execution=asdict(EXECUTION), max_event_age_ms=10_000)
    if config != expected:
        raise ValueError("Configuration differs from the selected paper candidate")
    database = Path(database)
    if database.exists():
        import sqlite3
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as old:
            try:
                identity = old.execute("SELECT value FROM burst_meta WHERE key='identity'").fetchone()
                mode = old.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()
            except sqlite3.DatabaseError as exc:
                raise ValueError("Not this paper account; never adopt another account DB") from exc
            if not identity or not mode or mode[0] != "paper":
                raise ValueError("Not this paper account DB")
    store = StateStore(database, "binance")
    try:
        store.acquire_writer()
        return BurstPaperManager(store, config, contract)
    except BaseException:
        store.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", choices=("init", "status", "run"), default="status")
    parser.add_argument("--duration-seconds", type=int)
    args = parser.parse_args(argv)
    if args.duration_seconds is not None and args.duration_seconds <= 0:
        parser.error("duration-seconds must be positive")
    manager = create_paper_manager(args.database, json.loads(args.config.read_text(encoding="utf-8-sig")))
    try:
        print(json.dumps(manager.status(), allow_nan=False), flush=True)
        if args.action != "run":
            return 0
        from utils.engine.adapters.binance_public import BinancePublic
        api = BinancePublic()
        api.validate_markets(["HYPEUSDT"])
        started = last_log = time.monotonic()
        try:
            for trade in api.stream(["HYPEUSDT"]):
                if trade is not None:
                    manager.on_trade(trade, int(time.time() * 1000))
                if time.monotonic() - last_log >= 60:
                    print(json.dumps(manager.status(), allow_nan=False), flush=True)
                    last_log = time.monotonic()
                if args.duration_seconds is not None and time.monotonic() - started >= args.duration_seconds:
                    break
        finally:
            api.http.close()
    finally:
        manager.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
