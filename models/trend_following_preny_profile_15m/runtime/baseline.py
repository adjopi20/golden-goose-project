"""Frozen causal baseline evaluation, no IO or execution."""
from __future__ import annotations
import math
from datetime import date, datetime, timedelta
from trading_core.session import NY, clock_ms


BAR_MS = 15 * 60_000


STRATEGY = "trend_following_preny_profile_15m_v1"


PROFILE_WINDOWS = ("pre_ny",)


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
