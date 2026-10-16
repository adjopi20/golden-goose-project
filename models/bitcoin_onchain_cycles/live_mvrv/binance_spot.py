"""Small Spot REST adapter; production mutation requires explicit opt-in."""
from decimal import Decimal, ROUND_DOWN
import base64
import getpass
import hashlib
import hmac
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlencode

import requests


class SpotError(RuntimeError):
    def __init__(self, status=None, code=None):
        self.status, self.code = status, code
        super().__init__(f"Binance request failed (HTTP {status}, code {code}); secrets redacted")


class UnknownExecution(SpotError):
    """Request may have reached the venue. Never automatically resubmit."""


class RateLimited(SpotError):
    def __init__(self, status, seconds):
        self.status, self.code, self.seconds = status, None, seconds
        RuntimeError.__init__(self, f"Binance rate-limit cooldown: wait {seconds}s (HTTP {status}); no automatic order retry")


def dec(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError("Expected finite nonnegative exchange amount")
    return value


def plain(value):
    return format(dec(value), "f")


class Spot:
    def __init__(self, *, environment=None, key=None, secret=None, session=None, key_type=None,
                 private_key_file=None, prompt_passphrase=False):
        self.environment = environment or os.getenv("BINANCE_ENV", "testnet").strip()
        if self.environment not in {"testnet", "production_readonly", "production"}:
            raise ValueError("BINANCE_ENV must be testnet, production_readonly or production")
        self.base = "https://testnet.binance.vision" if self.environment == "testnet" else "https://api.binance.com"
        self.key = (key if key is not None else os.getenv("BINANCE_API_KEY", "")).strip()
        self.secret = (secret if secret is not None else os.getenv("BINANCE_API_SECRET", "")).strip()
        self.key_type = (key_type or os.getenv("BINANCE_KEY_TYPE", "hmac")).strip().lower()
        if self.key_type not in {"hmac", "ed25519"}:
            raise ValueError("BINANCE_KEY_TYPE must be hmac or ed25519")
        self.private_key_file = private_key_file or os.getenv("BINANCE_PRIVATE_KEY_FILE", "").strip()
        self.prompt_passphrase = prompt_passphrase
        self._private_key = None
        self.session = session or requests.Session()
        self.offset = 0
        self.cooldown_until = 0.0
        self.rate_limits = []
        self.limit_usage = {}

    def _load_ed25519(self):
        if self._private_key is not None:
            return self._private_key
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if not self.private_key_file:
            raise ValueError("Set BINANCE_PRIVATE_KEY_FILE to the read-only mounted private PEM")
        try:
            with Path(self.private_key_file).open("rb") as handle:
                pem = handle.read(65537)
            if len(pem) > 65536:
                raise ValueError("Private key file too large")
        except OSError:
            raise ValueError("Cannot read private key mount; check path and file permissions") from None
        try:
            key = load_pem_private_key(pem, password=None)
        except TypeError:
            passfile = os.getenv("BINANCE_PRIVATE_KEY_PASSPHRASE_FILE", "").strip()
            password = None
            if passfile:
                try:
                    with Path(passfile).open("rb") as handle:
                        password = handle.read(4097)
                    if len(password) > 4096:
                        raise ValueError("Passphrase file too large")
                    password = password.removeprefix(b"\xef\xbb\xbf").rstrip(b"\r\n")
                except OSError:
                    raise ValueError("Cannot read passphrase secret file") from None
            elif self.prompt_passphrase and sys.stdin.isatty():
                password = getpass.getpass("Private-key passphrase (hidden; local only): ").encode("utf-8")
            if not password:
                raise ValueError("Encrypted private key needs a passphrase: use interactive status or BINANCE_PRIVATE_KEY_PASSPHRASE_FILE") from None
            try:
                key = load_pem_private_key(pem, password=password)
            except (ValueError, TypeError, UnsupportedAlgorithm):
                raise ValueError("Private key could not be unlocked; check passphrase and PEM format (details redacted)") from None
            finally:
                password = None
        except (ValueError, UnsupportedAlgorithm):
            raise ValueError("Invalid or unsupported private PEM (details redacted)") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Expected Ed25519 private key, not RSA/EC/public key")
        self._private_key = key
        return key

    def signature(self, payload):
        if self.key_type == "ed25519":
            return base64.b64encode(self._load_ed25519().sign(payload.encode("ascii"))).decode("ascii")
        if not self.secret:
            raise ValueError("HMAC needs BINANCE_API_SECRET; for a registered Ed25519 key set BINANCE_KEY_TYPE=ed25519")
        return hmac.new(self.secret.encode(), payload.encode("ascii"), hashlib.sha256).hexdigest()

    def request(self, method, path, parameters=None, *, signed=False):
        if method not in {"GET", "POST"} or (method != "GET" and self.environment == "production_readonly"):
            raise ValueError("Production mutation is blocked")
        if method == "POST" and path != "/api/v3/order":
            raise ValueError("Only Spot new-order POST is implemented")
        if path not in {"/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/account",
                        "/api/v3/openOrders", "/api/v3/ticker/bookTicker", "/api/v3/klines",
                        "/api/v3/order", "/api/v3/myTrades", "/api/v3/account/commission"}:
            raise ValueError("Only the explicitly supported /api/v3 endpoints are allowed; /sapi is unavailable")
        if method == "POST" and self.environment == "production":
            self.check_production_order(parameters or {})
        if time.time() < self.cooldown_until:
            raise RateLimited(None, math.ceil(self.cooldown_until - time.time()))
        params = dict(parameters or {})
        headers = {}
        if signed:
            if not self.key:
                raise ValueError("Set Binance credentials locally; never send them in chat")
            # Load/decrypt the signer before starting the signed request window.
            if self.key_type == "ed25519":
                self._load_ed25519()
            params.update(timestamp=int(time.time() * 1000) + self.offset, recvWindow=5000)
            payload = urlencode(params)
            params["signature"] = self.signature(payload)
            headers["X-MBX-APIKEY"] = self.key
        try:
            response = self.session.request(method, self.base + path, params=params if method == "GET" else None,
                                            data=params if method == "POST" else None, headers=headers,
                                            timeout=(10, 20), allow_redirects=False)
            response_headers = getattr(response, "headers", {})
            self.limit_usage = {k: v for k, v in response_headers.items()
                                if k.upper().startswith(("X-MBX-USED-WEIGHT-", "X-MBX-ORDER-COUNT-"))}
            if response.status_code in {418, 429}:
                # Do this before JSON parsing: an IP-ban response need not be JSON.
                fallback = 86400 if response.status_code == 418 else 60
                try:
                    seconds = float(response_headers.get("Retry-After", fallback))
                except (ValueError, TypeError):
                    seconds = fallback
                if not math.isfinite(seconds) or seconds < 1:
                    seconds = fallback
                seconds = math.ceil(seconds)
                self.cooldown_until = max(self.cooldown_until, time.time() + seconds)
                raise RateLimited(response.status_code, seconds)
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise (UnknownExecution() if method == "POST" else SpotError()) from None
        if response.status_code != 200:
            code = payload.get("code") if isinstance(payload, dict) and isinstance(payload.get("code"), int) else None
            error = UnknownExecution if method == "POST" and (response.status_code >= 500 or code == -1007) else SpotError
            raise error(response.status_code, code) from None
        if not isinstance(payload, (dict, list)):
            raise (UnknownExecution() if method == "POST" else SpotError())
        return payload

    def sync_time(self):
        before = int(time.time() * 1000)
        result = self.request("GET", "/api/v3/time")
        after = int(time.time() * 1000)
        self.offset = int(result["serverTime"]) - (before + after) // 2

    def account(self):
        return self.request("GET", "/api/v3/account", signed=True)

    def open_orders(self, symbol):
        return self.request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)

    def rules(self, symbol):
        result = self.request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        self.rate_limits = result.get("rateLimits", [])
        rows = result.get("symbols", [])
        if len(rows) != 1 or rows[0]["symbol"] != symbol:
            raise ValueError("Symbol information unavailable")
        row = rows[0]
        if row.get("status") != "TRADING" or row.get("baseAsset") != "BTC" or row.get("quoteAsset") != "USDT":
            raise ValueError("Prototype requires trading BTCUSDT Spot")
        if not row.get("isSpotTradingAllowed", False) or "MARKET" not in row.get("orderTypes", []):
            raise ValueError("Spot market orders unavailable")
        return row

    def quote(self, symbol):
        result = self.request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        bid, ask = dec(result["bidPrice"]), dec(result["askPrice"])
        if bid <= 0 or ask < bid:
            raise ValueError("Invalid live quote")
        if (ask - bid) / bid > Decimal("0.005"):
            raise ValueError("Spread exceeds 0.5% execution guard")
        return bid, ask

    def daily_close(self, symbol, day):
        from .data import utc
        start = int(utc(day + "T00:00:00+00:00").timestamp() * 1000)
        rows = self.request("GET", "/api/v3/klines", {"symbol": symbol, "interval": "1d", "startTime": start,
                                                      "endTime": start + 86400000 - 1, "limit": 1})
        if len(rows) != 1 or rows[0][0] != start or rows[0][6] != start + 86400000 - 1:
            raise ValueError("Missing exact UTC-day Binance close")
        price = dec(rows[0][4])
        if price <= 0:
            raise ValueError("Invalid Binance daily close")
        return price

    def order(self, symbol, client_id):
        try:
            return self.request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)
        except SpotError as error:
            if error.code == -2013:
                return None
            raise

    def trades(self, symbol, order_id):
        # Fail closed on a full page; never pretend truncated fills are complete.
        result = self.request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id, "limit": 1000}, signed=True)
        if len(result) >= 1000:
            raise ValueError("Fill pagination required; automatic execution paused")
        return result

    def market(self, symbol, client_id, side, size):
        if not self.execution_enabled:
            raise ValueError("Exchange execution disabled")
        params = {"symbol": symbol, "newClientOrderId": client_id, "side": side.upper(), "type": "MARKET",
                  "newOrderRespType": "FULL", **size}
        return self.request("POST", "/api/v3/order", params, signed=True)

    @property
    def execution_enabled(self):
        if self.environment == "testnet":
            return os.getenv("BINANCE_TESTNET_EXECUTION_ENABLED", "false").strip().lower() == "true"
        return (self.environment == "production"
                and os.getenv("BINANCE_PRODUCTION_EXECUTION_ENABLED", "false").strip().lower() == "true"
                and os.getenv("BINANCE_PRODUCTION_ACK", "") == "I_UNDERSTAND_REAL_MONEY")

    def check_production_order(self, params):
        if not self.execution_enabled:
            raise ValueError("Production mutation blocked: execution OFF or acknowledgement missing")
        if (params.get("symbol"), params.get("type")) != ("BTCUSDT", "MARKET"):
            raise ValueError("Production supports BTCUSDT MARKET only")
        cap = dec(os.getenv("BINANCE_PRODUCTION_MAX_ORDER_USDT", "0"))
        if cap <= 0:
            raise ValueError("Set a positive production maximum order USDT before enabling execution")
        if params.get("side") == "BUY" and "quantity" not in params:
            notional = dec(params.get("quoteOrderQty", "0"))
        elif params.get("side") == "SELL" and "quoteOrderQty" not in params:
            bid, _ = self.quote("BTCUSDT")
            notional = dec(params.get("quantity", "0")) * bid
        else:
            raise ValueError("Invalid production order sizing")
        if notional <= 0 or notional > cap:
            raise ValueError("Production order exceeds configured cap or is empty; no order sent")
        commission = self.request("GET", "/api/v3/account/commission", {"symbol": "BTCUSDT"}, signed=True)
        discount = commission.get("discount") if isinstance(commission, dict) else None
        if (not isinstance(discount, dict) or not isinstance(discount.get("enabledForAccount"), bool)
                or not isinstance(discount.get("enabledForSymbol"), bool)):
            raise ValueError("Production commission settings unavailable; no order sent")
        if discount["enabledForAccount"] and discount["enabledForSymbol"]:
            raise ValueError("Disable third-asset fee discount before production orders; native BTC/USDT fees only")


