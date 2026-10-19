import base64
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from live_engine.adapters.binance_futures_readonly import BinanceFuturesReadOnly, PreflightError
from live_engine.futures_preflight import load_config, minimum_geometry, preflight, quantity_rules


CONFIG = Path(__file__).resolve().parents[1] / 'models/trend_following_preny_profile_15m/deploy/pilot.ethusdc.preflight.json'
NOW = 1_800_000_000


def specification():
    return dict(symbol='ETHUSDC', status='TRADING', contractType='PERPETUAL',
                quoteAsset='USDC', marginAsset='USDC', filters=[
                    dict(filterType='LOT_SIZE', minQty='0.001', maxQty='1000', stepSize='0.001'),
                    dict(filterType='MARKET_LOT_SIZE', minQty='0.001', maxQty='500', stepSize='0.001'),
                    dict(filterType='MIN_NOTIONAL', notional='20'),
                    dict(filterType='PRICE_FILTER', tickSize='0.01')])


class FakeClient:
    offset = 0

    def __init__(self):
        self.calls = []
        self.data = {
            '/fapi/v1/exchangeInfo': {'symbols': [specification()]},
            '/fapi/v1/accountConfig': {'canTrade': True, 'dualSidePosition': False, 'multiAssetsMargin': False},
            '/fapi/v3/account': {'assets': [dict(asset='USDC', availableBalance='200', walletBalance='200')]},
            '/fapi/v3/positionRisk': [], '/fapi/v1/openOrders': [], '/fapi/v1/openAlgoOrders': [],
            '/fapi/v1/symbolConfig': [dict(symbol='ETHUSDC', leverage=3, marginType='CROSSED')],
            '/fapi/v1/commissionRate': dict(symbol='ETHUSDC', makerCommissionRate='0.0002', takerCommissionRate='0.0004'),
            '/fapi/v1/ticker/bookTicker': dict(symbol='ETHUSDC', bidPrice='2700', askPrice='2701', time=NOW * 1000),
        }

    def sync_time(self):
        pass

    def get(self, path, **params):
        self.calls.append((path, params))
        return deepcopy(self.data[path])


def test_minimum_quantity_and_risk_bounds():
    r = minimum_geometry(specification(), '2700', '2700', '145', ['0.0025', '0.005'], '0.1', '5')
    assert Decimal(r['minimum_exact_split_quantity']) == Decimal('.01')
    assert Decimal(r['tp1_quantity']) == Decimal('.001')
    assert Decimal(r['remainder_quantity']) == Decimal('.009')
    assert [Decimal(x['price_risk_budget']) for x in r['risk_bounds']] == [Decimal('.3625'), Decimal('.725')]
    assert Decimal(r['risk_bounds'][0]['maximum_stop_distance_price']) == Decimal('36.25')


def test_intersection_of_quantity_grids_and_disabled_market_step():
    spec = specification()
    spec['filters'][0]['stepSize'] = '.02'
    spec['filters'][1]['stepSize'] = '.03'
    assert quantity_rules(spec)['step'] == Decimal('.06')
    spec['filters'][1]['stepSize'] = '0'
    assert quantity_rules(spec)['step'] == Decimal('.02')


@pytest.mark.parametrize('change', [
    lambda s: s['filters'].pop(),
    lambda s: s['filters'].append(deepcopy(s['filters'][0])),
    lambda s: s['filters'][0].update(stepSize='NaN'),
    lambda s: s['filters'][0].update(minQty='10000'),
])
def test_malformed_filters_fail_closed(change):
    spec = specification()
    change(spec)
    with pytest.raises(PreflightError):
        quantity_rules(spec)


def test_small_cap_never_increases_risk_to_fit():
    with pytest.raises(PreflightError, match='cap'):
        minimum_geometry(specification(), '2700', '2700', '1', ['.005'], '.1', '5')


def test_readonly_pass_is_never_live_ready(monkeypatch):
    monkeypatch.setattr('live_engine.futures_preflight.time.time', lambda: NOW)
    client = FakeClient()
    r = preflight(client, load_config(CONFIG))
    assert r['read_only_checks'] == 'pass'
    assert not r['real_orders'] and not r['live_ready']
    assert r['account']['current_leverage'] == 3
    assert client.calls[-1][0] == '/fapi/v1/ticker/bookTicker'


def test_pilot_allocation_is_200_not_wallet_size(monkeypatch):
    monkeypatch.setattr('live_engine.futures_preflight.time.time', lambda: NOW)
    client = FakeClient()
    client.data['/fapi/v3/account']['assets'][0].update(availableBalance='202', walletBalance='202')
    r = preflight(client, load_config(CONFIG))
    assert r['allocated_equity']=='200'
    assert [Decimal(x['price_risk_budget']) for x in r['minimum_geometry']['risk_bounds']]==[Decimal('.5'),Decimal('1')]
    client.data['/fapi/v3/account']['assets'][0]['availableBalance']='145'
    assert 'insufficient_available_USDC_for_200_allocation' in preflight(client,load_config(CONFIG))['blockers']


