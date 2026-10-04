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

from .runtime.effort_result import BAR_MS, LABELS, ratio, annotate_session


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
