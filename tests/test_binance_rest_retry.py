from unittest.mock import Mock

import pytest
import requests

from utils.engine.adapters.binance_public import BinancePublic


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr("utils.engine.adapters.binance_public.time.sleep", lambda _:None)
    return BinancePublic()


def response(status=200):
    r = requests.Response()
    r.status_code = status
    r._content = b'{"serverTime":123}'
    return r


def test_transient_connection_recovers(api):
    api.http.get = Mock(side_effect=[requests.ConnectTimeout(), response()])
    assert api.server_time() == 123
    assert api.http.get.call_count == 2


def test_timeout_retries_are_bounded(api):
    api.http.get = Mock(side_effect=requests.ConnectTimeout())
    with pytest.raises(requests.ConnectTimeout): api.server_time()
    assert api.http.get.call_count == 3


def test_transient_server_error_recovers(api):
    api.http.get = Mock(side_effect=[response(503), response()])
    assert api.server_time() == 123


@pytest.mark.parametrize("status", [400, 403, 418, 429, 451])
def test_no_retries_for_access_or_rate_limits(api, status):
    api.http.get = Mock(return_value=response(status))
    with pytest.raises(requests.HTTPError): api.server_time()
    assert api.http.get.call_count == 1
