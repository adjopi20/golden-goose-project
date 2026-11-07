import json
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from model_development.models.trend_following_preny_profile_15m.legacy import strategy as model
from model_development.phase_2 import backtest as engine


def candidate(day, imbalance=.5, result=1.):
    return dict(sample_id=str(day), session_day=str(day),
        features=dict(directional_delta_imbalance=imbalance, directional_result_atr=result),
        signal=dict(sample_id=str(day), session_day=str(day)))


def test_c1_no_warmup_trade_frozen_train_and_future_invariance():
    first = date(2025, 1, 1)
    rows = [candidate(first+timedelta(days=i)) for i in range(250)]
    accepted, folds, counts = model.c1_calibrated_signals(rows)
    assert accepted[0]['session_day'] == str(first+timedelta(days=181))
    assert len(accepted) == 69 and counts['warmup_or_uncalibrated'] == 181
    assert folds[0]['train_candidates'] == 180
    changed = [dict(r) for r in rows]
    changed[200] = candidate(first+timedelta(days=200), imbalance=100., result=-100.)
    later, refits, _ = model.c1_calibrated_signals(changed)
    assert refits[0]['thresholds'] == folds[0]['thresholds']
    assert [s for s in accepted if s['session_day'] < changed[200]['session_day']] == [s for s in later if s['session_day'] < changed[200]['session_day']]
    assert changed[200]['sample_id'] not in {s['sample_id'] for s in later}


def test_c1_minimum_and_unavailable_features_are_no_trade():
    first = date(2025, 1, 1)
    rows = [candidate(first+timedelta(days=i*10)) for i in range(25)]
    selected, folds, counts = model.c1_calibrated_signals(rows)
    assert not selected and all(f['thresholds'] is None for f in folds)
    rows = [candidate(first+timedelta(days=i)) for i in range(190)]
    rows[-1]['features']['directional_result_atr'] = None
    selected, _, counts = model.c1_calibrated_signals(rows)
    assert counts['unavailable_features'] == 1
    assert rows[-1]['sample_id'] not in {s['sample_id'] for s in selected}


def signal(ident='a', eligible=1000, force=5000):
    return dict(sample_id=ident, session_day='2026-08-01', direction='long', stop=95.,
        entry_reference=100., feature_as_of_ms=eligible, feature_available_at_ms=eligible,
        decision_timestamp_ms=eligible, entry_eligible_timestamp_ms=eligible,
        entry_deadline_timestamp_ms=eligible+1000, force_exit_timestamp_ms=force)


def timing(signal):
    assert signal['feature_as_of_ms'] <= signal['entry_eligible_timestamp_ms']


def test_compact_shared_replay_partial_fees_compounding_and_no_overwrite(tmp_path):
    raw = tmp_path/'raw.parquet'
    pq.write_table(pa.table(dict(timestamp=[0,1000,2000,2001,5000,6000,7000,7001,10000],
        agg_trade_id=list(range(9)), price=[100.,100.,105.,105.,110.,100.,105.,105.,110.])), raw)
    cfg = engine.Config(tp1_r=1., tp1_fraction=.1)
    output = tmp_path/'run'
    result = engine.run_phase2(raw, [signal(),signal('b',6000,10000)], output, cfg,
        date(2026,8,1),date(2026,8,1),dict(run_id='test'),timing_validator=timing)
    trades = pq.read_table(output/'trades.parquet').to_pylist()
    assert len(trades) == 2 and all(t['tp1_filled'] for t in trades)
    assert trades[1]['quantity'] > trades[0]['quantity']
    for t in trades:
        assert t['net_pnl'] == pytest.approx(t['gross_pnl']-t['fees'])
        assert t['net_R'] == pytest.approx(t['net_pnl']/t['initial_risk'])
    assert result['metrics']['final_equity'] == pytest.approx(1000+sum(t['net_pnl'] for t in trades))
    assert {f.name for f in output.iterdir()} == {'run.json','trades.parquet','equity_curve.png'}
    assert (output/'equity_curve.png').stat().st_size > 1000
    with pytest.raises(FileExistsError):
        engine.run_phase2(raw, [], output, cfg, date(2026,8,1),date(2026,8,1),{},timing_validator=timing)


