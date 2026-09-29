"""Binance USD-M public feed; sequential aggTrade gap repair, no raw archive."""
import json
import math
import sys
import time

import requests
from websockets.sync.client import connect


class UnrecoverableGap(RuntimeError):
    pass


def normalize(row, symbol):
    if "s" in row and row["s"] != symbol:
        raise ValueError("Trade symbol mismatch")
    if type(row["m"]) is not bool or type(row["a"]) is not int or type(row["T"]) is not int:
        raise ValueError("Invalid Binance trade identity/side")
    price, quantity = float(row["p"]), float(row["q"])
    if row["a"] < 0 or row["T"] < 0 or not all(math.isfinite(x) and x > 0 for x in (price, quantity)):
        raise ValueError("Invalid Binance price/quantity")
    return dict(symbol=symbol, agg_trade_id=row["a"], timestamp_ms=row["T"],
                price=price, quantity=quantity, buy=not row["m"])


class BinancePublic:
    base = "https://fapi.binance.com"

    def __init__(self):
        self.http = requests.Session()
        self.next_request = 0.

    def get(self, path, **params):
        # <=60 requests/minute, including 20-weight aggTrade repair requests.
        for attempt in range(1, 4):
            time.sleep(max(0, self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+1
            try:
                response = self.http.get(self.base+path, params=params, timeout=(10, 20))
                response.raise_for_status()
                return response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = exc
            except requests.HTTPError as exc:
                # Do not hide access restrictions, IP bans or rate limiting.
                if exc.response is None or exc.response.status_code not in (500, 502, 503, 504):
                    raise
                error = exc
            if attempt == 3:
                raise error
            delay = 2 ** attempt
            print(f"Binance REST {path}: {type(error).__name__}; retry {attempt+1}/3 in {delay}s",
                  file=sys.stderr, flush=True)
            time.sleep(delay)

    def server_time(self):
        return int(self.get("/fapi/v1/time")["serverTime"])

    def validate_markets(self, symbols):
        markets = {r["symbol"]:r for r in self.get("/fapi/v1/exchangeInfo")["symbols"]}
        for symbol in symbols:
            r = markets.get(symbol, {})
            if r.get("status") != "TRADING" or r.get("contractType") != "PERPETUAL":
                raise ValueError(f"Unavailable USD-M perpetual: {symbol}")
        return {s:{k:markets[s][k] for k in ("symbol", "baseAsset", "quoteAsset", "marginAsset")}
                for s in symbols}

    def minute_history(self, symbol, start_ms, end_ms):
        """Completed native candles only. Base volume and taker-buy base volume."""
        cursor = start_ms
        while cursor < end_ms:
            rows = self.get("/fapi/v1/klines", symbol=symbol, interval="1m",
                            startTime=cursor, endTime=end_ms-1, limit=1500)
            if not rows:
                break
            for r in rows:
                stamp = int(r[0])
                if cursor <= stamp and stamp+60_000 <= end_ms:
                    volume, buy = float(r[5]), float(r[9])
                    if not 0 <= buy <= volume:
                        raise ValueError("Invalid kline taker volume")
                    yield dict(timestamp_ms=stamp, open=float(r[1]), high=float(r[2]),
                               low=float(r[3]), close=float(r[4]), volume=volume, delta=2*buy-volume)
            next_cursor = int(rows[-1][0])+60_000
            if next_cursor <= cursor:
                raise ValueError("Kline pagination did not advance")
            cursor = next_cursor

    def repair(self, symbol, previous, incoming):
        """Return missing trades, excluding incoming. Never treat an ID gap as no trades."""
        expected = previous["agg_trade_id"]+1
        if incoming["agg_trade_id"] <= expected:
            return
        if incoming["timestamp_ms"]-previous["timestamp_ms"] >= 48*3_600_000:
            raise UnrecoverableGap(f"{symbol}: checkpoint exceeds Binance 48h repair window")
        while expected < incoming["agg_trade_id"]:
            rows = self.get("/fapi/v1/aggTrades", symbol=symbol, fromId=expected, limit=1000)
            advanced = False
            for row in rows:
                trade = normalize(row, symbol)
                if trade["agg_trade_id"] >= incoming["agg_trade_id"]:
                    break
                if trade["agg_trade_id"] != expected:
                    raise UnrecoverableGap(f"{symbol}: missing aggregate trade {expected}")
                yield trade
                expected += 1
                advanced = True
            if not advanced:
                raise UnrecoverableGap(f"{symbol}: no repair data for {expected}")

    def stream(self, symbols):
        streams = "/".join(s.lower()+"@aggTrade" for s in symbols)
        # Binance's routed endpoint migration: aggTrade belongs to /market.
        with connect("wss://fstream.binance.com/market/stream?streams="+streams,
                     open_timeout=20, ping_interval=20, ping_timeout=20,
                     max_queue=1024) as socket:
            last_message = time.monotonic()
            while True:
                try:
                    message = json.loads(socket.recv(timeout=1))
                except TimeoutError:
                    if time.monotonic()-last_message > 60:
                        raise OSError("No public trade events for 60s; reconnect and repair")
                    yield None  # permits batch flush, heartbeats and graceful stop
                    continue
                row = message.get("data", {})
                if row.get("e") != "aggTrade" or row.get("s") not in symbols:
                    raise ValueError("Unexpected public stream message")
                last_message = time.monotonic()
                yield normalize(row, row["s"])
