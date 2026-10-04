"""Existing pure input preparation; all data is supplied by the caller."""
from collections import defaultdict, deque
from datetime import datetime, timedelta
import math
import statistics
import pandas as pd
from trading_core.indicators import hourly_indicators, trend_indicators_15m, trend_snapshot_15m
from trading_core.profiles import value_area
from .evaluator import NotReady
from .baseline import BAR_MS, NY, clock_ms


def profile_key(stamp):
    local = datetime.fromtimestamp(stamp/1000, NY)
    return str(local.date()) if 1 <= local.hour < 9 else None


def make_profile(day, prices):
    """Same equal-width 50-bin / contiguous 70% algorithm as prepare_aggtrades."""
    if not prices:
        raise NotReady("No native profile trades")
    return dict(session_day=str(day), profile_window="pre_ny",
                profile_start_timestamp_ms=clock_ms(day, 1), profile_end_timestamp_ms=clock_ms(day, 9),
                **value_area(dict(sorted(prices.items()))))


def quarter_bars(minutes):
    indexed = minutes.set_index(pd.to_datetime(minutes.timestamp_ms, unit="ms", utc=True))
    q = indexed.resample("15min").agg(open=("open","first"), high=("high","max"),
        low=("low","min"), close=("close","last"), volume=("volume","sum"), delta=("delta","sum"))
    output = []
    for stamp, r in q.dropna(subset=["close"]).iterrows():
        opening = int(stamp.timestamp()*1000)
        output.append(dict(open_timestamp_ms=opening, close_timestamp_ms=opening+BAR_MS,
            open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close),
            buy_volume=max(0., float((r.volume+r.delta)/2)),
            sell_volume=max(0., float((r.volume-r.delta)/2))))
    return output


def prepare_context(minutes, day, asof):
    """Reuse research indicator formulas; only completed input is accepted."""
    if minutes.empty or int(minutes.timestamp_ms.max())+60_000 > asof:
        raise NotReady("Missing or future minute history")
    all_bars = quarter_bars(minutes)
    start = clock_ms(day, 9)
    bars = [b for b in all_bars if start <= b["open_timestamp_ms"] < asof]
    history = defaultdict(lambda:deque(maxlen=20))
    for b in all_bars:
        local = datetime.fromtimestamp(b["open_timestamp_ms"]/1000, NY)
        if local.date() < day and 9 <= local.hour < 12:
            history[(local.hour-9)*4+local.minute//15].append(dict(
                volume=b["buy_volume"]+b["sell_volume"], buy_volume=b["buy_volume"],
                sell_volume=b["sell_volume"], body=abs(b["close"]-b["open"])))
    refs = {}
    for slot in range(12):
        past = history[slot]
        refs[slot] = dict(prior_sessions=len(past))
        if len(past) >= 10:
            refs[slot].update({k:statistics.median(x[k] for x in past)
                               for k in ("volume","buy_volume","sell_volume","body")})
    hours = hourly_indicators(minutes)
    completed_hours = hours.loc[hours.end_ms <= asof]
    signal_atr = None
    if not completed_hours.empty:
        last_hour = completed_hours.iloc[-1]
        if (asof-int(last_hour.end_ms) < 3_600_000
                and math.isfinite(float(last_hour.atr14)) and last_hour.atr14 > 0):
            signal_atr = dict(end_ms=int(last_hour.end_ms), atr14=float(last_hour.atr14))
    atr_before = {}
    for b in bars:
        stamp = b["open_timestamp_ms"]
        past = hours.loc[(hours.end_ms <= stamp) & (hours.end_ms > stamp-3_600_000)]
        if not past.empty and math.isfinite(float(past.iloc[-1].atr14)) and past.iloc[-1].atr14 > 0:
            atr_before[stamp] = float(past.iloc[-1].atr14)
    trend = trend_snapshot_15m(trend_indicators_15m(minutes), asof, "long")
    # Direction-independent fields drive sizing; actual alignment is set below.
    return dict(bars=bars, references=dict(through_session_day=str(day-timedelta(days=1)), slots=refs),
                atr_before=atr_before, signal_atr=signal_atr, trend=trend)


def paper_activation_signal(signal, ready_ms, max_delay_ms=300_000):
    """Use the first print after computation finishes, within the frozen deadline."""
    eligible = max(signal["entry_eligible_timestamp_ms"], ready_ms)
    deadline = min(signal["entry_deadline_timestamp_ms"],
                   signal["feature_as_of_ms"] + max_delay_ms)
    if eligible > deadline:
        raise NotReady("Candidate evaluated after entry deadline; no retrospective fill")
    return {**signal, "entry_eligible_timestamp_ms": eligible,
            "entry_deadline_timestamp_ms": deadline}