@pytest.mark.parametrize('endpoint,change,reason', [
    ('/fapi/v1/accountConfig', lambda r: r.update(canTrade=False), 'account_canTrade_not_true'),
    ('/fapi/v1/accountConfig', lambda r: r.update(dualSidePosition=True), 'hedge_mode_or_unknown_requires_review'),
    ('/fapi/v1/accountConfig', lambda r: r.pop('multiAssetsMargin'), 'multi_asset_margin_or_unknown_requires_review'),
    ('/fapi/v3/account', lambda r: r['assets'][0].update(availableBalance='0'), 'insufficient_available_USDC_for_200_allocation'),
    ('/fapi/v3/positionRisk', lambda r: r.append(dict(symbol='BTCUSDT', positionAmt='-.01')), 'account_not_flat_or_has_open_orders'),
    ('/fapi/v1/openOrders', lambda r: r.append(dict(symbol='ETHUSDC')), 'account_not_flat_or_has_open_orders'),
    ('/fapi/v1/openAlgoOrders', lambda r: r.append(dict(symbol='ETHUSDC')), 'account_not_flat_or_has_open_orders'),
])
def test_account_blockers_are_not_cleared(monkeypatch, endpoint, change, reason):
    monkeypatch.setattr('live_engine.futures_preflight.time.time', lambda: NOW)
    client = FakeClient()
    change(client.data[endpoint])
    r = preflight(client, load_config(CONFIG))
    assert r['read_only_checks'] == 'blocked'
    assert reason in r['blockers'] and not r['live_ready']


def test_unknown_order_schema_and_stale_quote_fail(monkeypatch):
    monkeypatch.setattr('live_engine.futures_preflight.time.time', lambda: NOW)
    client = FakeClient()
    client.data['/fapi/v1/openAlgoOrders'] = {'unexpected': []}
    with pytest.raises(PreflightError, match='cannot assert'):
        preflight(client, load_config(CONFIG))
    client = FakeClient()
    client.data['/fapi/v1/ticker/bookTicker']['time'] -= 10001
    with pytest.raises(PreflightError, match='stale'):
        preflight(client, load_config(CONFIG))


def test_public_only_never_reads_private_routes(monkeypatch):
    monkeypatch.setattr('live_engine.futures_preflight.time.time', lambda: NOW)
    client = FakeClient()
    r = preflight(client, load_config(CONFIG), public_only=True)
    assert r['read_only_checks'] == 'public_only'
    assert r['account'] is None and not r['live_ready']
    assert all(path in BinanceFuturesReadOnly.PUBLIC for path, _ in client.calls)


def test_contract_change_requires_review(tmp_path):
    changed = json.loads(CONFIG.read_text())
    changed['allocated_equity'] = '1000'
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(changed))
    with pytest.raises(PreflightError, match='differs'):
        load_config(path)


class FakeHTTP:
    trust_env = True

    def __init__(self, status=200, payload=None, error=None):
        self.calls = []
        self.status, self.payload, self.error = status, payload or {}, error

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        owner = self

        class Response:
            status_code = owner.status

            def json(self):
                return owner.payload
        return Response()

    def close(self):
        pass


def credentials(tmp_path, password=None, key=None):
    key = key or ed25519.Ed25519PrivateKey.generate()
    public = tmp_path / 'api-key.txt'
    private = tmp_path / 'key.pem'
    public.write_text('A' * 64)
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption))
    return key, public, private


def test_ed25519_signature_correct_and_only_get(tmp_path, monkeypatch):
    monkeypatch.setattr('live_engine.adapters.binance_futures_readonly.time.time', lambda: NOW)
    key, api, private = credentials(tmp_path)
    http = FakeHTTP(payload={})
    client = BinanceFuturesReadOnly(api_key_file=api, private_key_file=private, session=http)
    client.offset = 0
    client.get('/fapi/v1/commissionRate', symbol='ETHUSDC')
    url, options = http.calls[0]
    params = options['params'].copy()
    signature = base64.b64decode(params.pop('signature'))
    key.public_key().verify(signature, urlencode(params).encode('ascii'))
    assert url == 'https://fapi.binance.com/fapi/v1/commissionRate'
    assert options['headers'] == {'X-MBX-APIKEY': 'A' * 64}
    assert options['allow_redirects'] is False and http.trust_env is False
    for path in ('/fapi/v1/order', '/fapi/v1/algoOrder', '/api/v3/account', 'https://evil.test'):
        with pytest.raises(PreflightError, match='not allowed'):
            client.get(path)
    with pytest.raises(PreflightError, match='override'):
        client.get('/fapi/v3/account', timestamp=1)
    assert len(http.calls) == 1
    assert not hasattr(client, 'post') and not hasattr(client, 'request')


def test_encrypted_key_and_wrong_type_fail_safely(tmp_path):
    _, api, private = credentials(tmp_path, password=b'test-passphrase')
    client = BinanceFuturesReadOnly(api_key_file=api, private_key_file=private)
    with pytest.raises(PreflightError, match='passphrase'):
        client.load_credentials()
    password = tmp_path / 'passphrase.txt'
    password.write_bytes(b'test-passphrase\n')
    client = BinanceFuturesReadOnly(api_key_file=api, private_key_file=private, passphrase_file=password)
    client.load_credentials()
    _, api, private = credentials(tmp_path, key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with pytest.raises(PreflightError, match='Ed25519'):
        BinanceFuturesReadOnly(api_key_file=api, private_key_file=private).load_credentials()


@pytest.mark.parametrize('status,error', [(403, None), (429, None), (503, None),
                                         (200, requests.ConnectionError('https://secret?signature=DO_NOT_LEAK'))])
def test_no_secret_or_raw_response_in_errors(tmp_path, status, error):
    _, api, private = credentials(tmp_path)
    http = FakeHTTP(status, {'code': -2015, 'msg': 'DO_NOT_LEAK'}, error)
    client = BinanceFuturesReadOnly(api_key_file=api, private_key_file=private, session=http)
    client.offset = 0
    with pytest.raises(PreflightError) as exc:
        client.get('/fapi/v3/account')
    assert 'DO_NOT_LEAK' not in str(exc.value) and 'A' * 64 not in str(exc.value)
    assert len(http.calls) == 1
