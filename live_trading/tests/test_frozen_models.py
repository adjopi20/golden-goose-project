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
    'pre_ny_eth': 'live_trading/live_model/pre_ny_eth',
    'pre_ny_bnb': 'paper_trading/models/pre_ny_bnb',
    'pre_ny_hype': 'paper_trading/models/pre_ny_hype',
    'volume_profile_reversion': 'paper_trading/models/volume_profile_reversion',
    'btc_mvrv': 'live_trading/live_model/btc_mvrv',
}


def load(name):
    return importlib.import_module(PATHS[name].replace('/', '.') + '.strategy')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exclusive_stages_and_live_registry():
    from live_trading.live_model import load_model, verify_release
    assert set(verify_release()['models']) == {'pre_ny_eth', 'btc_mvrv_fixed', 'btc_mvrv_p5_p95'}
    assert load_model('btc_mvrv_fixed') is load_model('btc_mvrv_p5_p95')
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


def test_original_frozen_release_sources_retain_exact_bytes():
    manifest = json.loads((HISTORY / 'release.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = ROOT / name
        if name.startswith('live_trading/live_model/'):
            path = HISTORY / ('loader.py' if Path(name).name == '__init__.py' else Path(name).name)
        assert digest(path) == expected, name


@pytest.mark.parametrize('name', ['pre_ny_eth', 'pre_ny_bnb', 'pre_ny_hype', 'volume_profile_reversion'])
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
            verify_model(module.__file__)
    source = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    for node in ast.walk(source):
        names = [node.module or ''] if isinstance(node, ast.ImportFrom) else [x.name for x in node.names] if isinstance(node, ast.Import) else []
        assert not any(n == p or n.startswith(p + '.') for n in names for p in ('indicator', 'model_development', 'trading_core'))


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


def test_eth_factory_binds_supported_lifecycle_only():
    m = load('pre_ny_eth')
    with patch('utils.engine.execution.session.create_execution') as create:
        cfg = dict(contract='TF-PRENY-ETH-001')
        m.create_execution(mode='live', config=cfg, database='external.sqlite', adapter_options={})
        create.assert_called_once_with(mode='live', config=cfg, database='external.sqlite', adapter_options={})
        with pytest.raises(ValueError, match='requires TF-PRENY-ETH'):
            m.create_execution(mode='live', config={}, database='unused', adapter_options={})


def test_btc_model_local_config_tamper_rejected(tmp_path):
    from utils.model_registry import verify_model
    src = ROOT / PATHS['btc_mvrv']
    dst = tmp_path / 'btc'
    shutil.copytree(src, dst)
    (dst / 'model_config.json').write_text('{}', encoding='utf-8')
    with pytest.raises(RuntimeError, match='configuration changed'):
        verify_model(dst / 'strategy.py')
