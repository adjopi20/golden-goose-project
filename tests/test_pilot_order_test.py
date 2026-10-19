import base64
from urllib.parse import urlencode

import pytest
import requests

from live_engine.adapters.binance_futures_readonly import BinanceFuturesReadOnly, PreflightError
from models.trend_following_preny_profile_15m import pilot_order_test as subject
from test_futures_preflight import FakeClient, FakeHTTP, credentials, CONFIG, NOW


def test_probe_post_is_signed_exactly_test_only_and_no_retry(tmp_path):
    key,api,pem=credentials(tmp_path)
    http=FakeHTTP(payload={'executedQty':''});seen=[]
    def request(method,url,**kw):
        seen.append((method,url,kw))
        return http.get(url,**kw)
    http.request=request
    client=BinanceFuturesReadOnly(api_key_file=api,private_key_file=pem,session=http);client.offset=0
    result=subject.submit_test_market_request(client,'.010','BUY')
    assert result['accepted'] and result['real_order'] is False
    assert result['reported_executed_quantity'] is None
    assert seen[0][0:2]==('POST','https://fapi.binance.com/fapi/v1/order/test')
    params=seen[0][2]['data'].copy();signature=base64.b64decode(params.pop('signature'))
    key.public_key().verify(signature,urlencode(params).encode('ascii'))
    assert seen[0][2]['allow_redirects'] is False
    assert result['request_duration_ms']>=0
    http.status=503
    with pytest.raises(PreflightError,match='HTTP 503'):subject.submit_test_market_request(client,'.01','SELL')
    assert len(seen)==2


@pytest.mark.parametrize('executed',['1','NaN',False])
def test_probe_does_not_accept_nonzero_or_malformed_test_execution(tmp_path,executed):
    key,api,pem=credentials(tmp_path);http=FakeHTTP(payload={'executedQty':executed})
    http.request=lambda method,url,**kw:http.get(url,**kw)
    client=BinanceFuturesReadOnly(api_key_file=api,private_key_file=pem,session=http);client.offset=0
    with pytest.raises(PreflightError):subject.submit_test_market_request(client,'.01','BUY')


@pytest.mark.parametrize('qty,side',[('NaN','BUY'),('0','BUY'),('-1','SELL'),('.01','OTHER')])
def test_invalid_probe_cannot_make_any_request(tmp_path,qty,side):
    http=FakeHTTP()
    client=BinanceFuturesReadOnly(session=http)
    with pytest.raises(PreflightError):subject.submit_test_market_request(client,qty,side)
    assert not http.calls


def test_probe_checks_flat_wallet_before_and_after_without_real_endpoint(monkeypatch):
    monkeypatch.setattr('live_engine.futures_preflight.time.time',lambda:NOW)
    client=FakeClient();posts=[]
    monkeypatch.setattr(subject,'submit_test_market_request',lambda c,q,s:posts.append((q,s)) or {'side':s,'accepted':True})
    result=subject.probe(client,subject.load_config(CONFIG))
    assert posts==[('0.010','BUY'),('0.010','SELL')]
    assert result['wallet_before']==result['wallet_after']=='200'
    assert result['live_ready'] is False and result['real_orders'] is False
    assert result['native_stop_validation'].startswith('not_performed')
    assert all(path!='/fapi/v1/order' and path!='/fapi/v1/algoOrder' for path,_ in client.calls)


def test_blocked_account_never_reaches_test_post(monkeypatch):
    monkeypatch.setattr('live_engine.futures_preflight.time.time',lambda:NOW)
    client=FakeClient();client.data['/fapi/v1/openOrders']=[{'symbol':'ETHUSDC'}]
    monkeypatch.setattr(subject,'submit_test_market_request',lambda *a:pytest.fail('must not POST'))
    with pytest.raises(PreflightError,match='preflight blocked'):subject.probe(client,subject.load_config(CONFIG))


def test_probe_transport_errors_are_redacted_and_not_retried(tmp_path):
    key,api,pem=credentials(tmp_path);http=FakeHTTP();calls=[]
    def request(*a,**kw):
        calls.append(1)
        raise requests.Timeout('signed-secret-URL-must-not-be-shown')
    http.request=request
    client=BinanceFuturesReadOnly(api_key_file=api,private_key_file=pem,session=http);client.offset=0
    with pytest.raises(PreflightError) as e:subject.submit_test_market_request(client,'.01','BUY')
    assert 'signed-secret' not in str(e.value) and len(calls)==1


def test_test_only_probe_retains_safe_reason_but_not_api_key(tmp_path):
    key,api,pem=credentials(tmp_path)
    http=FakeHTTP(status=400,payload={'code':-4120,'msg':'Use the Algo Order API instead.'})
    http.request=lambda method,url,**kw:http.get(url,**kw)
    client=BinanceFuturesReadOnly(api_key_file=api,private_key_file=pem,session=http);client.offset=0
    with pytest.raises(PreflightError,match='Use the Algo Order API instead'):
        subject.submit_test_market_request(client,'.01','BUY')
    http.payload['msg']='Rejected '+client._key
    with pytest.raises(PreflightError) as exc:subject.submit_test_market_request(client,'.01','BUY')
    assert client._key not in str(exc.value) and '[redacted]' in str(exc.value)