def test_end_of_data_open_partial_not_counted_and_position_block_typed(tmp_path):
    raw = tmp_path/'raw.parquet'
    pq.write_table(pa.table(dict(timestamp=[0,1000,2000,2001,3000],
        agg_trade_id=list(range(5)),price=[100.,100.,105.,105.,104.])),raw)
    result = engine.run_phase2(raw,[signal(force=9000),signal('blocked',3000,10000)],tmp_path/'run',
        engine.Config(tp1_r=1.,tp1_fraction=.1),date(2026,8,1),date(2026,8,1),{},timing_validator=timing)
    records = pq.read_table(tmp_path/'run/trades.parquet').to_pylist()
    assert result['closed_trades'] == 0 and result['open_positions_omitted'] == 1
    assert result['metrics']['final_equity'] == 1000 and result['metrics']['win_rate'] is None
    assert len(records) == 1 and records[0]['record_type'] == 'position_block'
    assert records[0]['blocking_setup_id'] == 'a' and records[0]['net_pnl'] is None


def test_invalid_timing_failed_not_successful_empty_run(tmp_path):
    raw = tmp_path/'raw.parquet'
    pq.write_table(pa.table(dict(timestamp=[0,1000,5000],agg_trade_id=[0,1,2],price=[100.,100.,101.])),raw)
    bad = signal()
    bad['entry_deadline_timestamp_ms'] = 0
    with pytest.raises(ValueError, match='timing'):
        engine.run_phase2(raw,[bad],tmp_path/'run',engine.Config(),date(2026,8,1),date(2026,8,1),{},timing_validator=timing)
    saved = json.loads((tmp_path/'run/run.json').read_text())
    assert saved['status'] == 'failed' and 'metrics' not in saved


def test_full_model_cli_synthetic_not_market_validation(tmp_path, monkeypatch):
    import numpy as np
    import pandas as pd
    # 205 complete daily patterns, measured minute prints and contiguous IDs.
    starts = pd.date_range('2025-01-01', periods=205*1440, freq='min', tz='America/New_York')
    hour = starts.hour.to_numpy()
    minute = starts.minute.to_numpy()
    price = np.where(hour < 9, 99.+(minute%2), 101.+(hour-9)*.01+minute*.001)
    raw = tmp_path/'HYPEUSDT-aggTrades-synthetic.parquet'
    pq.write_table(pa.table(dict(timestamp=starts.tz_convert('UTC').as_unit('ms').asi8,
        agg_trade_id=np.arange(len(starts)), price=price, qty=np.ones(len(starts)),
        is_buyer_maker=np.zeros(len(starts),dtype=bool))),raw)
    import os
    if 'GG_TEST_SHARED_CORE' not in os.environ:
        pytest.skip('Set GG_TEST_SHARED_CORE to the declared executor dependency for CLI integration')
    core = Path(os.environ['GG_TEST_SHARED_CORE'])
    monkeypatch.setattr('sys.argv',['strategy','--backtest','--input',str(raw),'--output-dir',str(tmp_path/'run'),
        '--shared-core',str(core),'--cache-dir',str(tmp_path/'cache')])
    model.phase2_main()
    meta = json.loads((tmp_path/'run/run.json').read_text())
    assert meta['status'] == 'complete' and meta['closed_trades'] > 0
    trades = pq.read_table(tmp_path/'run/trades.parquet').to_pylist()
    assert all(t['decision_timestamp_ms'] == t['feature_available_at_ms'] for t in trades)
    assert min(t['session_day'] for t in trades) >= '2025-07-01'
    assert meta['config']['tp1_fraction'] == .1 and meta['config']['risk_fraction'] == .005
