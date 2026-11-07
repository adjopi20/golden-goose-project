"""pre_ny_bnb. Local frozen candidate; import does not start a feed or place an order."""
from __future__ import annotations
from utils.model_registry import verify_model as _verify_model
_PACKAGE_CONTRACT = _verify_model(__file__)
MODEL_ID = 'pre_ny_bnb'
SYMBOL = 'BNBUSDC'
RELEASE = 'local_frozen_v1'
from datetime import date, datetime, time as _clock_time
from zoneinfo import ZoneInfo
NY = ZoneInfo('America/New_York')

def clock_ms(day: date, hour: int, minute: int=0) -> int:
    return int(datetime.combine(day, _clock_time(hour, minute), NY).timestamp() * 1000)

def value_area(prices, profile_bins=50, value_fraction=0.7):
    if not prices:
        raise ValueError('No profile prices')
    if type(profile_bins) is not int or profile_bins < 1 or (not 0 < value_fraction <= 1):
        raise ValueError('Invalid profile configuration')
    low, high = (min(prices), max(prices))
    weights = [0.0] * profile_bins
    width = (high - low) / profile_bins
    for price, qty in prices.items():
        index = min(profile_bins - 1, int((price - low) / width)) if width else 0
        weights[index] += qty
    poc_bin = max(range(profile_bins), key=lambda i: weights[i])
    left = right = poc_bin
    held = weights[poc_bin]
    target = sum(weights) * value_fraction
    while held < target and (left > 0 or right < profile_bins - 1):
        if left == 0 or (right < profile_bins - 1 and weights[right + 1] > weights[left - 1]):
            right += 1
            held += weights[right]
        else:
            left -= 1
            held += weights[left]
    return dict(val=low + left * width, poc=low + (poc_bin + 0.5) * width if width else low, vah=low + (right + 1) * width if width else high, profile_trade_volume=sum(weights), profile_bins=profile_bins, value_fraction=value_fraction)
import math
import numpy as np
import pandas as pd
MINUTE_MS = 60000
HOUR_MS = 3600000

def _indicator_finite(value):
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _indicator_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_indicator_finite(item) for item in value]
    return value

