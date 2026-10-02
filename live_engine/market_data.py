"""Incremental minute bars and price/quantity profile buckets, not a tick archive."""
import json
import math

from .state_store import encode


MINUTE = 60_000
QUARTER = 15*MINUTE


class MarketData:
    def __init__(self, store, manager=None):
        self.store, self.manager = store, manager
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS feeds(symbol TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS minutes(
                symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL,
                source TEXT NOT NULL, PRIMARY KEY(symbol,timestamp_ms));
            CREATE TABLE IF NOT EXISTS profile_prices(
                symbol TEXT NOT NULL, session TEXT NOT NULL, price REAL NOT NULL,
                quantity REAL NOT NULL, PRIMARY KEY(symbol,session,price));
            CREATE TABLE IF NOT EXISTS profiles(
                symbol TEXT NOT NULL, session TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(symbol,session));
            CREATE TABLE IF NOT EXISTS evaluations(
                symbol TEXT NOT NULL, asof INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(symbol,asof));
        """)

    def state(self, symbol):
        row = self.store.db.execute("SELECT state FROM feeds WHERE symbol=?", (symbol,)).fetchone()
        return json.loads(row[0]) if row else None

    def seed_minutes(self, symbol, rows):
        """Warmup only, before live collection. Never replace trade-built bars."""
        if self.state(symbol):
            raise ValueError("Warmup must precede collection; do not reseed an active feed")
        with self.store.transaction():
            for row in rows:
                values = [row[k] for k in ("open", "high", "low", "close", "volume", "delta")]
                if (row["timestamp_ms"] % MINUTE or not all(math.isfinite(x) for x in values)
                        or not 0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]
                        or row["volume"] < abs(row["delta"])-1e-9):
                    raise ValueError("Invalid seed candle")
                # Empty native klines are not fabricated price observations.
                if row["volume"] > 0:
                    self.store.db.execute("INSERT OR IGNORE INTO minutes VALUES (?,?,?,?)",
                        (symbol, row["timestamp_ms"], encode(row), "binance_native_kline"))

    def consume(self, symbol, ticks, *, account=None, profile_key=lambda stamp:None,
                on_boundary=lambda symbol, end, state:None, consecutive_ids=True):
        """Atomic checkpoint + bars + paper fills. Callback precedes new-bar fills.

        Binance aggregate trade IDs are consecutive per market. Lighter IDs are
        only monotonic per market; its adapter repairs reconnects from venue
        history before calling this method with consecutive_ids=False.
        """
        with self.store.transaction():
            state = self.state(symbol)
            pending = []

            def execute():
                if pending and self.manager is not None and account is not None:
                    self.manager.ticks(account, pending)
                pending.clear()

            for tick in ticks:
                if tick["symbol"] != symbol:
                    raise ValueError("Feed symbol mismatch")
                if any(type(tick.get(k)) is not int or tick[k] < 0 for k in ("timestamp_ms", "agg_trade_id")):
                    raise ValueError("Invalid trade timestamp/id")
                if type(tick.get("buy")) is not bool or not all(math.isfinite(tick[k]) and tick[k] > 0 for k in ("price", "quantity")):
                    raise ValueError("Invalid trade price/quantity/side")
                stamp, opening = tick["timestamp_ms"], tick["timestamp_ms"]//MINUTE*MINUTE
                if state:
                    previous = state["cursor"]
                    if tick["agg_trade_id"] < previous["agg_trade_id"]:
                        continue
                    if tick["agg_trade_id"] == previous["agg_trade_id"]:
                        if tick != previous:
                            raise ValueError("Conflicting feed duplicate")
                        continue
                    if consecutive_ids and tick["agg_trade_id"] != previous["agg_trade_id"]+1:
                        raise ValueError("Unrepaired feed ID gap")
                    if stamp < previous["timestamp_ms"]:
                        raise ValueError("Feed timestamp regressed")
                    bar = state["bar"]
                    if opening > bar["timestamp_ms"]:
                        execute()
                        # Initial partial minute is deliberately not a full history bar.
                        if bar["timestamp_ms"] >= state["complete_from_ms"]:
                            self.store.db.execute("INSERT OR REPLACE INTO minutes VALUES (?,?,?,?)",
                                (symbol, bar["timestamp_ms"], encode(bar), "aggregate_trades"))
                        boundary = (bar["timestamp_ms"]//QUARTER+1)*QUARTER
                        while boundary <= opening:
                            on_boundary(symbol, boundary, {**state, "next_event_ms":stamp})
                            boundary += QUARTER
                        state["bar"] = None
                else:
                    state = dict(coverage_start_ms=stamp, complete_from_ms=opening+MINUTE,
                                 cursor=None, bar=None)
                price, quantity = tick["price"], tick["quantity"]
                delta = quantity if tick["buy"] else -quantity
                if state["bar"] is None:
                    state["bar"] = dict(timestamp_ms=opening, open=price, high=price,
                                        low=price, close=price, volume=quantity, delta=delta)
                else:
                    bar = state["bar"]
                    bar.update(high=max(bar["high"], price), low=min(bar["low"], price),
                               close=price, volume=bar["volume"]+quantity, delta=bar["delta"]+delta)
                key = profile_key(stamp)
                if key is not None:
                    self.store.db.execute("""INSERT INTO profile_prices VALUES (?,?,?,?)
                        ON CONFLICT(symbol,session,price) DO UPDATE SET quantity=quantity+excluded.quantity""",
                        (symbol, key, price, quantity))
                state["cursor"] = tick
                pending.append(tick)
            execute()
            if state:
                self.store.db.execute("INSERT OR REPLACE INTO feeds VALUES (?,?)", (symbol, encode(state)))

    def reset_coverage(self, symbol):
        """Discard continuity after a venue history gap; future profiles must wait."""
        with self.store.transaction():
            self.store.db.execute("DELETE FROM feeds WHERE symbol=?", (symbol,))
