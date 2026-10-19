"""Narrow USD-M order adapter. Disarmed by default; no account-setting endpoints."""
import base64
from decimal import Decimal, InvalidOperation
import re
import time
from urllib.parse import urlencode

import requests

from .binance_futures_readonly import BinanceFuturesReadOnly, PreflightError


ACKNOWLEDGEMENT = 'I_UNDERSTAND_REAL_MONEY'
PREFIX = 'ggp-'


def safe_venue_reason(message, sensitive_values=()):
    """Keep short venue explanations, never echoed credentials or signed URLs."""
    if not isinstance(message, str) or not message or len(message) > 1024:
        return None
    for value in sensitive_values:
        if value:
            message = message.replace(value, '[redacted]')
    if re.search(r'https?://|www\.|-----BEGIN|api[ _-]?key|secret|signature|authorization|private[ _-]?key|[A-Za-z0-9_+/=-]{48,}', message, re.I):
        return '[sensitive venue message redacted]'
    return ' '.join(re.sub(r'[^\x20-\x7e]', ' ', message).split())[:300] or None


class VenueError(RuntimeError):
    def __init__(self, status=None, code=None, *, uncertain=False, reason=None):
        self.status, self.code, self.uncertain = status, code, uncertain
        self.reason = safe_venue_reason(reason)
        super().__init__(f'Binance Futures HTTP {status}, code {code}; {self.reason or "no safe venue reason available"}')


class FuturesOrders(BinanceFuturesReadOnly):
    READS = BinanceFuturesReadOnly.PRIVATE | {'/fapi/v1/order', '/fapi/v1/algoOrder',
                                            '/fapi/v1/userTrades', '/fapi/v1/income'}

    def __init__(self, *, execution_enabled=False, acknowledgement='', **kwargs):
        super().__init__(**kwargs)
        self.armed = execution_enabled is True and acknowledgement == ACKNOWLEDGEMENT

    def read(self, path, **params):
        if path not in self.READS:
            raise PreflightError('Private read endpoint not allowed')
        return self._request('GET', path, params)

    def _request(self, method, path, params):
        if method not in ('GET', 'POST', 'DELETE'):
            raise PreflightError('Unsupported method')
        if method != 'GET' and (not self.armed or path not in ('/fapi/v1/order', '/fapi/v1/algoOrder')):
            raise PreflightError('Real order mutation is OFF or endpoint not allowed')
        if any(k in params for k in ('signature', 'timestamp', 'recvWindow')):
            raise PreflightError('Caller cannot override signing parameters')
        self.load_credentials()
        if self.offset is None:
            raise PreflightError('Synchronize server time before execution')
        fields = dict(params, timestamp=int(time.time() * 1000) + self.offset, recvWindow=5000)
        fields['signature'] = base64.b64encode(self._signer.sign(urlencode(fields).encode('ascii'))).decode('ascii')
        try:
            r = self.http.request(method, self.BASE + path,
                                  params=fields if method != 'POST' else None,
                                  data=fields if method == 'POST' else None,
                                  headers={'X-MBX-APIKEY': self._key}, timeout=(5, 15), allow_redirects=False)
        except requests.RequestException:
            raise VenueError(uncertain=method != 'GET') from None
        try:
            payload = r.json()
        except ValueError:
            raise VenueError(r.status_code, uncertain=method != 'GET') from None
        code = payload.get('code') if isinstance(payload, dict) and type(payload.get('code')) is int else None
        if r.status_code != 200 or (code is not None and code < 0):
            # Only unambiguous venue rejections are safe to classify as not accepted.
            rejected = {-1013, -1100, -1102, -1111, -2019, -2021, -2022, -4003, -4004, -4005}
            reason = safe_venue_reason(payload.get('msg') if isinstance(payload, dict) else None,
                                       (self._key, fields['signature']))
            raise VenueError(r.status_code, code, uncertain=method != 'GET' and code not in rejected, reason=reason)
        if not isinstance(payload, (dict, list)):
            raise VenueError(r.status_code, uncertain=method != 'GET')
        return payload

    @staticmethod
    def check_id(client_id):
        if not isinstance(client_id, str) or not re.fullmatch(r'ggp-[a-z0-9-]{1,32}', client_id):
            raise PreflightError('Order ID outside pilot namespace')

    def submit(self, role, params):
        expected = {
            'ENTRY': {'symbol', 'side', 'positionSide', 'type', 'quantity', 'newClientOrderId', 'newOrderRespType'},
            'TP1': {'symbol', 'side', 'positionSide', 'type', 'quantity', 'price', 'timeInForce', 'reduceOnly', 'newClientOrderId'},
            'CLOSE': {'symbol', 'side', 'positionSide', 'type', 'quantity', 'reduceOnly', 'newClientOrderId', 'newOrderRespType'},
            'STOP': {'algoType', 'symbol', 'side', 'positionSide', 'type', 'triggerPrice', 'workingType',
                     'closePosition', 'priceProtect', 'clientAlgoId'},
        }
        if role not in expected or set(params) != expected[role]:
            raise PreflightError('Unexpected order template')
        if not isinstance(params['symbol'], str) or not re.fullmatch(r'[A-Z0-9]{3,30}', params['symbol']):
            raise PreflightError('Invalid instrument name')
        for field in ('quantity', 'price', 'triggerPrice'):
            if field in params:
                try: amount = Decimal(str(params[field]))
                except InvalidOperation: raise PreflightError('Invalid numeric order field') from None
                if not amount.is_finite() or amount <= 0: raise PreflightError('Order amounts must be positive/finite')
        self.check_id(params.get('clientAlgoId') if role == 'STOP' else params.get('newClientOrderId'))
        if params['positionSide'] != 'BOTH' or params['side'] not in ('BUY', 'SELL'):
            raise PreflightError('Only one-way orders are implemented')
        if role in ('ENTRY', 'CLOSE') and (params['type'] != 'MARKET' or params['newOrderRespType'] != 'RESULT'):
            raise PreflightError('Expected market order')
        if role in ('TP1', 'CLOSE') and params['reduceOnly'] != 'true':
            raise PreflightError('Exit must be reduce-only')
        if role == 'TP1' and (params['type'], params['timeInForce']) != ('LIMIT', 'GTC'):
            raise PreflightError('Expected ordinary resting TP limit, not maker-only/fallback')
        if role == 'STOP' and tuple(params[k] for k in ('algoType', 'type', 'workingType', 'closePosition', 'priceProtect')) != (
                'CONDITIONAL', 'STOP_MARKET', 'CONTRACT_PRICE', 'true', 'false'):
            raise PreflightError('Unexpected native protective stop semantics')
        return self._request('POST', '/fapi/v1/algoOrder' if role == 'STOP' else '/fapi/v1/order', params)

    def query(self, client_id, *, stop=False, symbol):
        self.check_id(client_id)
        params = {'clientAlgoId': client_id} if stop else {'symbol': symbol, 'origClientOrderId': client_id}
        try:
            return self.read('/fapi/v1/algoOrder' if stop else '/fapi/v1/order', **params)
        except VenueError as e:
            if e.code in (-2011, -2013):
                return None  # UNKNOWN is not proof that a timed-out submission failed.
            raise

    def cancel(self, client_id, *, stop=False, symbol):
        self.check_id(client_id)
        params = {'clientAlgoId': client_id} if stop else {'symbol': symbol, 'origClientOrderId': client_id}
        return self._request('DELETE', '/fapi/v1/algoOrder' if stop else '/fapi/v1/order', params)
