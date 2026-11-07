from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_mvrv.binance_spot import Spot, RateLimited
from live_mvrv.binance_execution import execute, reconcile
from live_mvrv.binance_main import tick
from live_mvrv.storage import connect, get_meta
from test_binance import INFO, NOW, ledger, signal


@pytest.mark.parametrize('path', ['/sapi/v1/capital/withdraw/apply', '/sapi/v1/accountSnapshot', '/api/v3/uiKlines'])
def test_unsupported_endpoints_never_reach_network(path):
    class Session:
        def request(self, *args, **kwargs): pytest.fail('Forbidden endpoint reached network')
    with pytest.raises(ValueError, match='explicitly supported'):
        Spot(session=Session()).request('GET', path)


@pytest.mark.parametrize('status', [418, 429])
def test_retry_after_prevents_requests_until_deadline(status, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('live_mvrv.binance_spot.time.time', lambda: clock[0])
    class Response:
        status_code = status
        headers = {'Retry-After': '125', 'X-MBX-USED-WEIGHT-1M': '6000'}
        def json(self): pytest.fail('Must back off before attempting JSON parse')
    class Session:
        calls = 0
        def request(self, *args, **kwargs): self.calls += 1; return Response()
    session = Session()
    spot = Spot(session=session)
    with pytest.raises(RateLimited) as first: spot.request('GET', '/api/v3/time')
    assert first.value.seconds == 125 and spot.cooldown_until == 225
    assert spot.limit_usage['X-MBX-USED-WEIGHT-1M'] == '6000'
    clock[0] = 224
    with pytest.raises(RateLimited): spot.request('GET', '/api/v3/time')
    assert session.calls == 1
    clock[0] = 225
    with pytest.raises(RateLimited): spot.request('GET', '/api/v3/time')
    assert session.calls == 2


@pytest.mark.parametrize('status,fallback', [(418, 86400), (429, 60)])
def test_invalid_retry_after_has_conservative_fallback(status, fallback, monkeypatch):
    monkeypatch.setattr('live_mvrv.binance_spot.time.time', lambda: 10)
    class Response:
        status_code = status
        headers = {'Retry-After': 'nan'}
    class Session:
        def request(self, *args, **kwargs): return Response()
    spot = Spot(session=Session())
    with pytest.raises(RateLimited): spot.request('GET', '/api/v3/time')
    assert spot.cooldown_until == 10 + fallback


def test_worker_cooldown_persists_and_restores(tmp_path, monkeypatch):
    c = connect(tmp_path / 'cooldown.sqlite')
    spot = Spot()
    def limited(c, spot, *args, **kwargs):
        spot.cooldown_until = 9999999999
        raise RateLimited(429, 125)
    monkeypatch.setattr('live_mvrv.binance_main._tick', limited)
    with pytest.raises(RateLimited): tick(c, spot, '1000')
    assert get_meta(c, 'venue_cooldown_until') == '9999999999'
    c.close()
    c = connect(tmp_path / 'cooldown.sqlite')
    def check(c, spot, *args, **kwargs):
        assert spot.cooldown_until == 9999999999
        return 'restored'
    monkeypatch.setattr('live_mvrv.binance_main._tick', check)
    assert tick(c, Spot(), '1000') == 'restored'
    c.close()


@pytest.mark.parametrize('change', ['missing', 'reused_id'])
def test_next_day_history_audit_detects_reset_even_with_equal_balances(tmp_path, change):
    c, spot = ledger(tmp_path)
    signal(c)
    execute(c, spot, INFO, NOW, enabled=True)
    before = [tuple(r) for r in c.execute('SELECT * FROM accounts ORDER BY id')]
    posts = len(spot.posts)
    if change == 'missing': spot.orders.clear()
    else:
        for order in spot.orders.values(): order['orderId'] += 1000
    with pytest.raises(ValueError, match='history'):
        execute(c, spot, INFO, '2020-01-04T00:01:00+00:00', enabled=True)
    assert len(spot.posts) == posts
    assert [tuple(r) for r in c.execute('SELECT * FROM accounts ORDER BY id')] == before
    assert c.execute('SELECT COUNT(*) FROM venue_trades').fetchone()[0] == 1


def test_daily_history_audit_is_idempotent(tmp_path):
    c, spot = ledger(tmp_path)
    signal(c)
    execute(c, spot, INFO, NOW, enabled=True)
    calls = []
    original = spot.order
    def counted(symbol, cid): calls.append(cid); return original(symbol, cid)
    spot.order = counted
    reconcile(c, spot, '2020-01-04T00:01:00+00:00')
    reconcile(c, spot, '2020-01-04T00:02:00+00:00')
    assert len(calls) == 1
    assert c.execute('SELECT COUNT(*) FROM venue_trades').fetchone()[0] == 1


def test_rules_refresh_returns_new_filters_and_rate_limits(monkeypatch):
    spot = Spot()
    calls = []
    def request(*args):
        calls.append(args)
        return {'symbols': [dict(INFO, quoteAssetPrecision=len(calls))],
                'rateLimits': [{'rateLimitType': 'REQUEST_WEIGHT', 'limit': 6000}]}
    monkeypatch.setattr(spot, 'request', request)
    assert spot.rules('BTCUSDT')['quoteAssetPrecision'] == 1
    assert spot.rules('BTCUSDT')['quoteAssetPrecision'] == 2
    assert spot.rate_limits[0]['limit'] == 6000
    assert all(call[1] == '/api/v3/exchangeInfo' for call in calls)
