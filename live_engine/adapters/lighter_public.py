"""Read-only Lighter market feed with bounded, fail-closed reconnect repair."""
import json
import time

import requests
from websockets.sync.client import connect

from .lighter import MARKETS, normalize


REST = "https://mainnet.zklighter.elliot.ai/api/v1"
STREAM = "wss://mainnet.zklighter.elliot.ai/stream"
INTERNAL_SYMBOLS = {"ETHUSDC": "ETH", "BNBUSDC": "BNB", "HYPEUSDT": "HYPE"}


class LighterPublic:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def _get(self, path, params=None):
        response = self.session.get(REST + path, params=params, timeout=(5, 20))
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise ValueError(f"Lighter {path} returned code {payload.get('code')}")
        return payload

    def validate_markets(self):
        books = self._get("/orderBooks")["order_books"]
        by_symbol = {book["symbol"]: book for book in books if book.get("market_type") == "perp"}
        selected = {}
        for internal, native in INTERNAL_SYMBOLS.items():
            book = by_symbol.get(native)
            if not book or book.get("status") != "active" or book.get("market_id") != MARKETS[native]:
                raise ValueError(f"Lighter market mapping changed or inactive: {native}")
            selected[internal] = {"symbol": native, "market_id": book["market_id"], "status": book["status"]}
        return selected

    @staticmethod
    def _normalize_rows(symbol, rows):
        market_id = MARKETS[INTERNAL_SYMBOLS[symbol]]
        by_id = {}
        for row in rows:
            tick = normalize(row, market_id)
            if tick is not None:
                tick.pop("market_id")
                tick["symbol"] = symbol
                if tick["agg_trade_id"] in by_id and by_id[tick["agg_trade_id"]] != tick:
                    raise ValueError("Conflicting Lighter feed duplicate")
                by_id[tick["agg_trade_id"]] = tick
        return [by_id[key] for key in sorted(by_id)]

    def _repair(self, symbol, previous_id, snapshot):
        if any(row["agg_trade_id"] == previous_id for row in snapshot):
            return [row for row in snapshot if row["agg_trade_id"] > previous_id]
        cursor = None
        collected = {}
        # Bounded retrieval: never silently bridge an outage longer than history allows.
        for _ in range(100):
            params = {"market_id": MARKETS[INTERNAL_SYMBOLS[symbol]], "market_type": "perp",
                      "sort_by": "trade_id", "sort_dir": "desc", "limit": 100, "type": "trade"}
            if cursor:
                params["cursor"] = cursor
            page = self._get("/trades", params)
            rows = self._normalize_rows(symbol, page.get("trades", []))
            for row in rows:
                if row["agg_trade_id"] > previous_id:
                    collected[row["agg_trade_id"]] = row
            if any(row["agg_trade_id"] == previous_id for row in rows):
                for row in snapshot:
                    if row["agg_trade_id"] > previous_id:
                        collected[row["agg_trade_id"]] = row
                return [collected[key] for key in sorted(collected)]
            next_cursor = page.get("next_cursor")
            if not rows or not next_cursor or next_cursor == cursor or min(row["agg_trade_id"] for row in rows) < previous_id:
                break
            cursor = next_cursor
        return None

    def stream(self, cursors, *, heartbeats=False):
        """Yield (symbol, sorted ticks, coverage_reset) from one subscription cycle."""
        by_id = {MARKETS[native]: internal for internal, native in INTERNAL_SYMBOLS.items()}
        subscribed = set()
        with connect(STREAM, open_timeout=15, ping_interval=30, ping_timeout=15, max_size=8_000_000) as ws:
            first = json.loads(ws.recv(timeout=15))
            if first.get("type") != "connected":
                raise ValueError("Lighter websocket did not acknowledge connection")
            for market_id in by_id:
                ws.send(json.dumps({"type": "subscribe", "channel": f"trade/{market_id}"}))
            last_message = time.monotonic()
            while True:
                try:
                    message = json.loads(ws.recv(timeout=1 if heartbeats else 60))
                except TimeoutError:
                    if not heartbeats or time.monotonic()-last_message > 60:
                        raise
                    yield None, [], False
                    continue
                last_message = time.monotonic()
                kind = message.get("type")
                if kind == "ping":
                    ws.send(json.dumps({"type": "pong"}))
                    if heartbeats:
                        yield None, [], False
                    continue
                if kind not in ("subscribed/trade", "update/trade"):
                    continue
                channel = message.get("channel", "")
                if not channel.startswith("trade:"):
                    raise ValueError("Unexpected Lighter trade channel")
                symbol = by_id.get(int(channel.split(":", 1)[1]))
                if symbol is None:
                    raise ValueError("Unrequested Lighter trade market")
                rows = self._normalize_rows(symbol, message.get("trades", []))
                if kind == "subscribed/trade":
                    previous = cursors.get(symbol)
                    repaired = rows if previous is None else self._repair(symbol, previous, rows)
                    reset = repaired is None
                    subscribed.add(symbol)
                    yield symbol, rows if reset else repaired, reset
                else:
                    if symbol not in subscribed:
                        raise ValueError("Lighter trade update arrived before subscription snapshot")
                    yield symbol, rows, False
