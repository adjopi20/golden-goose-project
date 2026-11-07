"""Maintained Pre-NY research family; global runners own execution/output.
Legacy C1 feature formulas and calibration are deliberately preserved.
"""
from __future__ import annotations
from model_development.research_dependencies import bind_clock, clock_source
import math
from datetime import date, datetime, timedelta
from utils.market_session import NY, clock_ms
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

def evaluate_session(profile: dict, bars: list[dict], *, symbol: str, confirmation_bars: int=2,
                     require_delta: bool=True) -> tuple[list[dict], list[dict]]:
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
        deteriorating = require_delta and fading and 0 < progress < old_progress
        supported = (side != 0 and len(run) >= confirmation_bars and body > 0 and progress > 0
                     and (not require_delta or (sum(deltas) > 0 and deltas[-1] > 0 and not deteriorating)))
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
from utils.market_session import clock_ms
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
POLICIES = {'ETHUSDC': 'c2_union_i_a_p', 'BNBUSDC': 'c2_initiative', 'HYPEUSDT': 'c1_delta'}
ACCOUNT_IDS = {'ETHUSDC': 'tf-preny-eth-v1', 'BNBUSDC': 'tf-preny-bnb-v1', 'HYPEUSDT': 'tf-preny-hype-v1'}

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

def _thresholds(train: list[dict], plug: str) -> dict[str, float] | None:
    keys = {'vwap_persistence': ('vwap_persistence',), 'value_edge_acceptance': ('value_edge_acceptance',), 'volume_without_result': ('same_clock_relative_volume', 'directional_result_atr'), 'delta_without_result': ('directional_delta_imbalance', 'directional_result_atr'), 'atr_activation': ('atr_activation',), 'fee_stop_burden': ('fee_stop_burden',)}[plug]
    available = [row for row in train if all((row['features'][key] is not None for key in keys))]
    if len(available) < 25:
        return None
    return {key: statistics.median((row['features'][key] for row in available)) for key in keys}
import statistics
import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from indicator.value_area import value_area
from utils.market_session import NY, clock_ms

def to_path_audit_signal(signal: dict, entry_tick: dict) -> dict:
    """Bind an engine-selected eligible raw fill to model_development.phase_2.backtest.

    Engine must select the first executable tick, not a favorable later price.
    This function validates its time/risk geometry and does not simulate fills.
    """
    timestamp = int(entry_tick['timestamp_ms'])
    entry = number(entry_tick, 'price')
    side = 1 if signal['direction'] == 'long' else -1
    if not signal['entry_eligible_timestamp_ms'] <= timestamp <= signal['entry_deadline_timestamp_ms']:
        raise ValueError('Entry fill falls outside the entry window')
    if entry <= 0 or side * (entry - signal['stop']) <= 0:
        raise ValueError('Actual fill has invalid POC stop geometry')
    return {**signal, 'entry_timestamp_ms': timestamp, 'entry': entry, 'entry_fill_agg_trade_id': int(entry_tick['agg_trade_id']), 'entry_price_is_reference': False}

def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == '.parquet':
        import pyarrow.parquet as pq
        return pq.read_table(path).to_pylist()
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

