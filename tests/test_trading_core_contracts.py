from copy import deepcopy
from datetime import date
import pytest

from trading_core.contracts import make_event, make_intention, validate_event, validate_intention
from trading_core.session import clock_ms
from trading_core.profiles import value_area


def event(**changes):
    fields = dict(venue='binance',product='futures',instrument_id='ETHUSDC',source='aggTrades',
                  feature_version='v1',event_type='completed_bar',event_timestamp_ms=900000,
                  received_timestamp_ms=900002,available_at_ms=900001,stream_sequence=7,
                  source_sequence='10', quality='complete',payload=dict(open_timestamp_ms=0,close_timestamp_ms=900000))
    return make_event(**{**fields,**changes})


def intention(**changes):
    fields = dict(venue='binance',product='futures',instrument_id='ETHUSDC',environment='server-paper',
                  account_id='paper',allocation_id='eth-v1',model_id='preny',model_version='v1',
                  contract_version='eth-001',config_hash='abc',feature_version='v1',calibration_id='not-used',
                  decision_snapshot_ref='decision:1',session_id='2026-10-03',exit_policy='initial_stop_or_time',
                  direction='long',feature_as_of_ms=1,signal_timestamp_ms=1,eligible_timestamp_ms=2,
                  expires_timestamp_ms=5,force_exit_timestamp_ms=9,risk_fraction=.005,initial_stop=100,entry_reference=101)
    return make_intention(**{**fields,**changes})


def test_event_deduplication_and_venue_revision_isolation():
    first=event()
    assert first['event_id']==event(received_timestamp_ms=900005,stream_sequence=99)['event_id']
    assert first['event_id']!=event(venue='lighter',instrument_id='0')['event_id']
    assert first['event_id']!=event(revision=1)['event_id']
    assert event(source_sequence=None)['event_id']!=event(source_sequence=None,stream_sequence=8)['event_id']


@pytest.mark.parametrize('changes',[dict(available_at_ms=899999),dict(schema_version=2),dict(quality='invented'),
    dict(payload={'open_timestamp_ms':0,'close_timestamp_ms':999999}),dict(stream_sequence=True),dict(source_sequence=-1),dict(payload={'value':float('nan')})])
def test_invalid_event_rejected(changes):
    with pytest.raises(ValueError): event(**changes)


def test_intention_identity_stable_for_same_candidate_and_isolated_allocations():
    original=intention()
    assert original['intention_id']==intention(risk_fraction=.0025)['intention_id']
    assert original['intention_id']!=intention(allocation_id='other')['intention_id']
    assert original['intention_id']!=intention(venue='lighter',instrument_id='0')['intention_id']
    before=deepcopy(original)
    assert validate_intention(original)==before and original==before


@pytest.mark.parametrize('changes',[dict(initial_stop=102),dict(risk_fraction=True),dict(risk_fraction=2),
    dict(feature_as_of_ms=3),dict(eligible_timestamp_ms=6),dict(force_exit_timestamp_ms=5)])
def test_invalid_intention_rejected(changes):
    with pytest.raises(ValueError): intention(**changes)


def test_dst_uses_new_york_not_fixed_utc_offset():
    assert clock_ms(date(2026,3,8),9)-clock_ms(date(2026,3,7),9)==23*3600000
    assert clock_ms(date(2026,11,1),9)-clock_ms(date(2026,10,31),9)==25*3600000


def test_profile_ties_and_single_price_preserved():
    result=value_area({100.:2.})
    assert result['val']==result['poc']==result['vah']==100.
    assert value_area({100.:10.,101.:10.,102.:1.},profile_bins=3)['poc']==pytest.approx(100+1/3)
