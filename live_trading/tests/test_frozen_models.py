"""Operational package placement, isolation and source parity; no real endpoints."""
import ast
from copy import deepcopy
from datetime import date
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
HISTORY = ROOT / 'model_development/models/legacy_frozen_candidates'
PATHS = {
    'pre_ny_bnb': 'paper_trading/models/pre_ny_bnb',
    'pre_ny_hype': 'paper_trading/models/pre_ny_hype',
    'volume_profile_reversion': 'paper_trading/models/volume_profile_reversion',
    'btc_mvrv': 'live_trading/live_model/btc_mvrv',
    'burst_hype': 'live_trading/live_model/burst_breakout/hypeusdt',
    'burst_inj': 'live_trading/live_model/burst_breakout/injusdt',
    'burst_uni': 'live_trading/live_model/burst_breakout/uniusdc',
    'quiet_breakout': 'live_trading/live_model/quiet_breakout/hypeusdt',
    'quiet_breakout_ethusdc': 'live_trading/live_model/quiet_breakout/ethusdc',
    'quiet_breakout_uniusdc': 'live_trading/live_model/quiet_breakout/uniusdc',
}


def load(name):
    return importlib.import_module(PATHS[name].replace('/', '.') + '.strategy')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exclusive_stages_and_live_registry():
    from live_trading.live_model import load_model, verify_release
    models = verify_release()['models']
    assert set(models) == {
        'btc_mvrv_fixed', 'btc_mvrv_p5_p95',
        'burst_hypeusdt_16h_f_p1_initiative_add', 'burst_injusdt_24h_f_p15_initiative', 'burst_uniusdc_12h_f_p10_initiative',
        'quiet_breakout_stable', 'quiet_breakout_tail_dependent', 'quiet_breakout_ethusdc_stable',
        'quiet_breakout_uniusdc_stable',
    }
    assert load_model('btc_mvrv_fixed') is load_model('btc_mvrv_p5_p95')
    assert models['quiet_breakout_stable'] == models['quiet_breakout_tail_dependent'] == PATHS['quiet_breakout']
    assert load_model('quiet_breakout_stable') is load_model('quiet_breakout_tail_dependent')
    assert models['quiet_breakout_ethusdc_stable'] == PATHS['quiet_breakout_ethusdc']
    assert load_model('quiet_breakout_ethusdc_stable').Strategy.SYMBOL == 'ETHUSDC'
    assert models['quiet_breakout_uniusdc_stable'] == PATHS['quiet_breakout_uniusdc']
    assert load_model('quiet_breakout_uniusdc_stable').Strategy.SYMBOL == 'UNIUSDC'
    with pytest.raises(ValueError, match='Not a declared live'):
        load_model('pre_ny_hype')
    for name, path in PATHS.items():
        current = ROOT / path
        assert len(list(current.glob('*.py'))) == 1
        other = ROOT / ('paper_trading/models' if path.startswith('live_') else 'live_trading/live_model') / name
        assert not other.exists()
        assert not list(current.glob('*.sqlite'))
    assert (ROOT / 'model_development/models/trend_following_preny_profile_15m/strategy.py').is_file()
    assert (ROOT / 'model_development/models/bitcoin_onchain_cycles/live_mvrv/signal_monitor.py').is_file()


def test_burst_models_use_distinct_verified_policies():
    from decimal import Decimal
    from live_trading.live_model import load_model
    expected = {
        'burst_hypeusdt_16h_f_p1_initiative_add': ('HYPEUSDT', 16, '3m_12m', 'rm-e', 'path_p1', 'initiative_1p5_add_at_2r', 0.01),
        'burst_injusdt_24h_f_p15_initiative': ('INJUSDT', 24, '5m_10m', 'rm-e', 'path_p15', 'initiative_1p5', 0.009),
        'burst_uniusdc_12h_f_p10_initiative': ('UNIUSDC', 12, '1m_5m', 'rm-c', 'path_p10', 'initiative_1p5', 0.01),
    }
    for key, values in expected.items():
        cls = load_model(key).Strategy
        policy = cls.model_config()
        config = cls.execution_config(Decimal(99), False)
        assert (cls.SYMBOL, policy['lookback_hours'], policy['flow_timeframes'],
                policy['risk_management'], policy['entry_filter'], policy['position_sizing'],
                config['risk_fraction']) == values
        assert config['symbol'] == cls.SYMBOL and config['allocation_id'] == key
        assert cls.DATABASE == f'{key}.sqlite'
        assert policy['risk_exit_hours'] == 48
        armed = cls.execution_config(Decimal(99), True)
        assert not config['execution_enabled'] and not config['accept_new_entries']
        assert armed['execution_enabled'] and armed['accept_new_entries']
        from live_trading.live_model.burst_breakout.policy import SelectedBurstAdditionExecutor, SelectedBurstExecutor
        expected_executor = SelectedBurstAdditionExecutor if cls.SYMBOL == 'HYPEUSDT' else SelectedBurstExecutor
        assert cls.EXECUTOR is expected_executor


def test_burst_signal_freeze_matches_reference_source():
    from live_trading import burst_signals
    src = ROOT / 'model_development/models/burst_breakout/strategy.py'
    live = ast.parse(Path(burst_signals.__file__).read_text(encoding='utf-8'))
    reference = {n.name: n for n in ast.parse(src.read_text(encoding='utf-8')).body
                 if isinstance(n, ast.FunctionDef)}

    # Research also serves daily profiles. Its hourly default is the live window.
    hourly = reference['_burst_signals_from_features']
    assert [arg.arg for arg in hourly.args.kwonlyargs] == ['entry_window_ms']
    assert [default.value for default in hourly.args.kw_defaults] == [3_600_000]
    hour, = [n for n in ast.walk(hourly) if isinstance(n, ast.Assign) and
             isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'hour']
    assert hour.value.value == 3_600_000
    hourly.args.kwonlyargs.clear()
    hourly.args.kw_defaults.clear()
    deadline, = [n for n in ast.walk(hourly) if isinstance(n, ast.Name) and n.id == 'entry_window_ms']
    deadline.id = 'hour'
    for node in live.body:
        if isinstance(node, ast.FunctionDef):
            assert ast.dump(node) == ast.dump(reference[node.name])


