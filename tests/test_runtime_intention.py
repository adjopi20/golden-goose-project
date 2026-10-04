from datetime import date
from models.trend_following_preny_profile_15m.runtime.baseline import evaluate_session, clock_ms, BAR_MS
from models.trend_following_preny_profile_15m.runtime.intention import as_intention


def test_intention_adapter_preserves_selected_signal_and_risk():
    day=date(2026,10,3)
    start=clock_ms(day,9)
    profile=dict(session_day=str(day),profile_window='pre_ny',profile_start_timestamp_ms=clock_ms(day,1),
                 profile_end_timestamp_ms=start,val=90,poc=100,vah=110)
    bars=[dict(open_timestamp_ms=start+i*BAR_MS,close_timestamp_ms=start+(i+1)*BAR_MS,
               open=110+i,close=111+i,high=112+i,low=109+i,buy_volume=10,sell_volume=2) for i in range(2)]
    signal=evaluate_session(profile,bars,symbol='ETHUSDC')[0][0]
    inputs=dict(venue='binance',product='futures',instrument_id='ETHUSDC',environment='server-paper',
                account_id='paper',allocation_id='eth-v1',contract_version='eth-001',config_hash='abc',
                feature_version='v1',calibration_id='not-used',decision_snapshot_ref='decision:1')
    result=dict(selected=True,signal=signal,risk_fraction=.0025)
    intent=as_intention(result,**inputs)
    assert intent['initial_stop']==signal['stop']
    assert intent['risk_fraction']==.0025
    assert intent['force_exit_timestamp_ms']==signal['force_exit_timestamp_ms']
    assert as_intention({**result,'selected':False},**inputs) is None
    assert as_intention(dict(signal=None),**inputs) is None