def prepare_aggtrades(path: Path, start_day: date, end_day: date, *, profile_bins: int=50, value_fraction: float=0.7) -> tuple[dict[str, list[dict]], list[dict], list[dict]]:
    """Stream raw aggTrades into Pre-NY profiles and 15-minute bars.

    This reads only Parquet row groups overlapping [first day 01:00,
    final day 12:00). It does not load the entire file into memory.
    """
    import pyarrow.parquet as pq
    if end_day < start_day or profile_bins < 2 or (not 0 < value_fraction < 1):
        raise ValueError('Invalid date range or profile parameters')
    raw_start = clock_ms(start_day, 1)
    raw_end = clock_ms(end_day, 12)
    source = pq.ParquetFile(path)
    required = {'timestamp', 'price', 'qty', 'is_buyer_maker', 'agg_trade_id'}
    if not required.issubset(source.schema_arrow.names):
        raise ValueError(f'Raw aggTrades need columns: {sorted(required)}')
    profile_prices: dict[tuple[str, date], dict[float, float]] = defaultdict(lambda: defaultdict(float))
    bars_15m: dict[int, dict] = {}

    def add_trade(target: dict[int, dict], bar_start: int, bar_end: int, stamp: int, trade_id: int, price: float, qty: float, buyer_maker: bool) -> None:
        bar = target.get(bar_start)
        order = (stamp, trade_id)
        if bar is None:
            bar = target[bar_start] = dict(open_timestamp_ms=bar_start, close_timestamp_ms=bar_end, open=price, high=price, low=price, close=price, buy_volume=0.0, sell_volume=0.0, _first=order, _last=order)
        if order < bar['_first']:
            bar['open'], bar['_first'] = (price, order)
        if order >= bar['_last']:
            bar['close'], bar['_last'] = (price, order)
        bar['high'] = max(bar['high'], price)
        bar['low'] = min(bar['low'], price)
        bar['sell_volume' if buyer_maker else 'buy_volume'] += qty

    def finish_bars(target: dict[int, dict]) -> list[dict]:
        result = []
        for bar in sorted(target.values(), key=lambda b: b['open_timestamp_ms']):
            bar.pop('_first')
            bar.pop('_last')
            result.append(bar)
        return result
    processed = 0
    for rg in range(source.num_row_groups):
        stats = source.metadata.row_group(rg).column(source.schema_arrow.get_field_index('timestamp')).statistics
        if stats is not None and stats.has_min_max and (stats.max < raw_start or stats.min >= raw_end):
            continue
        for batch in source.iter_batches(row_groups=[rg], columns=['timestamp', 'price', 'qty', 'is_buyer_maker', 'agg_trade_id'], batch_size=100000):
            data = batch.to_pydict()
            ids = data['agg_trade_id']
            for stamp, price, qty, buyer_maker, trade_id in zip(data['timestamp'], data['price'], data['qty'], data['is_buyer_maker'], ids):
                if stamp is None or not raw_start <= stamp < raw_end:
                    continue
                if price is None or qty is None or buyer_maker is None or (trade_id is None) or (price <= 0) or (qty <= 0):
                    raise ValueError('Invalid aggTrade inside requested period')
                processed += 1
                local = datetime.fromtimestamp(stamp / 1000, NY)
                day = local.date()
                if start_day <= day <= end_day and 1 <= local.hour < 9:
                    profile_prices['pre_ny', day][float(price)] += float(qty)
                if start_day <= day <= end_day and 9 <= local.hour < 12:
                    quarter_start = clock_ms(day, local.hour, local.minute // 15 * 15)
                    add_trade(bars_15m, quarter_start, quarter_start + BAR_MS, stamp, int(trade_id), float(price), float(qty), buyer_maker)
    if not processed:
        raise ValueError('No aggTrades in requested interval')
    profile_sets: dict[str, list[dict]] = {window: [] for window in PROFILE_WINDOWS}
    for window in PROFILE_WINDOWS:
        for day in (start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)):
            prices = profile_prices.get((window, day))
            if not prices:
                continue
            profile_sets[window].append(dict(session_day=str(day), profile_window=window, profile_start_timestamp_ms=clock_ms(day, 1), profile_end_timestamp_ms=clock_ms(day, 9), **value_area(prices, profile_bins, value_fraction)))
    return (profile_sets, finish_bars(bars_15m), [])