def test_original_frozen_release_sources_retain_exact_bytes():
    manifest = json.loads((HISTORY / 'release.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = ROOT / name
        if name.startswith('live_trading/live_model/'):
            path = HISTORY / ('loader.py' if Path(name).name == '__init__.py' else Path(name).name)
        assert digest(path) == expected, name


@pytest.mark.parametrize('name', ['pre_ny_bnb', 'pre_ny_hype', 'volume_profile_reversion'])
def test_existing_strategy_functions_preserved(name):
    before = ast.parse((HISTORY / (name + '.py')).read_text(encoding='utf-8'))
    after = ast.parse((ROOT / PATHS[name] / 'strategy.py').read_text(encoding='utf-8'))
    definitions = {n.name: n for n in after.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for node in before.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            assert ast.dump(node) == ast.dump(definitions[node.name]), node.name


@pytest.mark.parametrize('name', list(PATHS))
def test_import_and_release_verification_are_offline(name):
    from utils.model_registry import verify_model
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('database')):
            module = load(name)
            contract = verify_model(module.__file__)
    source = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    for node in ast.walk(source):
        names = [node.module or ''] if isinstance(node, ast.ImportFrom) else [x.name for x in node.names] if isinstance(node, ast.Import) else []
        for imported in names:
            if name == 'quiet_breakout' and imported == 'indicator.volume_profile':
                assert 'indicator/volume_profile.py' in contract['dependencies']
                continue
            assert not any(imported == p or imported.startswith(p + '.')
                           for p in ('indicator', 'model_development', 'trading_core'))


def test_promotable_package_and_tamper_guard(tmp_path):
    from utils.model_registry import verify_model
    src = ROOT / PATHS['pre_ny_bnb']
    dst = tmp_path / 'live_trading/live_model/pre_ny_bnb'
    shutil.copytree(src, dst)
    original = verify_model(src / 'strategy.py')
    assert verify_model(dst / 'strategy.py') == original
    with (dst / 'strategy.py').open('a', encoding='utf-8') as handle:
        handle.write('\n# changed\n')
    with pytest.raises(RuntimeError, match='strategy source changed'):
        verify_model(dst / 'strategy.py')


def test_btc_config_and_signal_logic_match_research():
    module = load('btc_mvrv')
    from model_development.models.bitcoin_onchain_cycles.live_mvrv import data, signal_monitor
    from model_development.models.bitcoin_onchain_cycles.live_mvrv.engine import configuration
    assert module.configuration() == configuration()
    raw = pd.DataFrame(dict(time=['2010-07-18', '2010-07-19'], PriceUSD=[100., 101.], CapMrktCurUSD=[1000., 1100.], CapMVRVCur=[1., 1.1]))
    pd.testing.assert_frame_equal(module.prepare(raw, configuration()), data.prepare(raw, configuration()))
    row = dict(z=2., price=100., fixed_0_7_entry=0., fixed_0_7_exit=7., percentile_4y_p5_p95_entry=3., percentile_4y_p5_p95_exit=6.)
    assert module.zones(row, configuration()) == signal_monitor.zones(row, configuration())
    assert not hasattr(module, 'apply_day') and not hasattr(module, 'initialize')


def test_btc_signal_only_database_guard(tmp_path):
    module = load('btc_mvrv')
    c = module.connect(tmp_path / 'signals.sqlite')
    try:
        module.bind(c)
        status = module.report(c)
        assert status['orders_enabled'] is False and status['capital_required'] is False
        c.execute("INSERT INTO accounts VALUES('funded', '1000', '0')")
        with pytest.raises(ValueError, match='refuses funded'):
            module.bind(c)
    finally:
        c.close()


@pytest.mark.parametrize('name', ['pre_ny_bnb', 'pre_ny_hype'])
def test_paper_factory_binds_existing_binance_account(name):
    m = load(name)
    with patch('utils.engine.execution.paper_manager.PaperOrderManager') as factory:
        store = object()
        result = m.create_paper_manager(store)
        assert result is factory.return_value
        factory.assert_called_once_with(store)
        args, kwargs = factory.return_value.register.call_args
        cfg = m.account_configuration('binance')
        assert args[:2] == (cfg['id'], cfg['symbol'])
        assert kwargs == dict(allowed_risks=cfg['allowed_risks'], contract=cfg['contract'])
        with pytest.raises(ValueError, match='selects Binance'):
            m.create_paper_manager(store, venue='lighter')


def test_retired_pre_ny_eth_is_research_only():
    from live_trading.live_model import load_model
    with pytest.raises(ValueError, match='Not a declared live'):
        load_model('pre_ny_eth')
    assert not (ROOT / 'live_trading/live_model/pre_ny_eth').exists()
    assert (HISTORY / 'pre_ny_eth.py').is_file()


def test_btc_model_local_config_tamper_rejected(tmp_path):
    from utils.model_registry import verify_model
    src = ROOT / PATHS['btc_mvrv']
    dst = tmp_path / 'btc'
    shutil.copytree(src, dst)
    (dst / 'model_config.json').write_text('{}', encoding='utf-8')
    with pytest.raises(RuntimeError, match='configuration changed'):
        verify_model(dst / 'strategy.py')