def hourly_indicators(minutes: pd.DataFrame) -> pd.DataFrame:
    """EMA/ATR/ADX from completed, nonempty clock-hour bars (not current hour)."""
    indexed = minutes.set_index(pd.to_datetime(minutes['timestamp_ms'], unit='ms', utc=True))
    hourly = indexed.resample('1h').agg(high=('high', 'max'), low=('low', 'min'), close=('close', 'last'), minute_count=('close', 'count'))
    hourly = hourly.loc[hourly['minute_count'] > 0].copy()
    close, high, low = (hourly['close'], hourly['high'], hourly['low'])
    hourly['ema20'] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    hourly['ema200'] = close.ewm(span=200, adjust=False, min_periods=200).mean()
    previous_close = close.shift(1)
    true_range = pd.concat((high - low, (high - previous_close).abs(), (low - previous_close).abs()), axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    up, down = (high.diff(), -low.diff())
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    hourly['atr14'] = atr
    hourly['adx14'] = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    hourly['realized_volatility_20h_pct'] = 100 * close.pct_change().rolling(20, min_periods=20).std()
    hourly['atr14_to_20h_median'] = atr / atr.rolling(20, min_periods=20).median()
    hourly['end_ms'] = hourly.index.as_unit('ms').asi8 + HOUR_MS
    if hourly['end_ms'].iloc[0] <= int(minutes['timestamp_ms'].iloc[0]) or not hourly['end_ms'].is_monotonic_increasing:
        raise ValueError('Completed hourly timestamps are not valid epoch milliseconds')
    return hourly.reset_index(drop=True)

def trend_indicators_15m(minutes: pd.DataFrame) -> pd.DataFrame:
    """Observation only: all-hours EMA200/SMA20/ADX14 on clock-aligned 15m bars.

    Empty bars reset warmup; partial bars retain observed prices and coverage.
    No fabricated prices. EMA and Wilder smoothing start with a full-window SMA.
    """
    indexed = minutes.set_index(pd.to_datetime(minutes['timestamp_ms'], unit='ms', utc=True))
    bars = indexed.resample('15min').agg(high=('high', 'max'), low=('low', 'min'), close=('close', 'last'), minute_count=('close', 'count'))

    def smooth(values: pd.Series, period: int, alpha: float) -> pd.Series:
        seed = values.rolling(period, min_periods=period).mean().first_valid_index()
        output = pd.Series(np.nan, index=values.index, dtype=float)
        if seed is not None:
            seeded = values.loc[seed:].copy()
            seeded.iloc[0] = values.loc[:seed].iloc[-period:].mean()
            output.loc[seed:] = seeded.ewm(alpha=alpha, adjust=False).mean()
        return output
    chunks = []
    for _, group in bars.groupby(bars['minute_count'].eq(0).cumsum(), sort=False):
        group = group.loc[group['minute_count'] > 0].copy()
        if group.empty:
            continue
        close, high, low = (group['close'], group['high'], group['low'])
        group['ema200'] = smooth(close, 200, 2 / 201)
        group['sma20'] = close.rolling(20, min_periods=20).mean()
        tr = pd.concat((high - low, (high - close.shift()).abs(), (low - close.shift()).abs()), axis=1).max(axis=1)
        atr = smooth(tr, 14, 1 / 14)
        up, down = (high.diff(), -low.diff())
        plus = smooth(up.where((up > down) & (up > 0), 0.0), 14, 1 / 14)
        minus = smooth(down.where((down > up) & (down > 0), 0.0), 14, 1 / 14)
        group['plus_di14'] = (100 * plus / atr.replace(0, np.nan)).where(atr.ne(0), 0.0)
        group['minus_di14'] = (100 * minus / atr.replace(0, np.nan)).where(atr.ne(0), 0.0)
        di_sum = group['plus_di14'] + group['minus_di14']
        dx = (100 * (group['plus_di14'] - group['minus_di14']).abs() / di_sum.replace(0, np.nan)).where(di_sum.ne(0), 0.0)
        group['adx14'] = smooth(dx, 14, 1 / 14)
        for name in ('ema200', 'sma20'):
            group[f'{name}_change_4bars_pct'] = 100 * (group[name] / group[name].shift(4) - 1)
            group[f'price_vs_{name}_pct'] = 100 * (close / group[name] - 1)
        group['sma20_vs_ema200_pct'] = 100 * (group['sma20'] / group['ema200'] - 1)
        group['adx14_change_4bars'] = group['adx14'] - group['adx14'].shift(4)
        group['minute_coverage'] = group['minute_count'] / 15
        group['minute_coverage_200bars'] = group['minute_count'].rolling(200).sum() / 3000
        chunks.append(group)
    if not chunks:
        raise ValueError('No nonempty 15-minute bars')
    output = pd.concat(chunks).sort_index()
    output['end_ms'] = output.index.as_unit('ms').asi8 + 15 * MINUTE_MS
    return output.reset_index(drop=True)

def trend_snapshot_15m(frame: pd.DataFrame, as_of_ms: int, direction: str) -> dict:
    """Last completed quarter only; future bars never participate in the snapshot."""
    position = int(np.searchsorted(frame['end_ms'].to_numpy(), as_of_ms, side='right')) - 1
    expected_end = as_of_ms // (15 * MINUTE_MS) * (15 * MINUTE_MS)
    if position < 0 or int(frame.iloc[position]['end_ms']) != expected_end:
        return {'status': 'missing_completed_bar', 'ma_structure': 'unknown'}
    row = _indicator_finite(frame.iloc[position].to_dict())
    row['end_ms'] = int(row['end_ms'])
    ema, sma, close = (row['ema200'], row['sma20'], row['close'])
    row['status'] = 'ready' if all((row[k] is not None for k in ('ema200', 'sma20', 'adx14'))) else 'warmup'
    structure = 'unknown' if ema is None or sma is None else 'bullish_stack' if close > sma > ema else 'bearish_stack' if close < sma < ema else 'mixed'
    row['ma_structure'] = structure
    if structure in ('unknown', 'mixed'):
        row['trade_alignment'] = structure
    else:
        row['trade_alignment'] = 'aligned' if (structure == 'bullish_stack') == (direction == 'long') else 'opposed'
    return row
import math
from datetime import date, datetime, timedelta
BAR_MS = 15 * 60000
STRATEGY = 'trend_following_preny_profile_15m_v1'
PROFILE_WINDOWS = ('pre_ny',)

def number(row: dict, key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f'Non-finite {key}')
    return value

def validate_bar(row: dict) -> dict:
    bar = dict(row)
    for key in ('open', 'high', 'low', 'close', 'buy_volume', 'sell_volume'):
        bar[key] = number(row, key)
    start, end = (int(row['open_timestamp_ms']), int(row['close_timestamp_ms']))
    local = datetime.fromtimestamp(start / 1000, NY)
    if end - start != BAR_MS or local.minute % 15 or local.second or local.microsecond:
        raise ValueError('Bars must span exactly 15 minutes, with exclusive end timestamps')
    if not 0 < bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']:
        raise ValueError('Invalid OHLC')
    if min(bar['buy_volume'], bar['sell_volume']) < 0:
        raise ValueError('Negative volume')
    bar.update(open_timestamp_ms=start, close_timestamp_ms=end)
    return bar

def evaluate_session(profile: dict, bars: list[dict], *, symbol: str, confirmation_bars: int=2) -> tuple[list[dict], list[dict]]:
    """Return (signals, decisions), using only completed bars up to 12:00 NY.

    Profile is today's completed 01:00-09:00 NY session only.
    Bars may include later data, which is ignored.
    At most one entry intention per session. No outcome fields are consumed.
    """
    if confirmation_bars not in (2, 3):
        raise ValueError('confirmation_bars must be 2 or 3')
    day = date.fromisoformat(profile['session_day'])
    start, cutoff = (clock_ms(day, 9), clock_ms(day, 12))
    window = str(profile['profile_window'])
    if window not in PROFILE_WINDOWS:
        raise ValueError('Unknown profile_window')
    expected_start = clock_ms(day, 1)
    if int(profile['profile_start_timestamp_ms']) != expected_start or int(profile['profile_end_timestamp_ms']) != start:
        raise ValueError("Profile window must end at today's 09:00 NY open")
    val, poc, vah = (number(profile, key) for key in ('val', 'poc', 'vah'))
    if not 0 < val <= poc <= vah:
        raise ValueError('Expected 0 < VAL <= POC <= VAH')
    selected = sorted((validate_bar(b) for b in bars if start <= int(b['open_timestamp_ms']) < cutoff), key=lambda b: b['open_timestamp_ms'])
    stamps = [b['open_timestamp_ms'] for b in selected]
    if len(stamps) != len(set(stamps)):
        raise ValueError('Duplicate 15-minute bars')
    signals, decisions, run = ([], [], [])
    previous_end, previous_side = (start, 0)
    for b in selected:
        side = 1 if b['close'] > vah else -1 if b['close'] < val else 0
        gap = b['open_timestamp_ms'] != previous_end
        if gap or side == 0 or side != previous_side:
            run = []
        previous_end, previous_side = (b['close_timestamp_ms'], side)
        delta = b['buy_volume'] - b['sell_volume']
        if side:
            run.append(b)
        deltas = [side * (r['buy_volume'] - r['sell_volume']) for r in run]
        progress = side * (run[-1]['close'] - run[-2]['close']) if len(run) > 1 else 0.0
        body = side * (b['close'] - b['open'])
        fading = len(run) >= 3 and deltas[-3] > deltas[-2] > deltas[-1] > 0
        old_progress = side * (run[-2]['close'] - run[-3]['close']) if len(run) >= 3 else 0.0
        deteriorating = fading and 0 < progress < old_progress
        supported = side != 0 and len(run) >= confirmation_bars and (sum(deltas) > 0) and (deltas[-1] > 0) and (body > 0) and (progress > 0) and (not deteriorating)
        if not side:
            state, reason = ('WAIT', 'inside_value')
        elif len(run) < confirmation_bars:
            state, reason = ('WAIT', 'await_persistence')
        elif deteriorating:
            state, reason = ('WAIT', 'fading_delta_and_price_progress')
        elif not supported:
            state, reason = ('WAIT', 'effort_result_not_supported')
        else:
            state, reason = ('ENTRY_READY', 'supported_outside_value_progress')
        decision = dict(session_day=str(day), profile_window=window, timestamp_ms=b['close_timestamp_ms'], state=state, reason=reason, direction={1: 'long', -1: 'short', 0: None}[side], close=b['close'], delta=delta, buy_volume=b['buy_volume'], sell_volume=b['sell_volume'], consecutive_outside_closes=len(run), directional_cumulative_delta=sum(deltas), directional_progress=progress, directional_body=body, fading_directional_delta=fading, deteriorating_result=deteriorating, data_gap_before_bar=gap)
        decisions.append(decision)
        if supported:
            entry_ms = b['close_timestamp_ms']
            signals.append(dict(sample_id=f'{symbol}:{day}:{window}:{STRATEGY}', strategy=STRATEGY, symbol=symbol, session_day=str(day), profile_window=window, route='trend_following_preny_profile_15m', direction=decision['direction'], signal_timestamp_ms=entry_ms, feature_as_of_ms=entry_ms, entry_timestamp_ms=entry_ms, entry_eligible_timestamp_ms=entry_ms, entry_deadline_timestamp_ms=cutoff, entry_reference=b['close'], entry_price_is_reference=True, stop=poc, source_stop=poc, stop_model=f'{window}_poc', stops={'baseline': poc}, force_exit_timestamp_ms=clock_ms(day + timedelta(days=1), 8, 59), exit_policy='initial_stop_or_time', context=decision))
            break
    if not signals:
        decisions.append(dict(session_day=str(day), profile_window=window, timestamp_ms=cutoff, state='NO_TRADE', reason='no_supported_breakout_by_cutoff'))
    return (signals, decisions)
from collections import Counter, deque
from datetime import date
import numpy as np
BAR_MS = 900000
LABELS = ('initiative', 'possible_absorption', 'path_of_least_resistance', 'participation_fading')

def ratio(value, denominator):
    return value / denominator if denominator is not None and denominator > 0 else None

def annotate_session(day, bars, profile, references, atr_before):
    """12 scheduled closes; parallel buy/long and sell/short interpretations.

    references contains same-clock medians from earlier sessions only.
    atr_before maps bar-open timestamps to completed-hour ATR known at that open.
    No signals, outcomes, entry prices or gate decisions are accepted as inputs.
    """
    start = clock_ms(date.fromisoformat(day), 9)
    levels = [float(profile[k]) for k in ('val', 'poc', 'vah')]
    if not all(np.isfinite(levels)) or not 0 < levels[0] <= levels[1] <= levels[2]:
        raise ValueError(f'Invalid profile geometry: {day}')
    indexed = {int(b['open_timestamp_ms']): b for b in bars}
    if len(indexed) != len(bars):
        raise ValueError(f'Duplicate bars: {day}')
    histories = {'long': deque(maxlen=4), 'short': deque(maxlen=4)}
    episode = {'long': 0, 'short': 0}
    streak = {'long': 0, 'short': 0}
    previous = None
    cvd = pv = volume = 0.0
    complete = True
    outside_streak = {'long': 0, 'short': 0}
    output = []
    for i in range(12):
        opening = start + i * BAR_MS
        bar = indexed.get(opening)
        if bar is None:
            complete = False
            previous = None
            for direction in histories:
                histories[direction].clear()
                streak[direction] = outside_streak[direction] = 0
            output.append(dict(session_day=day, feature_as_of_ms=opening + BAR_MS, status='missing_bar', session_coverage_complete=False))
            continue
        if int(bar['close_timestamp_ms']) != opening + BAR_MS:
            raise ValueError(f'Invalid quarter-hour end: {day}')
        o, h, l, c = (float(bar[k]) for k in ('open', 'high', 'low', 'close'))
        buy, sell = (float(bar['buy_volume']), float(bar['sell_volume']))
        if not all(np.isfinite([o, h, l, c, buy, sell])) or min(o, h, l, c) <= 0 or min(buy, sell) < 0 or (not l <= min(o, c) <= max(o, c) <= h):
            raise ValueError(f'Invalid OHLC/volume: {day}, {opening}')
        total, delta = (buy + sell, buy - sell)
        cvd += delta
        pv += (h + l + c) / 3 * total
        volume += total
        vwap = pv / volume if volume > 0 else None
        atr = atr_before.get(opening)
        ref = references.get(i, {})
        span = h - l
        line = dict(session_day=day, feature_as_of_ms=opening + BAR_MS, status='observed', session_coverage_complete=complete, open=o, high=h, low=l, close=c, buy_volume=buy, sell_volume=sell, delta=delta, cvd_since_0900=cvd, vwap_since_0900_bar_approx=vwap, atr14_1h_before_bar=atr, relative_volume=ratio(total, ref.get('volume')), reference=ref, body_atr=ratio(abs(c - o), atr), range_atr=ratio(span, atr), upper_wick_fraction=ratio(h - max(o, c), span), lower_wick_fraction=ratio(min(o, c) - l, span), directions={})
        for direction, sign in [('long', 1), ('short', -1)]:
            hist = histories[direction]
            edge = float(profile['vah'] if sign == 1 else profile['val'])
            outside = sign * (c - edge) > 0
            was_outside = bool(hist and hist[-1]['outside_value'])
            if outside and (not was_outside) or (was_outside and (not outside)):
                hist.clear()
                streak[direction] = outside_streak[direction] = 0
                if outside:
                    episode[direction] += 1
            body = sign * (c - o)
            progress = sign * (c - (float(previous['close']) if previous else o))
            close_position = ((c - l) / span if sign == 1 else (h - c) / span) if span > 0 else 0.5
            adverse_wick = h - max(o, c) if sign == 1 else min(o, c) - l
            rv = ratio(buy if sign == 1 else sell, ref.get('buy_volume' if sign == 1 else 'sell_volume'))
            relative_body = ratio(abs(c - o), ref.get('body'))
            vwap_margin = sign * (c - vwap) if vwap is not None else None
            good_result = body > 0 and progress > 0 and (close_position > 0.5) and (relative_body is not None) and (relative_body >= 1)
            weak_result = body <= 0 or progress <= 0 or close_position <= 0.5 or (relative_body is not None and relative_body < 1 and (adverse_wick >= abs(c - o)))
            directional_delta = sign * delta
            initiative = rv is not None and rv >= 1 and (directional_delta > 0) and good_result
            path = line['relative_volume'] is not None and line['relative_volume'] < 1 and (directional_delta > 0) and good_result
            absorption = rv is not None and rv >= 1 and (directional_delta > 0) and weak_result
            prior = list(hist)[-2:]
            fading = len(prior) == 2 and prior[0]['directional_delta'] > prior[1]['directional_delta'] > directional_delta > 0 and (prior[0]['progress'] > prior[1]['progress'] > progress) and (prior[0]['aggressive_volume'] > prior[1]['aggressive_volume'] > (buy if sign == 1 else sell))
            peak = max((x['favorable_extreme'] for x in hist), default=sign * (h if sign == 1 else l))
            peak_cvd = next((x['directional_cvd'] for x in reversed(hist) if x['favorable_extreme'] == peak), None)
            extreme = sign * (h if sign == 1 else l)
            divergence = peak_cvd is not None and extreme > peak and (sign * cvd < peak_cvd) and weak_result
            labels = [name for name, flag in zip(LABELS, [initiative, absorption, path, fading]) if flag]
            supported = outside and vwap_margin is not None and (vwap_margin > 0) and (initiative or path) and (not absorption) and (not fading) and (not divergence) and complete
            streak[direction] = streak[direction] + 1 if supported else 0
            outside_streak[direction] = outside_streak[direction] + 1 if outside else 0
            record = dict(episode_id=episode[direction], outside_value=outside, edge=edge, edge_distance_atr=ratio(sign * (c - edge), atr), directional_delta=directional_delta, directional_cvd=sign * cvd, aggressive_volume=buy if sign == 1 else sell, relative_aggressive_volume=rv, directional_body_atr=ratio(body, atr), progress=progress, progress_atr=ratio(progress, atr), close_position=close_position, relative_body=relative_body, favorable_extreme=extreme, retracement_atr=ratio(max(0, max(peak, extreme) - sign * c), atr), vwap_distance_atr=ratio(vwap_margin, atr) if vwap_margin is not None else None, cvd_price_divergence_with_weak_result=divergence, labels=labels, reference_ready=rv is not None and relative_body is not None, support_streak=streak[direction], outside_close_streak=outside_streak[direction])
            hist.append(record)
            record['recent_label_counts'] = dict(Counter((label for x in hist for label in x['labels'])))
            record['recent_directional_delta'] = sum((x['directional_delta'] for x in hist))
            record['evidence_bars'] = len(hist)
            record['state'] = 'UNKNOWN_DATA' if not complete else 'UNKNOWN_REFERENCE' if not record['reference_ready'] else 'REBALANCING' if was_outside and (not outside) else 'INSIDE_VALUE' if not outside else 'SUPPORT_PERSISTING' if streak[direction] >= 2 else 'BUILDING_SUPPORT' if supported else 'WAIT'
            line['directions'][direction] = record
        output.append(line)
        previous = bar
    return output

def _gate(features: dict, plug: str, thresholds: dict[str, float]) -> bool | None:
    if plug in ('vwap_persistence', 'value_edge_acceptance', 'atr_activation'):
        score = features[plug]
        return None if score is None else score >= thresholds[plug]
    if plug == 'fee_stop_burden':
        score = features[plug]
        return None if score is None else score <= thresholds[plug]
    effort_key = 'same_clock_relative_volume' if plug == 'volume_without_result' else 'directional_delta_imbalance'
    effort, result = (features[effort_key], features['directional_result_atr'])
    if effort is None or result is None:
        return None
    return not (effort >= thresholds[effort_key] and result < thresholds['directional_result_atr'])

def keep(row, policy):
    trend = row['trend_15m']
    state = row['effort_result_pre_entry']['setup_direction']
    labels = state['labels']
    initiative = 'initiative' in labels
    persistence = state['state'] == 'SUPPORT_PERSISTING'
    if policy == 'baseline':
        return True
    if policy in ('c1_delta', 'atr_activation'):
        plug = 'delta_without_result' if policy == 'c1_delta' else policy
        return row['research_gates'][plug]['gate'] is not False
    if policy == 'c2_initiative':
        return initiative
    if policy == 'c2_persistence':
        return persistence
    if policy == 'c2_union_i_p':
        return initiative or persistence
    if policy == 'c2_union_i_a_p':
        return initiative or persistence or 'possible_absorption' in labels
    if policy.startswith('ma_'):
        return trend['trade_alignment'] == policy[3:]
    if policy.startswith('ema_'):
        value = trend.get('price_vs_ema200_pct')
        sign = 1 if row['direction'] == 'long' else -1
        return value is None or sign * value >= float(policy[4:].replace('p', '.'))
    if policy.startswith('adx_'):
        value = trend.get('adx14')
        return value is None or value >= float(policy[4:])
    raise ValueError(policy)
from datetime import date
import math
POLICIES = {'BNBUSDC': 'c2_initiative'}
ACCOUNT_IDS = {'BNBUSDC': 'tf-preny-bnb-v1'}

class NotReady(ValueError):
    """Missing/stale preparation: wait for repair; do not invent a signal."""

def _finite(value):
    return isinstance(value, (int, float)) and (not isinstance(value, bool)) and math.isfinite(value)

def _risk(symbol, trend, direction, asof):
    if symbol == 'HYPEUSDT':
        return 0.005
    if trend is None or trend.get('status') != 'ready' or trend.get('end_ms') != asof:
        raise NotReady('Completed 15m trend snapshot is unavailable')
    if symbol == 'ETHUSDC':
        gap = trend.get('price_vs_ema200_pct')
        if not _finite(gap):
            raise NotReady('EMA200 directional gap missing')
        return 0.005 if (1 if direction == 'long' else -1) * gap >= 0.5 else 0.0025
    adx = trend.get('adx14')
    if not _finite(adx) or not 0 <= adx <= 100:
        raise NotReady('Completed ADX14 missing/invalid')
    return 0.005 if adx >= 30 else 0.0025

def hype_candidate_features(bars, direction, atr_before, signal_atr=None):
    recent = bars[-4:]
    volume = sum((float(b['buy_volume']) + float(b['sell_volume']) for b in recent))
    atr = signal_atr['atr14'] if signal_atr is not None else atr_before.get(int(recent[-1]['open_timestamp_ms']))
    if not volume > 0 or not _finite(atr) or atr <= 0:
        raise NotReady('HYPE C1 needs completed-hour ATR and positive volume')
    sign = 1 if direction == 'long' else -1
    return {'directional_delta_imbalance': sign * sum((float(b['buy_volume']) - float(b['sell_volume']) for b in recent)) / volume, 'directional_result_atr': sign * (float(recent[-1]['close']) - float(recent[0]['open'])) / atr}

def _hype_gate(bars, direction, atr_before, calibration, day, signal_atr=None):
    if calibration is None:
        raise NotReady('HYPE C1 calibration missing')
    try:
        first, last = (date.fromisoformat(calibration['valid_from']), date.fromisoformat(calibration['valid_to_exclusive']))
        trained_through = date.fromisoformat(calibration['trained_through'])
        thresholds = calibration['thresholds']
    except (KeyError, ValueError, TypeError) as exc:
        raise NotReady('Invalid C1 calibration artifact') from exc
    if not trained_through < day or not first <= day < last:
        raise NotReady('HYPE C1 calibration is not valid for this session')
    keys = ('directional_delta_imbalance', 'directional_result_atr')
    if any((not _finite(thresholds.get(key)) for key in keys)):
        raise NotReady('HYPE C1 thresholds incomplete')
    features = hype_candidate_features(bars, direction, atr_before, signal_atr)
    return (_gate(features, 'delta_without_result', thresholds), features)

def evaluate_completed_bar(*, symbol, profile, bars, as_of_ms, references=None, atr_before=None, trend=None, c1_calibration=None, signal_atr=None):
    """Evaluate one just-completed 15m bar. Returns None or immutable evidence.

    Bars after as_of_ms are rejected, never silently sliced away. The caller
    must call this on every completed 15m close in 09:00-12:00 NY time.
    A session with no candidate is still WAIT until the entry cutoff.
    """
    if symbol not in POLICIES:
        raise ValueError('No paper contract for symbol')
    day = date.fromisoformat(profile['session_day'])
    start, cutoff = (clock_ms(day, 9), clock_ms(day, 12))
    if type(as_of_ms) is not int or not start + BAR_MS <= as_of_ms <= cutoff or (as_of_ms - start) % BAR_MS:
        raise ValueError('Expected a completed NY 15m evaluation close')
    checked = sorted((validate_bar(b) for b in bars), key=lambda b: b['open_timestamp_ms'])
    if any((b['close_timestamp_ms'] > as_of_ms for b in checked)):
        raise ValueError('Future bar supplied to paper evaluator')
    if not checked or checked[-1]['close_timestamp_ms'] != as_of_ms:
        raise NotReady('Current completed 15m bar missing')
    signals, decisions = evaluate_session(profile, checked, symbol=symbol)
    current = next((d for d in decisions if d['timestamp_ms'] == as_of_ms and d['state'] != 'NO_TRADE'), None)
    if current is None:
        raise NotReady('Current decision missing')
    if not signals:
        return dict(state='NO_TRADE' if as_of_ms == cutoff else 'WAIT', decision=current, signal=None)
    signal = signals[0]
    if signal['feature_as_of_ms'] != as_of_ms:
        raise ValueError('Earlier first candidate was not processed at its close')
    if signal['session_day'] != profile['session_day'] or signal['symbol'] != symbol:
        raise ValueError('Signal/profile mismatch')
    atr_before = atr_before or {}
    if any((int(key) > checked[-1]['open_timestamp_ms'] for key in atr_before)):
        raise ValueError('Future ATR snapshot supplied')
    if symbol == 'HYPEUSDT':
        if signal_atr is not None:
            end = signal_atr.get('end_ms')
            if type(end) is not int or end > as_of_ms or as_of_ms - end >= 3600000 or (not _finite(signal_atr.get('atr14'))) or (signal_atr['atr14'] <= 0):
                raise NotReady('Completed signal-time HYPE ATR is unavailable')
        expected = list(range(start, as_of_ms, BAR_MS))
        if [b['open_timestamp_ms'] for b in checked] != expected:
            raise NotReady('Incomplete 09:00-to-signal order-flow coverage')
        gate, features = _hype_gate(checked, signal['direction'], atr_before, c1_calibration, day, signal_atr)
        row = dict(trend_15m={}, effort_result_pre_entry={'setup_direction': {'labels': [], 'state': 'NOT_USED'}}, research_gates={'delta_without_result': {'gate': gate}})
        selected = bool(keep(row, POLICIES[symbol]))
        snapshot = dict(feature_as_of_ms=as_of_ms, profile=profile, decision=current, c2=None, trend_15m=None, c1_features=features, c1_calibration=c1_calibration, selector=POLICIES[symbol], selected=selected, risk_fraction=0.005)
        return dict(state='ENTRY_READY' if selected else 'SELECTOR_REJECTED', decision=current, signal=signal, snapshot=snapshot, selected=selected, risk_fraction=0.005)
    if references is None or references.get('through_session_day') is None:
        raise NotReady('Prior-session C2 references missing')
    if not date.fromisoformat(references['through_session_day']) < day:
        raise ValueError('C2 references include current/future session')
    slot = (as_of_ms - start) // BAR_MS - 1
    slot_refs = references.get('slots', {})
    ref = slot_refs.get(slot, slot_refs.get(str(slot)))
    if not isinstance(ref, dict) or ref.get('prior_sessions', 0) < 10:
        raise NotReady('Insufficient same-clock C2 history')
    refs = {int(k): v for k, v in slot_refs.items()}
    timeline = annotate_session(str(day), checked, profile, refs, atr_before)
    evidence = next((r for r in timeline if r['feature_as_of_ms'] == as_of_ms), None)
    if evidence is None or evidence['status'] != 'observed' or (not evidence['session_coverage_complete']):
        raise NotReady('Incomplete 09:00-to-signal order-flow coverage')
    direction_state = evidence['directions'][signal['direction']]
    if direction_state['state'] in ('UNKNOWN_DATA', 'UNKNOWN_REFERENCE'):
        raise NotReady('C2 references not ready')
    gate = None
    c1_features = None
    if symbol == 'HYPEUSDT':
        gate, c1_features = _hype_gate(checked, signal['direction'], atr_before, c1_calibration, day)
    row = dict(direction=signal['direction'], trend_15m=trend or {}, effort_result_pre_entry={'setup_direction': direction_state}, research_gates={'delta_without_result': {'gate': gate}})
    selected = bool(keep(row, POLICIES[symbol]))
    risk = _risk(symbol, trend, signal['direction'], as_of_ms)
    snapshot = dict(feature_as_of_ms=as_of_ms, profile=profile, decision=current, c2=evidence, trend_15m=trend, c1_features=c1_features, c1_calibration=c1_calibration, selector=POLICIES[symbol], selected=selected, risk_fraction=risk)
    return dict(state='ENTRY_READY' if selected else 'SELECTOR_REJECTED', decision=current, signal=signal, snapshot=snapshot, selected=selected, risk_fraction=risk)
from collections import defaultdict, deque
from datetime import datetime, timedelta
import math
import statistics
import pandas as pd

def profile_key(stamp):
    local = datetime.fromtimestamp(stamp / 1000, NY)
    return str(local.date()) if 1 <= local.hour < 9 else None

def make_profile(day, prices):
    """Same equal-width 50-bin / contiguous 70% algorithm as prepare_aggtrades."""
    if not prices:
        raise NotReady('No native profile trades')
    return dict(session_day=str(day), profile_window='pre_ny', profile_start_timestamp_ms=clock_ms(day, 1), profile_end_timestamp_ms=clock_ms(day, 9), **value_area(dict(sorted(prices.items()))))

def quarter_bars(minutes):
    indexed = minutes.set_index(pd.to_datetime(minutes.timestamp_ms, unit='ms', utc=True))
    q = indexed.resample('15min').agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'), delta=('delta', 'sum'))
    output = []
    for stamp, r in q.dropna(subset=['close']).iterrows():
        opening = int(stamp.timestamp() * 1000)
        output.append(dict(open_timestamp_ms=opening, close_timestamp_ms=opening + BAR_MS, open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close), buy_volume=max(0.0, float((r.volume + r.delta) / 2)), sell_volume=max(0.0, float((r.volume - r.delta) / 2))))
    return output

