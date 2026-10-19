"""Allowlisted Binance USD-M GETs only. No order, cancel, or account mutation API."""
import base64
from pathlib import Path
import re
import time
from urllib.parse import urlencode

import requests


class PreflightError(ValueError):
    """Only messages safe to show without credentials or raw venue responses."""


def secret_bytes(path, limit):
    try:
        with Path(path).open('rb') as handle:
            value = handle.read(limit + 1)
    except (OSError, TypeError):
        raise PreflightError('Cannot read a required secret mount; check path/permissions') from None
    if len(value) > limit or not value:
        raise PreflightError('Invalid secret file size')
    return value


class BinanceFuturesReadOnly:
    BASE = 'https://fapi.binance.com'
    PUBLIC = {'/fapi/v1/time', '/fapi/v1/exchangeInfo', '/fapi/v1/ticker/bookTicker'}
    PRIVATE = {'/fapi/v3/account', '/fapi/v1/accountConfig', '/fapi/v3/positionRisk',
               '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders',
               '/fapi/v1/symbolConfig', '/fapi/v1/commissionRate'}

    def __init__(self, *, api_key_file=None, private_key_file=None, passphrase_file=None, session=None, api_key=None):
        self.api_key_file = api_key_file
        self._provided_key = api_key
        self.private_key_file = private_key_file
        self.passphrase_file = passphrase_file
        self.http = session if session is not None else requests.Session()
        self.http.trust_env = False
        self._key = self._signer = None
        self.offset = None

    def load_credentials(self):
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if self._signer is not None:
            return
        try:
            key = (self._provided_key.strip() if self._provided_key is not None else
                   secret_bytes(self.api_key_file, 1024).decode('utf-8-sig').strip())
        except UnicodeError:
            raise PreflightError('API key file must contain the API key as plain text') from None
        if not re.fullmatch(r'[A-Za-z0-9]{16,512}', key):
            raise PreflightError('Invalid API key file; do not paste a private key into it')
        pem = secret_bytes(self.private_key_file, 65536)
        password = None
        if self.passphrase_file:
            try:
                with Path(self.passphrase_file).open('rb') as handle:
                    password = handle.read(4097).rstrip(b'\r\n') or None
            except OSError:
                raise PreflightError('Cannot read passphrase mount') from None
            if password and len(password) > 4096:
                raise PreflightError('Passphrase file too large')
        try:
            signer = load_pem_private_key(pem, password=password)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise PreflightError('Cannot load private PEM; check key type and passphrase mount') from None
        if not isinstance(signer, Ed25519PrivateKey):
            raise PreflightError('This pilot requires an Ed25519 private key; RSA/HMAC are not assumed')
        self._key, self._signer = key, signer

    def get(self, path, **parameters):
        if path not in self.PUBLIC | self.PRIVATE:
            raise PreflightError('Endpoint not allowed by read-only preflight')
        if any(k in parameters for k in ('signature', 'timestamp', 'recvWindow')):
            raise PreflightError('Caller cannot override signing parameters')
        params, headers = dict(parameters), {}
        if path in self.PRIVATE:
            self.load_credentials()
            if self.offset is None:
                raise PreflightError('Synchronize server time before authenticated reads')
            params.update(timestamp=int(time.time() * 1000) + self.offset, recvWindow=5000)
            payload = urlencode(params)
            params['signature'] = base64.b64encode(self._signer.sign(payload.encode('ascii'))).decode('ascii')
            headers['X-MBX-APIKEY'] = self._key
        try:
            response = self.http.get(self.BASE + path, params=params, headers=headers,
                                     timeout=(5, 15), allow_redirects=False)
        except requests.RequestException:
            # requests exceptions may contain a signed URL. Never display them.
            raise PreflightError('Binance GET connection/timeout failure; no retry or order sent') from None
        if response.status_code != 200:
            code = None
            try:
                result = response.json()
                if isinstance(result, dict) and type(result.get('code')) is int:
                    code = result['code']
            except ValueError:
                pass
            raise PreflightError(f'Binance GET failed: HTTP {response.status_code}, code {code}; no automatic retry')
        try:
            result = response.json()
        except ValueError:
            raise PreflightError('Invalid JSON from Binance GET') from None
        if not isinstance(result, (dict, list)):
            raise PreflightError('Unexpected Binance GET response shape')
        return result

    def sync_time(self):
        before = int(time.time() * 1000)
        row = self.get('/fapi/v1/time')
        after = int(time.time() * 1000)
        stamp = row.get('serverTime') if isinstance(row, dict) else None
        if type(stamp) is not int or stamp <= 0 or not 0 <= after - before <= 5000:
            raise PreflightError('Invalid server time or slow time synchronization; retry later')
        self.offset = stamp - (before + after) // 2

    def close(self):
        self.http.close()
