"""Frozen causal effort/result annotations shared with research."""
from collections import Counter, deque
from datetime import date
import numpy as np
from trading_core.session import clock_ms


BAR_MS = 900_000


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
            output.append(dict(session_day=day, feature_as_of_ms=opening+BAR_MS,
                               status='missing_bar', session_coverage_complete=False))
            continue
        if int(bar['close_timestamp_ms']) != opening + BAR_MS:
            raise ValueError(f'Invalid quarter-hour end: {day}')
        o,h,l,c = (float(bar[k]) for k in ('open','high','low','close'))
        buy,sell = float(bar['buy_volume']),float(bar['sell_volume'])
        if not all(np.isfinite([o,h,l,c,buy,sell])) or min(o,h,l,c)<=0 or min(buy,sell)<0 or not l<=min(o,c)<=max(o,c)<=h:
            raise ValueError(f'Invalid OHLC/volume: {day}, {opening}')
        total,delta = buy+sell,buy-sell
        cvd += delta
        pv += (h+l+c)/3*total
        volume += total
        vwap = pv/volume if volume > 0 else None
        atr = atr_before.get(opening)
        ref = references.get(i,{})
        span = h-l
        line = dict(session_day=day, feature_as_of_ms=opening+BAR_MS, status='observed',
            session_coverage_complete=complete, open=o,high=h,low=l,close=c,
            buy_volume=buy,sell_volume=sell,delta=delta,cvd_since_0900=cvd,
            vwap_since_0900_bar_approx=vwap,atr14_1h_before_bar=atr,
            relative_volume=ratio(total,ref.get('volume')),reference=ref,
            body_atr=ratio(abs(c-o),atr),range_atr=ratio(span,atr),
            upper_wick_fraction=ratio(h-max(o,c),span),lower_wick_fraction=ratio(min(o,c)-l,span),
            directions={})
        for direction,sign in [('long',1),('short',-1)]:
            hist=histories[direction]
            edge=float(profile['vah'] if sign==1 else profile['val'])
            outside=sign*(c-edge)>0
            was_outside=bool(hist and hist[-1]['outside_value'])
            if (outside and not was_outside) or (was_outside and not outside):
                hist.clear()  # earlier opposing/inside-value evidence cannot accumulate forever
                streak[direction]=outside_streak[direction]=0
                if outside: episode[direction]+=1
            body=sign*(c-o)
            progress=sign*(c-(float(previous['close']) if previous else o))
            close_position=((c-l)/span if sign==1 else (h-c)/span) if span>0 else .5
            adverse_wick=((h-max(o,c)) if sign==1 else (min(o,c)-l))
            rv=ratio(buy if sign==1 else sell,ref.get('buy_volume' if sign==1 else 'sell_volume'))
            relative_body=ratio(abs(c-o),ref.get('body'))
            vwap_margin=sign*(c-vwap) if vwap is not None else None
            good_result=(body>0 and progress>0 and close_position>.5 and
                         relative_body is not None and relative_body>=1)
            weak_result=(body<=0 or progress<=0 or close_position<=.5 or
                         (relative_body is not None and relative_body<1 and adverse_wick>=abs(c-o)))
            directional_delta=sign*delta
            initiative=rv is not None and rv>=1 and directional_delta>0 and good_result
            path=(line['relative_volume'] is not None and line['relative_volume']<1
                  and directional_delta>0 and good_result)
            absorption=(rv is not None and rv>=1 and directional_delta>0 and weak_result)
            prior=list(hist)[-2:]
            fading=(len(prior)==2 and prior[0]['directional_delta']>prior[1]['directional_delta']>directional_delta>0
                    and prior[0]['progress']>prior[1]['progress']>progress
                    and prior[0]['aggressive_volume']>prior[1]['aggressive_volume']>(buy if sign==1 else sell))
            peak=max((x['favorable_extreme'] for x in hist),default=sign*(h if sign==1 else l))
            peak_cvd=next((x['directional_cvd'] for x in reversed(hist) if x['favorable_extreme']==peak),None)
            extreme=sign*(h if sign==1 else l)
            divergence=peak_cvd is not None and extreme>peak and sign*cvd<peak_cvd and weak_result
            labels=[name for name,flag in zip(LABELS,[initiative,absorption,path,fading]) if flag]
            supported=(outside and vwap_margin is not None and vwap_margin>0 and (initiative or path)
                       and not absorption and not fading and not divergence and complete)
            streak[direction]=streak[direction]+1 if supported else 0
            outside_streak[direction]=outside_streak[direction]+1 if outside else 0
            record=dict(episode_id=episode[direction],outside_value=outside,
                edge=edge,edge_distance_atr=ratio(sign*(c-edge),atr),
                directional_delta=directional_delta,directional_cvd=sign*cvd,
                aggressive_volume=buy if sign==1 else sell,relative_aggressive_volume=rv,
                directional_body_atr=ratio(body,atr),progress=progress,progress_atr=ratio(progress,atr),
                close_position=close_position,relative_body=relative_body,
                favorable_extreme=extreme,retracement_atr=ratio(max(0,max(peak,extreme)-sign*c),atr),
                vwap_distance_atr=ratio(vwap_margin,atr) if vwap_margin is not None else None,
                cvd_price_divergence_with_weak_result=divergence,
                labels=labels,reference_ready=rv is not None and relative_body is not None,
                support_streak=streak[direction],outside_close_streak=outside_streak[direction])
            hist.append(record)
            record['recent_label_counts']=dict(Counter(label for x in hist for label in x['labels']))
            record['recent_directional_delta']=sum(x['directional_delta'] for x in hist)
            record['evidence_bars']=len(hist)
            record['state']=('UNKNOWN_DATA' if not complete else 'UNKNOWN_REFERENCE' if not record['reference_ready'] else
                'REBALANCING' if was_outside and not outside else 'INSIDE_VALUE' if not outside else
                'SUPPORT_PERSISTING' if streak[direction]>=2 else 'BUILDING_SUPPORT' if supported else 'WAIT')
            line['directions'][direction]=record
        output.append(line)
        previous=bar
    return output
