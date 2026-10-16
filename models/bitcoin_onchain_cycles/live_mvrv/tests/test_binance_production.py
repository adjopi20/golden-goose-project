import json
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_mvrv import binance_main as worker
from live_mvrv.binance_spot import Spot, SpotError
from live_mvrv.binance_execution import execute, reserve, require_bound, venue_report
from live_mvrv.storage import connect, get_meta, set_meta
from live_mvrv.main import health
from test_binance import Fake, INFO, NOW, signal


class Production(Fake):
    environment = 'production'
    check_production_order = Spot.check_production_order
    @property
    def execution_enabled(self):
        return Spot.execution_enabled.fget(self)
    def request(self, method, path, parameters=None, **kw):
        assert (method, path) == ('GET', '/api/v3/account/commission')
        return {'discount': {'enabledForAccount': False, 'enabledForSymbol': True}}


def prepared(tmp_path, spot=None):
    c = connect(tmp_path / 'production.sqlite')
    spot = spot or Production()
    spot.sync_time = lambda: None
    spot.rules = lambda symbol: INFO
    spot.cooldown_until = 0
    reserve(c, spot, '1000', NOW)
    return c, spot


def arm(monkeypatch, cap='500'):
    monkeypatch.setenv('BINANCE_PRODUCTION_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('BINANCE_PRODUCTION_ACK', 'I_UNDERSTAND_REAL_MONEY')
    monkeypatch.setenv('BINANCE_PRODUCTION_MAX_ORDER_USDT', cap)


@pytest.mark.parametrize('flag,ack', [('false', ''), ('true', ''), ('false', 'I_UNDERSTAND_REAL_MONEY')])
def test_production_post_blocked_before_network(flag, ack, monkeypatch):
    monkeypatch.setenv('BINANCE_PRODUCTION_EXECUTION_ENABLED', flag)
    monkeypatch.setenv('BINANCE_PRODUCTION_ACK', ack)
    class NoNetwork:
        def request(self, *a, **kw): pytest.fail('Production mutation leaked to network')
    spot = Spot(environment='production', session=NoNetwork())
    with pytest.raises(ValueError, match='blocked'):
        spot.request('POST', '/api/v3/order', {'symbol': 'BTCUSDT', 'type': 'MARKET', 'side': 'BUY', 'quoteOrderQty': '1'})


def test_testnet_flag_cannot_arm_production(monkeypatch):
    monkeypatch.delenv('BINANCE_PRODUCTION_EXECUTION_ENABLED', raising=False)
    monkeypatch.setenv('BINANCE_TESTNET_EXECUTION_ENABLED', 'true')
    assert not Spot(environment='production').execution_enabled


@pytest.mark.parametrize('cap', ['0', '10'])
def test_cap_rejects_before_intent(tmp_path, monkeypatch, cap):
    arm(monkeypatch, cap)
    c, spot = prepared(tmp_path)
    signal(c)
    with pytest.raises(ValueError): execute(c, spot, INFO, NOW, enabled=True)
    assert not spot.posts
    assert c.execute('SELECT COUNT(*) FROM venue_orders').fetchone()[0] == 0


def test_production_account_attribution_and_idempotency(tmp_path, monkeypatch):
    arm(monkeypatch)
    c, spot = prepared(tmp_path)
    signal(c)
    execute(c, spot, INFO, NOW, enabled=True)
    execute(c, spot, INFO, NOW, enabled=True)
    assert len(spot.posts) == 1
    assert c.execute('SELECT id FROM fills').fetchone()[0].startswith('binance-production:')
    report = venue_report(c, spot)
    assert report['real_money'] is True and report['execution_enabled'] is True
    assert report['execution'] == 'binance_spot_production'
    assert c.execute("SELECT cash FROM accounts WHERE id='percentile_4y_p5_p95'").fetchone()[0] == '500.0'


def test_cross_environment_database_refused(tmp_path):
    c, spot = prepared(tmp_path)
    spot.environment = 'testnet'
    with pytest.raises(ValueError, match='Wrong venue'): require_bound(c, spot)


def test_off_expiry_never_backfills(tmp_path, monkeypatch):
    monkeypatch.setenv('BINANCE_PRODUCTION_EXECUTION_ENABLED', 'false')
    c, spot = prepared(tmp_path)
    signal(c)
    execute(c, spot, INFO, NOW, enabled=False)
    assert c.execute('SELECT status FROM actions').fetchone()[0] == 'expired_disabled'
    arm(monkeypatch)
    execute(c, spot, INFO, NOW, enabled=True)
    assert not spot.posts


def test_network_failure_recovery_not_sticky_data_flag(tmp_path, monkeypatch):
    c, spot = prepared(tmp_path)
    original_utc = worker.data.utc
    monkeypatch.setattr(worker.data, 'utc', lambda value=None: original_utc(value or NOW))
    spot.sync_time = lambda: (_ for _ in ()).throw(SpotError(400, -1021))
    with pytest.raises(SpotError): worker.tick(c, spot, '1000', evaluate=False)
    assert 'binance_time' in get_meta(c, 'venue_error')
    assert get_meta(c, 'error') is None
    spot.sync_time = lambda: None
    worker.tick(c, spot, '1000', evaluate=False)
    assert get_meta(c, 'venue_error') == ''
    assert not spot.posts


def test_legacy_generic_error_requires_full_validation(tmp_path, monkeypatch):
    c, spot = prepared(tmp_path)
    with c: set_meta(c, 'error', 'Testnet exchange/data check failed; new orders paused')
    calls = []
    spot.daily_close = lambda symbol, day: Decimal(100)
    def validate(c, *a, **kw):
        calls.append(True)
        with c: set_meta(c, 'error', '')
    monkeypatch.setattr(worker, 'checked_cycle', validate)
    worker.tick(c, spot, '1000', evaluate=False)
    assert calls == [True] and get_meta(c, 'error') == ''
    assert not spot.posts


def test_notification_failure_does_not_corrupt_exchange_state(tmp_path, monkeypatch):
    c, spot = prepared(tmp_path)
    monkeypatch.setattr(worker, 'deliver', lambda *a: (_ for _ in ()).throw(RuntimeError('https://secret-token')))
    worker.tick(c, spot, '1000', evaluate=False)
    assert get_meta(c, 'venue_error') == '' and get_meta(c, 'error') is None
    assert 'RuntimeError' in get_meta(c, 'notification_error')
    assert 'secret-token' not in get_meta(c, 'notification_error')
    monkeypatch.setattr(worker, 'deliver', lambda *a: 0)
    worker.tick(c, spot, '1000', evaluate=False)
    assert get_meta(c, 'notification_error') == ''


def test_inventory_mismatch_cannot_recover_by_clearing_flag(tmp_path):
    c, spot = prepared(tmp_path)
    spot.cash += 1
    with pytest.raises(ValueError): worker.tick(c, spot, '1000', evaluate=False)
    assert get_meta(c, 'venue_error').startswith('exchange_reconcile:')
    assert not spot.posts


def test_production_template_stays_off():
    folder = Path(__file__).resolve().parents[1]
    env = (folder / 'binance-production.env.example').read_text()
    assert 'BINANCE_PRODUCTION_EXECUTION_ENABLED=false' in env
    assert 'BINANCE_PRODUCTION_ACK=\n' in env
    compose = (folder / 'compose.binance-production.yaml').read_text()
    assert 'BINANCE_ENV: production\n' in compose and 'production_data:/data' in compose
    assert 'binance_testnet_data:' not in compose


def test_unknown_order_still_halts_after_worker_recovery_change(tmp_path):
    c, spot = prepared(tmp_path)
    action = signal(c)
    with c:
        c.execute('INSERT INTO venue_orders VALUES(?,?,?,?,?,?,?)',
                  (action['id'], 'mv1_' + action['id'], 'BTCUSDT', 'buy', '{}', 'unknown', None))
    for _ in range(2):
        with pytest.raises(ValueError, match='unknown'): worker.tick(c, spot, '1000', evaluate=False)
        assert get_meta(c, 'venue_error').startswith('exchange_reconcile:')
    assert not spot.posts


def test_data_error_cannot_be_cleared_by_exchange_success(tmp_path, monkeypatch):
    arm(monkeypatch)
    c, spot = prepared(tmp_path)
    with c: set_meta(c, 'error', 'Historical revision detected; archived fetch requires operator review')
    signal(c)
    worker.tick(c, spot, '1000', evaluate=False)
    assert get_meta(c, 'error').startswith('Historical revision')
    assert not spot.posts


@pytest.mark.parametrize('discount', [None, {'enabledForAccount': True, 'enabledForSymbol': True}])
def test_unsupported_fee_configuration_blocks_before_intent(tmp_path, monkeypatch, discount):
    arm(monkeypatch)
    c, spot = prepared(tmp_path)
    spot.request = lambda *a, **kw: {'discount': discount or {}}
    signal(c)
    with pytest.raises(ValueError): execute(c, spot, INFO, NOW, enabled=True)
    assert not spot.posts and c.execute('SELECT COUNT(*) FROM venue_orders').fetchone()[0] == 0


def test_sell_cap_uses_bid_and_blocks_before_intent(tmp_path, monkeypatch):
    arm(monkeypatch, '50')
    spot = Production()
    with pytest.raises(ValueError, match='cap'):
        spot.check_production_order(dict(symbol='BTCUSDT', type='MARKET', side='SELL', quantity='1'))


def test_readonly_mode_never_armed_by_production_flags(monkeypatch):
    arm(monkeypatch)
    spot = Spot(environment='production_readonly')
    assert not spot.execution_enabled
    with pytest.raises(ValueError, match='blocked'): spot.request('POST', '/api/v3/order')


def test_slow_key_loading_precedes_timestamp(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('live_mvrv.binance_spot.time.time', lambda: clock[0])
    class Key:
        def sign(self, payload): return b'x' * 64
    class Response:
        status_code = 200
        def json(self): return {'balances': []}
    class Session:
        def request(self, *a, **kw):
            assert kw['params']['timestamp'] == 110000
            return Response()
    spot = Spot(environment='testnet', key='synthetic', key_type='ed25519', session=Session())
    def load():
        if spot._private_key is None:
            clock[0] += 10
            spot._private_key = Key()
        return spot._private_key
    monkeypatch.setattr(spot, '_load_ed25519', load)
    spot.account()


def test_safe_failure_never_echoes_external_exception():
    assert worker.safe_failure(ValueError('https://secret-token?signature=SECRET')) == 'ValueError'
    assert 'secret' not in worker.safe_failure(OSError('private credential payload'))


def test_production_requires_explicit_capital_before_db(monkeypatch):
    monkeypatch.delenv('INITIAL_CAPITAL', raising=False)
    monkeypatch.setenv('DATABASE', '/data/production.sqlite')
    monkeypatch.setattr(worker, 'Spot', lambda: Production())
    monkeypatch.setattr(worker, 'connect', lambda *a: pytest.fail('No funding default allowed'))
    monkeypatch.setattr(sys, 'argv', ['binance_main', 'once'])
    with pytest.raises(SystemExit) as error: worker.main()
    assert error.value.code == 2
