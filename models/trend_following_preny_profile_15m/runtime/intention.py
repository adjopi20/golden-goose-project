"""Explicit adapter to V1 intentions; not wired to legacy execution yet."""
from trading_core.contracts import make_intention


def as_intention(result, *, venue, product, instrument_id, environment, account_id,
                 allocation_id, contract_version, config_hash, feature_version,
                 calibration_id, decision_snapshot_ref):
    if result.get('signal') is None or result.get('selected') is not True:
        return None
    signal = result['signal']
    return make_intention(
        venue=venue, product=product, instrument_id=instrument_id,
        environment=environment, account_id=account_id, allocation_id=allocation_id,
        model_id=signal['route'], model_version=signal['strategy'],
        contract_version=contract_version, config_hash=config_hash,
        feature_version=feature_version, calibration_id=calibration_id,
        decision_snapshot_ref=decision_snapshot_ref, session_id=signal['session_day'],
        direction=signal['direction'], feature_as_of_ms=signal['feature_as_of_ms'],
        signal_timestamp_ms=signal['signal_timestamp_ms'],
        eligible_timestamp_ms=signal['entry_eligible_timestamp_ms'],
        expires_timestamp_ms=signal['entry_deadline_timestamp_ms'],
        force_exit_timestamp_ms=signal['force_exit_timestamp_ms'],
        risk_fraction=result['risk_fraction'], initial_stop=signal['stop'],
        entry_reference=signal['entry_reference'], exit_policy=signal['exit_policy'])
