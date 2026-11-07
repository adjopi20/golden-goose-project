import json
from datetime import date, timedelta
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from model_development.models.trend_following_preny_profile_15m import strategy as model
from model_development.phase_2 import backtest as engine

def candidate(day, imbalance=0.5, result=1.0):
    return dict(sample_id=str(day), session_day=str(day), features=dict(directional_delta_imbalance=imbalance, directional_result_atr=result), signal=dict(sample_id=str(day), session_day=str(day)))

def test_c1_no_warmup_trade_frozen_train_and_future_invariance():
    first = date(2025, 1, 1)
    rows = [candidate(first + timedelta(days=i)) for i in range(250)]
    accepted, folds, counts = model.c1_calibrated_signals(rows)
    assert accepted[0]['session_day'] == str(first + timedelta(days=181))
    assert len(accepted) == 69 and counts['warmup_or_uncalibrated'] == 181
    assert folds[0]['train_candidates'] == 180
    changed = [dict(r) for r in rows]
    changed[200] = candidate(first + timedelta(days=200), imbalance=100.0, result=-100.0)
    later, refits, _ = model.c1_calibrated_signals(changed)
    assert refits[0]['thresholds'] == folds[0]['thresholds']
    assert [s for s in accepted if s['session_day'] < changed[200]['session_day']] == [s for s in later if s['session_day'] < changed[200]['session_day']]
    assert changed[200]['sample_id'] not in {s['sample_id'] for s in later}

def test_c1_minimum_and_unavailable_features_are_no_trade():
    first = date(2025, 1, 1)
    rows = [candidate(first + timedelta(days=i * 10)) for i in range(25)]
    selected, folds, counts = model.c1_calibrated_signals(rows)
    assert not selected and all((f['thresholds'] is None for f in folds))
    rows = [candidate(first + timedelta(days=i)) for i in range(190)]
    rows[-1]['features']['directional_result_atr'] = None
    selected, _, counts = model.c1_calibrated_signals(rows)
    assert counts['unavailable_features'] == 1
    assert rows[-1]['sample_id'] not in {s['sample_id'] for s in selected}

def signal(ident='a', eligible=1000, force=5000):
    return dict(sample_id=ident, session_day='2026-08-01', direction='long', stop=95.0, entry_reference=100.0, feature_as_of_ms=eligible, feature_available_at_ms=eligible, decision_timestamp_ms=eligible, entry_eligible_timestamp_ms=eligible, entry_deadline_timestamp_ms=eligible + 1000, force_exit_timestamp_ms=force)

def timing(signal):
    assert signal['feature_as_of_ms'] <= signal['entry_eligible_timestamp_ms']

def test_compact_shared_replay_partial_fees_compounding_and_no_overwrite(tmp_path):
    raw = tmp_path / 'raw.parquet'
    pq.write_table(pa.table(dict(timestamp=[0, 1000, 2000, 2001, 5000, 6000, 7000, 7001, 10000], agg_trade_id=list(range(9)), price=[100.0, 100.0, 105.0, 105.0, 110.0, 100.0, 105.0, 105.0, 110.0])), raw)
    cfg = engine.Config(tp1_r=1.0, tp1_fraction=0.1)
    output = tmp_path / 'run'
    result = engine.run_phase2(raw, [signal(), signal('b', 6000, 10000)], output, cfg, date(2026, 8, 1), date(2026, 8, 1), dict(run_id='test'), timing_validator=timing)
    trades = pq.read_table(output / 'trades.parquet').to_pylist()
    assert len(trades) == 2 and all((t['tp1_filled'] for t in trades))
    assert trades[1]['quantity'] > trades[0]['quantity']
    for t in trades:
        assert t['net_pnl'] == pytest.approx(t['gross_pnl'] - t['fees'])
        assert t['net_R'] == pytest.approx(t['net_pnl'] / t['initial_risk'])
    assert result['metrics']['final_equity'] == pytest.approx(1000 + sum((t['net_pnl'] for t in trades)))
    assert {f.name for f in output.iterdir()} == {'run.json', 'trades.parquet', 'equity_curve.png'}
    assert (output / 'equity_curve.png').stat().st_size > 1000
    with pytest.raises(FileExistsError):
        engine.run_phase2(raw, [], output, cfg, date(2026, 8, 1), date(2026, 8, 1), {}, timing_validator=timing)