def prepare_context(minutes, day, asof):
    """Reuse research indicator formulas; only completed input is accepted."""
    if minutes.empty or int(minutes.timestamp_ms.max()) + 60000 > asof:
        raise NotReady('Missing or future minute history')
    all_bars = quarter_bars(minutes)
    start = clock_ms(day, 9)
    bars = [b for b in all_bars if start <= b['open_timestamp_ms'] < asof]
    history = defaultdict(lambda: deque(maxlen=20))
    for b in all_bars:
        local = datetime.fromtimestamp(b['open_timestamp_ms'] / 1000, NY)
        if local.date() < day and 9 <= local.hour < 12:
            history[(local.hour - 9) * 4 + local.minute // 15].append(dict(volume=b['buy_volume'] + b['sell_volume'], buy_volume=b['buy_volume'], sell_volume=b['sell_volume'], body=abs(b['close'] - b['open'])))
    refs = {}
    for slot in range(12):
        past = history[slot]
        refs[slot] = dict(prior_sessions=len(past))
        if len(past) >= 10:
            refs[slot].update({k: statistics.median((x[k] for x in past)) for k in ('volume', 'buy_volume', 'sell_volume', 'body')})
    hours = hourly_indicators(minutes)
    completed_hours = hours.loc[hours.end_ms <= asof]
    signal_atr = None
    if not completed_hours.empty:
        last_hour = completed_hours.iloc[-1]
        if asof - int(last_hour.end_ms) < 3600000 and math.isfinite(float(last_hour.atr14)) and (last_hour.atr14 > 0):
            signal_atr = dict(end_ms=int(last_hour.end_ms), atr14=float(last_hour.atr14))
    atr_before = {}
    for b in bars:
        stamp = b['open_timestamp_ms']
        past = hours.loc[(hours.end_ms <= stamp) & (hours.end_ms > stamp - 3600000)]
        if not past.empty and math.isfinite(float(past.iloc[-1].atr14)) and (past.iloc[-1].atr14 > 0):
            atr_before[stamp] = float(past.iloc[-1].atr14)
    trend = trend_snapshot_15m(trend_indicators_15m(minutes), asof, 'long')
    return dict(bars=bars, references=dict(through_session_day=str(day - timedelta(days=1)), slots=refs), atr_before=atr_before, signal_atr=signal_atr, trend=trend)

def paper_activation_signal(signal, ready_ms, max_delay_ms=300000):
    """Use the first print after computation finishes, within the frozen deadline."""
    eligible = max(signal['entry_eligible_timestamp_ms'], ready_ms)
    deadline = min(signal['entry_deadline_timestamp_ms'], signal['feature_as_of_ms'] + max_delay_ms)
    if eligible > deadline:
        raise NotReady('Candidate evaluated after entry deadline; no retrospective fill')
    return {**signal, 'entry_eligible_timestamp_ms': eligible, 'entry_deadline_timestamp_ms': deadline}
from utils.engine.contracts import make_intention

def as_intention(result, *, venue, product, instrument_id, environment, account_id, allocation_id, contract_version, config_hash, feature_version, calibration_id, decision_snapshot_ref):
    if result.get('signal') is None or result.get('selected') is not True:
        return None
    signal = result['signal']
    intention = make_intention(venue=venue, product=product, instrument_id=instrument_id, environment=environment, account_id=account_id, allocation_id=allocation_id, model_id=signal['route'], model_version=signal['strategy'], contract_version=contract_version, config_hash=config_hash, feature_version=feature_version, calibration_id=calibration_id, decision_snapshot_ref=decision_snapshot_ref, session_id=signal['session_day'], direction=signal['direction'], feature_as_of_ms=signal['feature_as_of_ms'], signal_timestamp_ms=signal['signal_timestamp_ms'], eligible_timestamp_ms=signal['entry_eligible_timestamp_ms'], expires_timestamp_ms=signal['entry_deadline_timestamp_ms'], force_exit_timestamp_ms=signal['force_exit_timestamp_ms'], risk_fraction=result['risk_fraction'], initial_stop=signal['stop'], entry_reference=signal['entry_reference'], exit_policy=signal['exit_policy'])
    if signal.get('sample_id'):
        intention['source_sample_id'] = signal['sample_id']
    return intention
from utils.engine.state.base import encode
from collections import Counter
from datetime import datetime
import json
import time
import pandas as pd

class SessionObserver:

    def __init__(self, store, *, calibration=None, now_ms=None, strict_coverage=False, min_native_reference_sessions=0, activate=None):
        self.store = store
        self.calibration = calibration
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.strict_coverage = strict_coverage
        self.min_native_reference_sessions = min_native_reference_sessions
        self.activate = activate

    def boundary(self, symbol, asof, state):
        if symbol != SYMBOL:
            raise ValueError('This frozen submodel accepts only its declared symbol')
        local = datetime.fromtimestamp(asof / 1000, NY)
        day = local.date()
        start, cutoff = (clock_ms(day, 9), clock_ms(day, 12))
        if not start <= asof <= cutoff:
            return
        db = self.store.db
        if db.execute('SELECT 1 FROM evaluations WHERE symbol=? AND asof=?', (symbol, asof)).fetchone():
            return
        result = dict(state='NOT_READY', session_day=str(day), as_of_ms=asof, candidate=False)
        try:
            if self.strict_coverage and state['coverage_start_ms'] > clock_ms(day, 1):
                raise NotReady('Collector did not cover full 01:00-09:00 profile; wait for next session')
            if asof > start and self.min_native_reference_sessions and (symbol != 'HYPEUSDT'):
                prior_minutes = db.execute("SELECT timestamp_ms FROM minutes WHERE symbol=? AND source='aggregate_trades' AND timestamp_ms>=? AND timestamp_ms<?", (symbol, asof - 25 * 86400000, start)).fetchall()
                counts = Counter()
                for row in prior_minutes:
                    local_minute = datetime.fromtimestamp(row[0] / 1000, NY)
                    if local_minute.date() < day and 9 <= local_minute.hour < 12:
                        counts[local_minute.date()] += 1
                if sum((count >= 60 for count in counts.values())) < self.min_native_reference_sessions:
                    raise NotReady('Need earlier native 09:00-12:00 sessions before relative order-flow evaluation')
            stored = db.execute('SELECT payload FROM profiles WHERE symbol=? AND session=?', (symbol, str(day))).fetchone()
            if stored:
                profile = json.loads(stored[0])
            else:
                if state['coverage_start_ms'] > clock_ms(day, 1):
                    raise NotReady('Collector did not cover full 01:00-09:00 profile; wait for next session')
                prices = {r[0]: r[1] for r in db.execute('SELECT price,quantity FROM profile_prices WHERE symbol=? AND session=? ORDER BY price', (symbol, str(day)))}
                profile = make_profile(day, prices)
                db.execute('INSERT INTO profiles VALUES (?,?,?)', (symbol, str(day), encode(profile)))
                db.execute('DELETE FROM profile_prices WHERE symbol=? AND session<?', (symbol, str(day)))
            if asof == start:
                result.update(state='PROFILE_READY', profile=profile)
            else:
                prior = db.execute('SELECT payload FROM evaluations WHERE symbol=? AND asof>=? AND asof<?', (symbol, start, asof)).fetchall()
                if any((json.loads(r[0]).get('candidate') for r in prior)):
                    result['state'] = 'SESSION_LOCKED'
                else:
                    rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM minutes WHERE symbol=? AND timestamp_ms<? ORDER BY timestamp_ms', (symbol, asof))]
                    if not rows:
                        raise NotReady('No completed history')
                    context = prepare_context(pd.DataFrame(rows), day, asof)
                    signals, _ = evaluate_session(profile, context['bars'], symbol=symbol)
                    result['candidate'] = bool(signals)
                    if signals:
                        signal = signals[0]
                        if signal['feature_as_of_ms'] != asof:
                            raise NotReady('First candidate was missed; no replacement entry')
                        if symbol == 'HYPEUSDT':
                            result['c1_candidate_features'] = hype_candidate_features(context['bars'], signal['direction'], context['atr_before'], context['signal_atr'])
                        trend = context['trend']
                        structure = trend.get('ma_structure')
                        if structure in ('bullish_stack', 'bearish_stack'):
                            trend['trade_alignment'] = 'aligned' if (structure == 'bullish_stack') == (signal['direction'] == 'long') else 'opposed'
                    evaluated = evaluate_completed_bar(symbol=symbol, profile=profile, as_of_ms=asof, c1_calibration=self.calibration, **context)
                    result.update(evaluated)
                    if evaluated['signal'] is not None:
                        ready_ms = self.now_ms()
                        result['decision_ready_ms'] = ready_ms
                        result['decision_delay_ms'] = ready_ms - asof
                        if self.activate is not None:
                            result.update(self.activate(evaluated, ready_ms))
                        else:
                            result['paper_result'] = 'collection_only_no_order'
        except NotReady as exc:
            result.update(state='NOT_READY', reason=str(exc))
        db.execute('INSERT INTO evaluations VALUES (?,?,?)', (symbol, asof, encode(result)))
        print(encode(dict(symbol=symbol, session=str(day), asof=asof, state=result['state'], reason=result.get('reason'), paper=self.activate is not None)), flush=True)
        return result

_PAPER_ACCOUNT_PRESETS = {'binance': {'id': 'tf-preny-bnb-v1', 'symbol': 'BNBUSDC', 'contract': 'TF-PRENY-BNB-001', 'allowed_risks': [0.0025, 0.005], 'execution': {'initial_equity': 1000, 'risk_fraction': 0.005, 'fee_bps': 4, 'max_leverage': 5, 'max_entry_delay_ms': 300000, 'tp1_r': 2, 'tp1_fraction': 0.1}}, 'lighter': {'id': 'tf-preny-bnb-v1', 'symbol': 'BNBUSDC', 'contract': 'TF-PRENY-BNB-001-lighter-paper-pending-parity', 'allowed_risks': [0.0025, 0.005], 'execution': {'initial_equity': 1000, 'risk_fraction': 0.005, 'fee_bps': 4, 'max_leverage': 5, 'max_entry_delay_ms': 300000, 'tp1_r': 2, 'tp1_fraction': 0.1}}}

def account_configuration(venue):
    """Frozen local paper account settings, not a server/venue activation choice."""
    if venue not in _PAPER_ACCOUNT_PRESETS:
        raise ValueError("No frozen local paper preset for this venue")
    return json.loads(json.dumps(_PAPER_ACCOUNT_PRESETS[venue]))


def create_paper_manager(store, *, venue='binance'):
    """Construct the existing paper contract; no feed or activation is started."""
    _verify_model(__file__)
    if venue != 'binance':
        raise ValueError('This operational package selects Binance')
    from utils.engine.adapters.paper_execution import Config
    from utils.engine.execution.paper_manager import PaperOrderManager
    account = account_configuration(venue)
    manager = PaperOrderManager(store)
    manager.register(account['id'], account['symbol'], Config(**account['execution']),
                     allowed_risks=account['allowed_risks'], contract=account['contract'])
    return manager