def c1_calibrated_signals(candidates: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Frozen historical 180d/60d C1 schedule; no outcomes and no unknown fallback."""
    import math
    rows = sorted(candidates, key=lambda r: (r['session_day'], r['sample_id']))
    counts = dict(candidates=len(rows), warmup_or_uncalibrated=0, unavailable_features=0, rejected_c1=0)
    if not rows:
        return ([], [], counts)
    if len({r['sample_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate C1 candidate')
    first, last = (date.fromisoformat(rows[i]['session_day']) for i in (0, -1))
    cursor = first + timedelta(days=180)
    accepted, folds, covered = ([], [], set())
    while cursor + timedelta(days=1) <= last:
        train_start, test_start = (cursor - timedelta(days=180), cursor + timedelta(days=1))
        test_end = test_start + timedelta(days=60)
        keys = ('directional_delta_imbalance', 'directional_result_atr')
        valid = lambda r: all((isinstance(r['features'].get(k), (int, float)) and math.isfinite(r['features'][k]) for k in keys))
        train = [r for r in rows if train_start <= date.fromisoformat(r['session_day']) < cursor and valid(r)]
        thresholds = _thresholds(train, 'delta_without_result') if len(train) >= 25 else None
        fold = dict(train_start=str(train_start), train_end_exclusive=str(cursor), test_start=str(test_start), test_end_exclusive=str(test_end), train_candidates=len(train), thresholds=thresholds, taken=0)
        for row in rows:
            if not test_start <= date.fromisoformat(row['session_day']) < test_end:
                continue
            covered.add(row['sample_id'])
            if thresholds is None:
                counts['warmup_or_uncalibrated'] += 1
            elif not valid(row):
                counts['unavailable_features'] += 1
            elif _gate(row['features'], 'delta_without_result', thresholds):
                accepted.append({**row['signal'], 'c1_fold': len(folds) + 1})
                fold['taken'] += 1
            else:
                counts['rejected_c1'] += 1
        folds.append(fold)
        cursor += timedelta(days=60)
    counts['warmup_or_uncalibrated'] += sum((r['sample_id'] not in covered for r in rows))
    return (accepted, folds, counts)

def frozen_direction_oos(config, execution):
    if set(config) != {'comparison', 'oos_start', 'oos_end', 'calibration_run'} or config['comparison'] != 'direction':
        raise ValueError('OOS requires comparison=direction, oos_start, oos_end and calibration_run')
    start, finish = (date.fromisoformat(config[k]) for k in ('oos_start', 'oos_end'))
    path = Path(config['calibration_run']).resolve()
    payload = path.read_bytes()
    saved = json.loads(payload)
    if (start > finish or saved.get('status') != 'complete' or saved.get('phase') != 'backtest'
            or saved.get('instrument') != 'HYPEUSDT' or saved.get('model_version') != 'phase2_hype_direction_v1'
            or saved.get('requested_model_config') != {'comparison': 'direction'}
            or saved.get('config') != execution or date.fromisoformat(saved['end_date']) >= start):
        raise ValueError('Require a completed pre-OOS HYPE direction run with matching execution')
    folds = saved.get('calibration_folds') or []
    if not folds:
        raise ValueError('Calibration run has no saved C1 fold')
    fold_number = len(folds)
    fold = folds[-1]
    thresholds = fold['thresholds']
    keys = {'directional_delta_imbalance', 'directional_result_atr'}
    if (date.fromisoformat(fold['train_end_exclusive']) >= start
            or date.fromisoformat(fold['test_start']) > start or not isinstance(thresholds, dict)
            or set(thresholds) != keys or any(type(v) not in (int, float) or not math.isfinite(v) for v in thresholds.values())):
        raise ValueError('Require finite C1 thresholds available before OOS')
    import hashlib
    return start, finish, dict(run_id=saved['run_id'], path=str(path), sha256=hashlib.sha256(payload).hexdigest(),
                               fold_number=fold_number, **fold)


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
    price_only = a.model_config.get('comparison') == 'price_only'
    comparison = price_only or a.model_config.get('comparison') == 'direction'
    if price_only and set(a.model_config) != {'comparison', 'oos_start', 'oos_end'}:
        raise ValueError('price_only requires only comparison, oos_start and oos_end')
    if a.model_config and not comparison:
        raise ValueError('Use the frozen C1 defaults, comparison=direction, or comparison=price_only')
    source_path = a.input.resolve()
    if infer_instrument([source_path]) != 'HYPEUSDT':
        raise ValueError('This frozen Phase 2 configuration is HYPEUSDT only')
    core = clock_source(a.shared_core)
    module = bind_clock(a.shared_core)
    cfg = Config(a.initial_equity, a.risk_fraction, a.fee_bps, a.max_leverage, 300000,
                 tp1_r=None if comparison else 1.0, tp1_fraction=None if comparison else 0.1)
    from utils.engine.adapters.paper_execution import validate_config
    validate_config(cfg)
    oos = frozen_direction_oos(a.model_config, asdict(cfg)) if comparison and not price_only and len(a.model_config) > 1 else None
    window = tuple(date.fromisoformat(a.model_config[k]) for k in ('oos_start', 'oos_end')) if price_only else oos[:2] if oos else None
    metadata = dict(run_id=a.output_dir.name, model_id='trend_following_preny_profile_15m', model_version='phase2_hype_c1_v1', submodel_id='c1_delta', iteration_id='ITER-001', checkpoint_id=None, legacy_checkpoint='TF-PRENY-PAPER-001', phase='backtest', status='running', venue='binance', product='linear_usdt_margined_futures', instrument='HYPEUSDT', quantity_unit='HYPE', pnl_unit='USDT', venue_product_provenance='User-declared historical dataset; filename alone does not prove venue/product', config=asdict(cfg), selection='Frozen contract; no parameter/asset search', prior_inspection='Historical sample used in prior model selection; not unseen OOS', reserved_oos=None, calibration='180 calendar days, minimum 25 valid candidates; 1d embargo; 60d blocks', strategy_config=dict(timezone='America/New_York', profile_window='01:00-09:00', profile_bins=50, value_fraction=0.7, confirmation_bars=2, entry_window='09:00-12:00 inclusive', stop='frozen Pre-NY POC', tp1_r=1.0, tp1_fraction=0.1, remainder='original stop or next-day 08:59 NY; no trail/breakeven'), calibration_unknown_policy='NO_TRADE; no full-sample/future thresholds or baseline fallback', timeframes=dict(base='1m measured', model_evaluation='15m', c1_atr='1h legacy formula'), availability='Historical completed-candle-close assumption; no measured receipt latency', source=source_identity([source_path]))
    if comparison:
        metadata.update(model_version='phase2_hype_direction_v1', submodel_id=None, legacy_checkpoint=None,
                        selection='Four independent baseline/C1 x both/long-only research portfolios',
                        report_directional_expectancy=True)
        metadata['strategy_config'].update(tp1_r=None, tp1_fraction=None,
            remainder='full position: original stop or next-day 08:59 NY')
    if price_only:
        metadata.update(model_version='phase2_hype_price_only_v1',
                        selection='Price-only development comparison; two independent direction portfolios',
                        calibration=None, calibration_unknown_policy=None)
        metadata['strategy_config']['delta_gate'] = False
        metadata['timeframes'].pop('c1_atr')
    print('Phase 2: validating/reusing raw-derived minute data', flush=True)
    cache_root = a.cache_dir or Path(__file__).resolve().parents[3] / 'tmp/cache/observe/HYPEUSDT'
    minutes, cache = candle_cache([source_path], cache_root, id_policy=a.id_policy)
    if not {'buy_volume', 'sell_volume', 'delta'}.issubset(minutes):
        raise ValueError('C1 needs measured aggressor-side volume')
    lower, upper = (cache['source_start_ms'], cache['source_end_ms'])
    start, finish = (datetime.fromtimestamp(t / 1000, NY).date() for t in (lower, upper))
    if clock_ms(start, 1) < lower:
        start += timedelta(days=1)
    replay = {}
    if window:
        start, finish = window
        next_day = finish + timedelta(days=1)
        if start > finish or lower > clock_ms(start, 1) or upper < clock_ms(finish, 12):
            raise ValueError('Source does not cover the requested NY entry window')
        replay['replay_window'] = (clock_ms(start, 9), min(upper + 1, clock_ms(next_day, 9)))
        deadline = clock_ms(next_day, 8, 59)
        period = dict(start=str(start), end=str(finish), timezone='America/New_York')
        if oos:
            frozen = oos[2]
            metadata.update(calibration=dict(policy='frozen_pre_oos_no_refit', receipt=frozen), reserved_oos=period)
        else:
            metadata['development_window'] = period
        metadata.update(directional_block_months=1, final_session_exit_coverage=dict(
                            deadline_ms=deadline, source_end_ms=upper, reaches_deadline=upper >= deadline))
        if upper < deadline:
            print('Phase 2: source ends before last session time exit; unresolved positions will be omitted', flush=True)
    print(f'Phase 2: exact profiles and baseline candidates {start} through {finish}', flush=True)
    profile_sets, bars, _ = prepare_aggtrades(source_path, start, finish)
    profiles = [r for r in profile_sets['pre_ny'] if r['profile_start_timestamp_ms'] >= lower and r['profile_end_timestamp_ms'] <= upper]
    grouped = defaultdict(list)
    for bar in bars:
        if bar['close_timestamp_ms'] <= upper:
            grouped[str(datetime.fromtimestamp(bar['open_timestamp_ms'] / 1000, NY).date())].append(bar)
    if not price_only:
        hourly = hourly_indicators(minutes)
        ends = hourly.end_ms.to_numpy(dtype='int64')
        hour_bars = minutes.set_index(pd.to_datetime(minutes.timestamp_ms, unit='ms', utc=True)).resample('1h').agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'))
        hour_bars = hour_bars.dropna(subset=['open', 'high', 'low', 'close'])
        hour_bars = hour_bars[hour_bars.index >= pd.to_datetime((lower + 3599999) // 3600000 * 3600000, unit='ms', utc=True)]
    candidates, baseline, coverage_blocked, unavailable_atr, invalid_fill = ([], [], 0, 0, 0)
    raw = RawPrints([source_path])
    try:
        for profile in profiles:
            day, session = (profile['session_day'], grouped[profile['session_day']])
            signals, _ = evaluate_session(profile, session, symbol='HYPEUSDT', require_delta=not price_only)
            if not signals:
                continue
            signal = signals[0]
            asof = signal['feature_as_of_ms']
            recent = [b for b in session if b['close_timestamp_ms'] <= asof]
            frame = pd.DataFrame([{**{k: b[k] for k in ('open', 'high', 'low', 'close')}, 'timestamp_ms': b['open_timestamp_ms'], 'volume': b['buy_volume'] + b['sell_volume']} for b in recent])
            try:
                ctx = module.CandleContext(frame, as_of_ms=asof, base_timeframe='15m')
                ctx.candles('15m')
                if len(recent) != (asof - clock_ms(date.fromisoformat(day), 9)) // BAR_MS:
                    raise module.CandleNotReady('Incomplete NY evaluation coverage')
            except module.CandleNotReady:
                coverage_blocked += 1
                continue
            c1_ready = False
            if not price_only:
                try:
                    module.CandleContext(hour_bars.loc[:pd.to_datetime(asof, unit='ms', utc=True)], as_of_ms=asof, base_timeframe='1h').candles('1h')
                    hourly_ready = True
                except module.CandleNotReady:
                    hourly_ready = False
                    if not comparison:
                        coverage_blocked += 1
                        continue
                i = int(np.searchsorted(ends, asof, side='right')) - 1
                atr = float(hourly.iloc[i].atr14) if i >= 0 else float('nan')
                c1_ready = hourly_ready and np.isfinite(atr) and atr > 0 and 0 <= asof - int(ends[i]) < 3600000
                if not c1_ready:
                    unavailable_atr += 1
                    if not comparison:
                        continue
            ref = raw.first(asof)
            if ref[0] >= len(raw.groups):
                invalid_fill += 1
                continue
            tick = raw.tick(ref)
            sign = 1 if signal['direction'] == 'long' else -1
            if tick['timestamp_ms'] > min(signal['entry_deadline_timestamp_ms'], asof + 300000) or sign * (tick['price'] - signal['stop']) <= 0:
                invalid_fill += 1
                continue
            signal.update(feature_available_at_ms=asof, decision_timestamp_ms=asof)
            if comparison:
                baseline.append(signal)
            if not c1_ready:
                continue
            last = recent[-4:]
            volume = sum((b['buy_volume'] + b['sell_volume'] for b in last))
            features = dict(directional_delta_imbalance=sign * sum((b['buy_volume'] - b['sell_volume'] for b in last)) / volume if volume else None, directional_result_atr=sign * (last[-1]['close'] - last[0]['open']) / atr)
            candidates.append(dict(sample_id=signal['sample_id'], session_day=day, features=features, signal=signal))
    finally:
        raw.close()
    if price_only:
        selected, folds, counts = baseline, [], dict(candidates=len(baseline))
    elif oos:
        selected = [{**row['signal'], 'c1_fold': frozen['fold_number']} for row in candidates
                    if _gate(row['features'], 'delta_without_result', frozen['thresholds'])]
        folds = []
        counts = dict(candidates=len(candidates), warmup_or_uncalibrated=0, unavailable_features=0,
                      rejected_c1=len(candidates) - len(selected))
    else:
        selected, folds, counts = c1_calibrated_signals(candidates)
    metadata.update(start_date=str(start), end_date=str(finish), calibration_folds=folds, counts={**counts, 'coverage_blocked': coverage_blocked, 'unavailable_atr': unavailable_atr, 'invalid_first_fill': invalid_fill, 'selected_signals': len(selected)}, cache=cache)
    if comparison:
        # Both C1 lanes share the same all-direction causal calibration.
        populations = (('price_only', baseline),) if price_only else (('baseline', baseline), ('c1', selected))
        selected = {name: [s for s in population if side == 'both' or s['direction'] == 'long']
                    for label, population in populations
                    for side in ('both', 'long_only') for name in (f'{label}_{side}',)}
        metadata['counts']['signals_by_variant'] = {name: len(rows) for name, rows in selected.items()}
    metadata['configuration_id'] = hashlib.sha256(json.dumps({k: metadata[k] for k in ('config', 'strategy_config', 'calibration', 'calibration_unknown_policy')}, sort_keys=True).encode()).hexdigest()
    dependencies = {Path(__file__), core}
    for name in ('model_development.phase_2.backtest', 'model_development.phase_2.backtest', 'model_development.phase_1.observe', 'model_development.observation_cache', 'utils.engine.adapters.paper_execution', 'indicator.legacy_preny', 'indicator.value_area', 'utils.market_session'):
        dependencies.add(Path(sys.modules[name].__file__))
    metadata['code'] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(dependencies)}
    if source_identity([source_path]) != metadata['source']:
        raise ValueError('Source changed during preparation')
    return dict(signals=selected, config=cfg, start_day=start, end_day=finish, metadata=metadata,
                timing_validator=module.validate_signal_timing, **replay)
