from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from model_development.models.trend_following_preny_profile_15m.legacy.effort_result import annotate_session, export_effort_result
from model_development.models.trend_following_preny_profile_15m.legacy.strategy import clock_ms
from datetime import date

DAY='2026-08-19'
START=clock_ms(date.fromisoformat(DAY),9)
PROFILE=dict(session_day=DAY,profile_window='pre_ny',val=97.,poc=99.,vah=101.)
REF={i:dict(prior_sessions=20,volume=80.,buy_volume=40.,sell_volume=40.,body=1.) for i in range(12)}
ATR={START+i*900_000:2. for i in range(12)}


def bar(i,o,h,l,c,buy=80.,sell=20.):
    return dict(open_timestamp_ms=START+i*900_000,close_timestamp_ms=START+(i+1)*900_000,
                open=o,high=h,low=l,close=c,buy_volume=buy,sell_volume=sell)


def test_initiative_persists_then_absorption_resets_support():
    bars=[bar(0,100,103,99,102),bar(1,102,105,101,104,90),bar(2,104,105,103,103.5,100)]
    out=annotate_session(DAY,bars,PROFILE,REF,ATR)
    assert out[0]['directions']['long']['labels']==['initiative']
    assert out[1]['directions']['long']['state']=='SUPPORT_PERSISTING'
    assert out[1]['directions']['long']['recent_label_counts']['initiative']==2
    assert out[2]['directions']['long']['labels']==['possible_absorption']
    assert out[2]['directions']['long']['support_streak']==0


def test_low_volume_with_good_result_is_path_not_fading():
    row=annotate_session(DAY,[bar(0,100,104,100,103,15,5)],PROFILE,REF,ATR)[0]
    assert row['directions']['long']['labels']==['path_of_least_resistance']


def test_fading_requires_degrading_result_not_only_delta():
    bars=[bar(0,101,105,101,104,100,10),bar(1,104,107,104,106,90,10),bar(2,106,108,106,107,80,10)]
    out=annotate_session(DAY,bars,PROFILE,REF,ATR)
    assert 'participation_fading' in out[2]['directions']['long']['labels']
    bars[2]=bar(2,106,111,106,110,80,10)
    assert 'participation_fading' not in annotate_session(DAY,bars,PROFILE,REF,ATR)[2]['directions']['long']['labels']


def test_reentry_starts_new_episode_without_old_support():
    bars=[bar(0,100,103,99,102),bar(1,102,105,101,104),bar(2,104,104,99,100),bar(3,100,104,100,103)]
    out=annotate_session(DAY,bars,PROFILE,REF,ATR)
    assert out[2]['directions']['long']['state']=='REBALANCING'
    last=out[3]['directions']['long']
    assert last['episode_id']==2 and last['evidence_bars']==1 and last['support_streak']==1


def test_future_bars_do_not_change_prior_annotations_or_inputs():
    bars=[bar(0,100,103,99,102),bar(1,102,105,101,104)]
    saved=deepcopy(bars)
    first=annotate_session(DAY,bars,PROFILE,REF,ATR)
    extended=annotate_session(DAY,bars+[bar(2,104,500,90,400)],PROFILE,REF,ATR)
    assert first[:2]==extended[:2] and bars==saved


def test_long_short_mirror():
    bars=[bar(0,100,103,99,102),bar(1,102,105,101,104)]
    reflected=[]
    for b in bars:
        reflected.append({**b,'open':200-b['open'],'close':200-b['close'],
            'high':200-b['low'],'low':200-b['high'],'buy_volume':b['sell_volume'],'sell_volume':b['buy_volume']})
    mirrored={**PROFILE,'val':200-PROFILE['vah'],'vah':200-PROFILE['val'],'poc':200-PROFILE['poc']}
    lhs=annotate_session(DAY,bars,PROFILE,REF,ATR)
    rhs=annotate_session(DAY,reflected,mirrored,REF,ATR)
    for a,b in zip(lhs[:2],rhs[:2]):
        assert a['directions']['long']['labels']==b['directions']['short']['labels']
        assert a['directions']['long']['state']==b['directions']['short']['state']


def test_missing_bars_and_history_are_unknown_not_rejection():
    out=annotate_session(DAY,[bar(0,100,103,99,102),bar(2,102,105,101,104)],PROFILE,{},ATR)
    assert out[0]['directions']['long']['state']=='UNKNOWN_REFERENCE'
    assert out[1]['status']=='missing_bar'
    assert out[2]['directions']['long']['state']=='UNKNOWN_DATA'
    assert out[-1]['feature_as_of_ms']==START+12*900_000


def test_export_enriches_only_and_keeps_post_entry_separate(tmp_path):
    obs=tmp_path/'obs';obs.mkdir()
    context=tmp_path/'context';context.mkdir()
    bars=[bar(i,100+i*2,103+i*2,99+i*2,102+i*2) for i in range(12)]
    def write(path,rows): path.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    write(obs/'prepared_bars_15m.jsonl',bars)
    write(obs/'prepared_profiles.jsonl',[PROFILE])
    cache=tmp_path/'minutes.parquet'
    pd.DataFrame(dict(timestamp_ms=[START-(i+1)*60_000 for i in reversed(range(30*60))],
                      open=100.,high=101.,low=99.,close=100.,volume=1.,delta=.2)).to_parquet(cache)
    manifest=dict(sources=[dict(symbol='TEST',observation_dir=str(obs),minute_caches=[dict(path=str(cache))])])
    (context/'manifest.json').write_text(json.dumps(manifest))
    candidate=dict(symbol='TEST',session_day=DAY,feature_as_of_ms=START+900_000,entry_ms=START+900_001,
                   direction='long',audit_context={'price':102.},research_gates={},
                   outcomes={'net_r':-1.2,'path':{'mfe_before_stop_lower_pct':.1}},
                   stop=99.,features={'vwap_persistence':.2})
    write(context/'candidate_context.jsonl',[candidate])
    result=export_effort_result(context,tmp_path/'out')
    assert result['entry_stop_exit_changed'] is False
    output=json.loads((tmp_path/'out/candidate_context.jsonl').read_text())
    added=output.pop('effort_result_pre_entry')
    assert output==candidate
    assert added['feature_as_of_ms']<=candidate['entry_ms']
    timeline=[json.loads(x) for x in (tmp_path/'out/session_timeline.jsonl').read_text().splitlines()]
    assert timeline[0]['audit_phase']=='PRE_ENTRY' and timeline[1]['audit_phase']=='POST_ENTRY'
