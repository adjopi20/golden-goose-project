"""Challenger 2 stage 1: causal annotations only, never trading decisions."""
from collections import Counter, defaultdict, deque
from datetime import date
import hashlib
import json
from pathlib import Path
import statistics

import numpy as np
import pandas as pd

from .audit_opportunity import _finite, _write_jsonl, hourly_indicators, load_minutes
from .strategy import clock_ms

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


def export_effort_result(context_dir: Path, output_dir: Path) -> dict:
    """Add C2 annotations to the existing single candidate ledger, all assets."""
    from .evaluate_plugs import read_jsonl, _bars_by_day, slot
    if output_dir.resolve()==context_dir.resolve() or output_dir.exists():
        raise FileExistsError('Use a fresh output directory; source outputs are immutable')
    manifest=json.loads((context_dir/'manifest.json').read_text(encoding='utf-8'))
    candidates=read_jsonl(context_dir/'candidate_context.jsonl')
    source_candidates=defaultdict(list)
    for row in candidates: source_candidates[row['symbol']].append(row)
    all_timeline=[];enriched=[];coverage=[]
    for source in manifest['sources']:
        symbol=source['symbol']
        print(f'{symbol}: annotating cached 15m sessions (no raw replay)',flush=True)
        obs=Path(source['observation_dir'])
        for cached in source['minute_caches']:
            stat=Path(cached['path']).stat()
            if ('bytes' in cached and stat.st_size!=cached['bytes']) or ('mtime_ns' in cached and stat.st_mtime_ns!=cached['mtime_ns']):
                raise ValueError(f'Minute cache changed since source context: {cached["path"]}; regenerate context before comparing')
        bars_by_day=_bars_by_day(obs)
        profiles={p['session_day']:p for p in read_jsonl(obs/'prepared_profiles.jsonl') if p['profile_window']=='pre_ny'}
        hours=hourly_indicators(load_minutes([Path(x['path']) for x in source['minute_caches']]))
        ends=hours.end_ms.to_numpy();atrs=hours.atr14.to_numpy()
        history=defaultdict(lambda:deque(maxlen=20))
        day_lookup={};session_count=0
        for day in sorted(profiles):
            bars=[b for b in bars_by_day.get(day,[]) if 36<=slot(int(b['open_timestamp_ms']))<48]
            refs={}
            for quarter in range(12):
                past=history[quarter]
                refs[quarter]={'prior_sessions':len(past)}
                if len(past)>=10:
                    refs[quarter].update({k:statistics.median(x[k] for x in past) for k in
                                         ('volume','buy_volume','sell_volume','body')})
            atr_map={}
            for b in bars:
                stamp=int(b['open_timestamp_ms']);index=int(np.searchsorted(ends,stamp,side='right'))-1
                # No stale ATR over a missing full clock hour.
                if index>=0 and 0<=stamp-int(ends[index])<3_600_000 and np.isfinite(atrs[index]) and atrs[index]>0:
                    atr_map[stamp]=float(atrs[index])
            timeline=annotate_session(day,bars,profiles[day],refs,atr_map)
            for line in timeline: line['symbol']=symbol
            day_lookup[day]={r['feature_as_of_ms']:r for r in timeline}
            all_timeline.extend(timeline);session_count+=1
            for b in bars:
                quarter=slot(int(b['open_timestamp_ms']))-36
                history[quarter].append(dict(volume=float(b['buy_volume'])+float(b['sell_volume']),
                    buy_volume=float(b['buy_volume']),sell_volume=float(b['sell_volume']),
                    body=abs(float(b['close'])-float(b['open']))))
        for original in source_candidates[symbol]:
            day=original['session_day'];asof=int(original['feature_as_of_ms'])
            if asof>int(original['entry_ms']): raise ValueError('Signal after entry')
            snapshot=day_lookup.get(day,{}).get(asof)
            if snapshot is None or snapshot['status']!='observed':
                raise ValueError(f'Missing signal-time evidence: {symbol} {day}')
            if not np.isclose(snapshot['close'],original['audit_context']['price'],rtol=1e-10,atol=1e-8):
                raise ValueError(f'Input versions differ: {symbol} {day}')
            record=dict(original)
            # This is the only annotation joined to outcomes for research summaries.
            record['effort_result_pre_entry']={k:v for k,v in snapshot.items() if k!='directions'}
            record['effort_result_pre_entry']['setup_direction']=snapshot['directions'][original['direction']]
            enriched.append(record)
        coverage.append(dict(symbol=symbol,sessions=session_count,candidates=len(source_candidates[symbol])))
    if len(enriched)!=len(candidates): raise ValueError('Candidate population changed')
    output_dir.mkdir(parents=True)
    _write_jsonl(output_dir/'candidate_context.jsonl',enriched)
    pd.json_normalize(_finite(enriched),sep='__').to_csv(output_dir/'candidate_context.csv',index=False)
    # Timeline includes no-entry sessions and later bars, deliberately outside the pre-entry ledger.
    entry_times={(r['symbol'],r['session_day']):r['entry_ms'] for r in candidates}
    for line in all_timeline:
        entry=entry_times.get((line['symbol'],line['session_day']))
        line['audit_phase']=('NO_BASELINE_ENTRY' if entry is None else
                             'PRE_ENTRY' if line['feature_as_of_ms']<=entry else 'POST_ENTRY')
    _write_jsonl(output_dir/'session_timeline.jsonl',all_timeline)
    summary=[]
    for symbol in sorted(source_candidates):
        selected=[r for r in enriched if r['symbol']==symbol]
        for scope in ('all_candidates','existing_test_folds'):
            cohort=[r for r in selected if scope=='all_candidates' or 'delta_without_result' in r['research_gates']]
            for label in ('ALL',*LABELS,'SUPPORT_PERSISTING'):
                group=[r for r in cohort if label=='ALL' or label in r['effort_result_pre_entry']['setup_direction']['labels']
                       or label==r['effort_result_pre_entry']['setup_direction']['state']]
                values=[r['outcomes']['net_r'] for r in group]
                losses=-sum(v for v in values if v<0)
                mfes=[r['outcomes']['path'].get('mfe_before_stop_lower_pct') for r in group]
                mfes=[v for v in mfes if v is not None]
                summary.append(dict(symbol=symbol,scope=scope,label=label,n=len(group),
                    expectancy_r=statistics.mean(values) if values else None,
                    win_rate=sum(v>0 for v in values)/len(values) if values else None,
                    pf_r=sum(v for v in values if v>0)/losses if losses else None,
                    mean_mfe_pct=statistics.mean(mfes) if mfes else None))
    pd.DataFrame(summary).to_csv(output_dir/'annotation_summary.csv',index=False)
    new_manifest={**manifest,'schema_version':2,'annotation_version':'effort_result_stateful_v1',
        'source_context':str(context_dir.resolve()),
        'source_context_sha256':hashlib.sha256((context_dir/'candidate_context.jsonl').read_bytes()).hexdigest(),
        'annotation_parameters':dict(same_clock_history=20,min_reference_sessions=10,recent_bars=4,
                                     fading_bars=3,support_streak_label=2),
        'annotation_scope':'Stage 1 observation only: no filtering, replacement entry or replay',
        'annotation_normalization':'Same-clock prior-session medians; ATR14 1h known at bar OPEN; no MA/ADX input',
        'annotation_timeline':'09:15 through 12:00 NY bar ends, including sessions without baseline entries; later bars must not be used as entry features',
        'annotation_summary':'Overlapping labels, source baseline time-exit outcomes only, descriptive not significance or new strategy performance',
        'annotation_coverage':coverage}
    (output_dir/'manifest.json').write_text(json.dumps(new_manifest,indent=2),encoding='utf-8')
    return dict(output_dir=str(output_dir),coverage=coverage,entry_stop_exit_changed=False)