def test_end_of_data_open_partial_not_counted_and_position_block_typed(tmp_path):
    raw = tmp_path / 'raw.parquet'
    pq.write_table(pa.table(dict(timestamp=[0, 1000, 2000, 2001, 3000], agg_trade_id=list(range(5)), price=[100.0, 100.0, 105.0, 105.0, 104.0])), raw)
    result = engine.run_phase2(raw, [signal(force=9000), signal('blocked', 3000, 10000)], tmp_path / 'run', engine.Config(tp1_r=1.0, tp1_fraction=0.1), date(2026, 8, 1), date(2026, 8, 1), {}, timing_validator=timing)
    records = pq.read_table(tmp_path / 'run/trades.parquet').to_pylist()
    assert result['closed_trades'] == 0 and result['open_positions_omitted'] == 1
    assert result['metrics']['final_equity'] == 1000 and result['metrics']['win_rate'] is None
    assert len(records) == 1 and records[0]['record_type'] == 'position_block'
    assert records[0]['blocking_setup_id'] == 'a' and records[0]['net_pnl'] is None

def test_invalid_timing_failed_not_successful_empty_run(tmp_path):
    raw = tmp_path / 'raw.parquet'
    pq.write_table(pa.table(dict(timestamp=[0, 1000, 5000], agg_trade_id=[0, 1, 2], price=[100.0, 100.0, 101.0])), raw)
    bad = signal()
    bad['entry_deadline_timestamp_ms'] = 0
    with pytest.raises(ValueError, match='timing'):
        engine.run_phase2(raw, [bad], tmp_path / 'run', engine.Config(), date(2026, 8, 1), date(2026, 8, 1), {}, timing_validator=timing)
    saved = json.loads((tmp_path / 'run/run.json').read_text())
    assert saved['status'] == 'failed' and 'metrics' not in saved


def test_hype_research_global_dispatch_without_data_read(tmp_path, monkeypatch):
    from model_development.phase_2 import model as runner
    from model_development import observation_cache
    root = Path(__file__).resolve().parents[4]
    selected = root / 'model_development/models/trend_following_preny_profile_15m/strategy.py'
    raw = tmp_path / 'HYPEUSDT-aggTrades-readiness.parquet'
    output = tmp_path / 'not_executed'
    def no_read(*args, **kwargs):
        raise AssertionError('Readiness must not read market data or build caches')
    monkeypatch.setattr(observation_cache, 'candle_cache', no_read)
    def dispatch(args):
        loaded = runner.load_model(args.model)
        assert loaded is model
        assert loaded.prepare_backtest.__module__ == loaded.__name__
        assert args.shared_core.resolve() == root
        assert args.input == raw
        assert args.risk_fraction == .005 and args.fee_bps == 4
        assert not output.exists() and not raw.exists()
        return dict(run_id='readiness_only', closed_trades=0)
    monkeypatch.setattr(runner, 'run_model', dispatch)
    result = engine.main(['--model', str(selected), '--input', str(raw), '--output', str(output),
        '--venue', 'binance', '--product', 'linear_quote_margined', '--quantity-unit', 'HYPE',
        '--pnl-unit', 'USDT', '--iteration', 'ITER-001', '--inspection-status', 'development'])
    assert result == 0 and not output.exists()


def test_hype_research_rejects_other_asset_and_overrides_before_data_io(tmp_path):
    from dataclasses import replace
    from model_development.phase_2.model import Request
    root = Path(__file__).resolve().parents[4]
    request = Request(input=tmp_path/'BNBUSDC-aggTrades-unused.parquet', output_dir=tmp_path/'unused',
        shared_core=root, cache_dir=None, initial_equity=1000, risk_fraction=.005,
        fee_bps=4, max_leverage=5, model_config={})
    with pytest.raises(ValueError, match='HYPEUSDT only'):
        model.prepare_backtest(request)
    with pytest.raises(ValueError, match='no model-config overrides'):
        model.prepare_backtest(replace(request, model_config={'new_filter': True}))
    assert not request.output_dir.exists()