def balances(account):
    return {r["asset"]: {"free": dec(r["free"]), "locked": dec(r["locked"])} for r in account["balances"]}


def quantity_rules(info):
    filters = {f["filterType"]: f for f in info["filters"]}
    return [filters[k] for k in ("LOT_SIZE", "MARKET_LOT_SIZE") if k in filters]


def dust_limit(info):
    positive = [dec(f[k]) for f in quantity_rules(info) for k in ("minQty", "stepSize") if dec(f[k]) > 0]
    return max(positive or [Decimal("1e-12")]) - Decimal("1e-18")


def order_size(info, side, cash, btc, bid, ask):
    """Reserve 1% for fees/rounding. Never include another branch's holdings."""
    cash, btc, bid, ask = map(dec, (cash, btc, bid, ask))
    filters = {f["filterType"]: f for f in info["filters"]}
    if side == "buy":
        if not info.get("quoteOrderQtyMarketAllowed", False):
            raise ValueError("quoteOrderQty market buys unavailable")
        quantum = Decimal(1).scaleb(-int(info["quoteAssetPrecision"]))
        quote = (cash * Decimal("0.99")).quantize(quantum, rounding=ROUND_DOWN)
        estimate = quote / ask
        notional = quote
        result = {"quoteOrderQty": plain(quote)}
    elif side == "sell":
        quantity = btc
        # Round to all active step grids, then validate every grid.
        for f in quantity_rules(info):
            step = dec(f["stepSize"])
            if step:
                quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        estimate, notional = quantity, quantity * bid
        result = {"quantity": plain(quantity)}
    else:
        raise ValueError("Invalid order side")
    if estimate <= 0:
        raise ValueError("Position is below exchange minimum")
    for f in quantity_rules(info):
        if estimate < dec(f["minQty"]) or (dec(f["maxQty"]) and estimate > dec(f["maxQty"])):
            raise ValueError("Quantity outside exchange limits")
        if side == "sell" and dec(f["stepSize"]) and estimate % dec(f["stepSize"]):
            raise ValueError("Incompatible quantity grids")
    for kind in ("MIN_NOTIONAL", "NOTIONAL"):
        if kind in filters:
            f = filters[kind]
            if f.get("applyToMarket", f.get("applyMinToMarket", True)) and notional < dec(f["minNotional"]):
                raise ValueError("Order below exchange minimum notional")
            if f.get("applyMaxToMarket", False) and notional > dec(f["maxNotional"]):
                raise ValueError("Order above exchange maximum notional")
    return result
