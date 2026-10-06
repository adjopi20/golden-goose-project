"""V1 transport contracts for future collectors/account owners. No IO.

Legacy workers keep their existing signal/DB formats during Phase 1. These
contracts are validated at the new service boundary in subsequent phases.
"""
from copy import deepcopy
import hashlib
import json
import math

SCHEMA_VERSION = 1
EVENT_TYPES = {'trade', 'completed_bar', 'profile', 'coverage', 'quote', 'mark_price', 'funding', 'instrument', 'evaluation_boundary'}
QUALITY = {'complete', 'partial', 'gap', 'stale', 'invalid'}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _text(row, key):
    if not isinstance(row.get(key), str) or not row[key].strip():
        raise ValueError(f'Missing/invalid {key}')


def _integer(row, key):
    if type(row.get(key)) is not int or row[key] < 0:
        raise ValueError(f'Missing/invalid {key}')


def _base(row):
    if type(row.get('schema_version')) is not int or row['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported schema_version')
    for key in ('venue', 'product', 'instrument_id'):
        _text(row, key)
    if row['product'] not in ('spot', 'futures'):
        raise ValueError('Unknown product')


def validate_event(row):
    _base(row)
    for key in ('event_id', 'event_type', 'source', 'feature_version'):
        _text(row, key)
    if row['event_type'] not in EVENT_TYPES or row.get('quality') not in QUALITY:
        raise ValueError('Unknown event_type/quality')
    for key in ('event_timestamp_ms', 'received_timestamp_ms', 'available_at_ms', 'stream_sequence', 'revision'):
        _integer(row, key)
    if not row['event_timestamp_ms'] <= row['available_at_ms'] <= row['received_timestamp_ms']:
        raise ValueError('Invalid event availability chronology')
    if 'source_sequence' not in row or (row['source_sequence'] is not None
            and (type(row['source_sequence']) not in (int,str) or str(row['source_sequence']) == ''
                 or (type(row['source_sequence']) is int and row['source_sequence'] < 0))):
        raise ValueError('Invalid source_sequence')
    if not isinstance(row.get('payload'), dict):
        raise ValueError('Event payload must be an object')
    if row['event_type'] == 'completed_bar':
        for key in ('open_timestamp_ms','close_timestamp_ms'):
            _integer(row['payload'], key)
        if not row['payload']['open_timestamp_ms'] < row['payload']['close_timestamp_ms'] <= row['available_at_ms']:
            raise ValueError('Unfinished completed_bar')
    if row['event_type'] == 'evaluation_boundary':
        for key in ('as_of_ms', 'coverage_start_ms', 'complete_from_ms', 'next_event_ms'):
            _integer(row['payload'], key)
        p = row['payload']
        if (p['as_of_ms'] != row['event_timestamp_ms'] or p['as_of_ms'] % 900_000
                or not p['as_of_ms'] <= p['next_event_ms'] <= row['available_at_ms']):
            raise ValueError('Invalid source-confirmed evaluation boundary')
    canonical_json(row)
    return deepcopy(row)


def make_event(**fields):
    row = {'schema_version':SCHEMA_VERSION, 'source_sequence':None, 'revision':0, **fields}
    identity = {key:row[key] for key in ('venue','product','instrument_id','source','event_type',
                                        'event_timestamp_ms','source_sequence','revision')}
    if identity['source_sequence'] is None:
        identity['stream_sequence'] = row['stream_sequence']
    row['event_id'] = 'event-v1:' + content_hash(identity)
    return validate_event(row)


def validate_intention(row):
    _base(row)
    for key in ('intention_id','environment','account_id','allocation_id','model_id',
                'model_version','contract_version','config_hash','feature_version',
                'calibration_id','decision_snapshot_ref','session_id','exit_policy'):
        _text(row, key)
    if row['environment'] not in ('local-dev','server-paper','server-live'):
        raise ValueError('Unknown environment')
    if row.get('direction') not in ('long','short'):
        raise ValueError('Unknown direction')
    for key in ('feature_as_of_ms','signal_timestamp_ms','eligible_timestamp_ms',
                'expires_timestamp_ms','force_exit_timestamp_ms'):
        _integer(row,key)
    if not (row['feature_as_of_ms'] <= row['signal_timestamp_ms'] <= row['eligible_timestamp_ms']
            <= row['expires_timestamp_ms'] < row['force_exit_timestamp_ms']):
        raise ValueError('Invalid intention chronology')
    for key in ('risk_fraction','initial_stop','entry_reference'):
        value = row.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'Invalid {key}')
    if row['risk_fraction'] > 1:
        raise ValueError('Risk fraction exceeds equity')
    sign = 1 if row['direction']=='long' else -1
    if sign * (row['entry_reference']-row['initial_stop']) <= 0:
        raise ValueError('Invalid reference stop geometry')
    canonical_json(row)
    return deepcopy(row)


def make_intention(**fields):
    row = dict(schema_version=SCHEMA_VERSION, **fields)
    # Stable identity for a candidate; changing its proposal is not a new order.
    identity = {key:row[key] for key in ('environment','venue','product','instrument_id',
                                        'account_id','allocation_id','model_id','model_version',
                                        'contract_version','session_id','signal_timestamp_ms','direction')}
    row['intention_id'] = 'intention-v1:' + content_hash(identity)
    return validate_intention(row)
