"""Binance REST admission at the HTTP boundary; one SQLite file per egress IP."""
from collections import deque
from contextlib import closing, contextmanager
from email.utils import parsedate_to_datetime
from itertools import islice
from pathlib import Path
from time import monotonic, time as wall_time
import logging
import os
import re
import sqlite3
import tempfile


LOG = logging.getLogger(__name__)
GET_WEIGHT = {
    "/api/v3/account": 20,
    "/fapi/v1/time": 1, "/fapi/v1/exchangeInfo": 1, "/fapi/v1/aggTrades": 20,
    "/fapi/v3/account": 5, "/fapi/v1/accountConfig": 5, "/fapi/v3/positionRisk": 5,
    "/fapi/v1/symbolConfig": 5, "/fapi/v1/commissionRate": 20,
    "/fapi/v1/order": 1, "/fapi/v1/algoOrder": 1, "/fapi/v1/userTrades": 5,
    "/fapi/v1/allOrders": 5, "/fapi/v1/allAlgoOrders": 5, "/fapi/v1/income": 30,
}


def request_weight(method, path, params):
    if method == "GET":
        if path in GET_WEIGHT:
            return GET_WEIGHT[path]
        if path in {"/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"}:
            return 1 if params.get("symbol") else 40
        if path == "/fapi/v1/ticker/bookTicker":
            return 2 if params.get("symbol") else 5
        if path == "/fapi/v1/klines":
            limit = int(params.get("limit", 500))
            if not 1 <= limit <= 1500:
                raise ValueError("invalid Binance kline limit")
            return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
    elif method in {"POST", "DELETE"} and path in {"/fapi/v1/order", "/fapi/v1/algoOrder"}:
        return 0 if method == "POST" else 1
    raise ValueError("unbudgeted Binance REST endpoint")


class RestDeferred(RuntimeError):
    """Admission failed before HTTP; a mutation definitely was not transmitted."""

    def __init__(self, reason, delay):
        self.reason, self.delay = reason, delay
        super().__init__(f"Binance REST deferred: {reason}; retry in {delay:.2f}s")


class RestReadThrottled(RestDeferred):
    """Venue rejected a read, so the caller can defer without using stale data."""


def integer(value):
    return int(value) if re.fullmatch(r"[0-9]{1,16}", str(value)) else None


class RestBudget:
    def __init__(self, path=None, *, service=None, account=None, futures=None, spot=None,
                 orders_10s=None, orders_1m=None, clock=wall_time):
        configured = path or os.environ.get("BINANCE_REST_BUDGET_DB")
        # Local CLI tools also coordinate by default. In containers Compose must mount this
        # file's parent from the same host directory and set the explicit path.
        self.path = Path(configured or Path(tempfile.gettempdir()) / "golden-goose-binance-rest.sqlite")
        if not self.path.is_absolute():
            raise ValueError("Binance REST budget path must be absolute")
        self.uri = self.path.as_uri() + ("?mode=rw" if configured else "?mode=rwc")
        self.clock = clock
        self.service = service or os.environ.get("BINANCE_REST_SERVICE", "local")
        self.account = account or os.environ.get("BINANCE_REST_ACCOUNT", "unclassified")
        if any(not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", x) for x in (self.service, self.account)):
            raise ValueError("Invalid Binance REST service/account label")

        def limit(val, name, default, maximum):
            val = integer(val if val is not None else os.environ.get("BINANCE_REST_" + name, default))
            if val is None or not 10 <= val <= maximum:
                raise ValueError(f"Invalid Binance REST {name} budget")
            return val

        self.limits = {
            "futures": limit(futures, "FUTURES_1M", 1920, 2160),
            "spot": limit(spot, "SPOT_1M", 4800, 5400),
            "orders10": limit(orders_10s, "ORDERS_10S", 240, 270),
            "orders60": limit(orders_1m, "ORDERS_1M", 960, 1080),
        }
        self._initialized = False
        self._next_cleanup = 0.0
        self._next_flush = monotonic() + 5
        self._metrics = deque(maxlen=2048)
        self._pending_observations = deque()
        self.settings = ",".join(str(n) for n in self.limits.values())

    @contextmanager
    def _transaction(self, *, timeout=.125):
        # Brief transactions only. No lock spans HTTP or an exchange retry.
        with closing(sqlite3.connect(self.uri, uri=True, timeout=timeout)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not self._initialized:
                self._initialize(db)
            if db.execute("SELECT limits FROM rest_config WHERE id=1").fetchone()[0] != self.settings:
                raise RestDeferred("coordinator_settings_mismatch", 5)
            yield db
        self._initialized = True

    def _initialize(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS reservations(
                   id INTEGER PRIMARY KEY AUTOINCREMENT, stamp REAL, product TEXT, account TEXT,
                   weight INTEGER, orders INTEGER, done INTEGER DEFAULT 0)""")
        db.execute("CREATE INDEX IF NOT EXISTS rest_stamp ON reservations(stamp)")
        db.execute("""CREATE TABLE IF NOT EXISTS floors(
                   scope TEXT, seconds INTEGER, win INTEGER, used INTEGER,
                   PRIMARY KEY(scope,seconds,win))""")
        db.execute("CREATE TABLE IF NOT EXISTS cooldown(scope TEXT PRIMARY KEY, until REAL)")
        db.execute("""CREATE TABLE IF NOT EXISTS metrics(
                   minute INTEGER, service TEXT, method TEXT, endpoint TEXT, event TEXT,
                   count INTEGER, weight INTEGER, wait_seconds REAL, header_max INTEGER,
                   PRIMARY KEY(minute,service,method,endpoint,event))""")
        db.execute("CREATE TABLE IF NOT EXISTS rest_config (id INTEGER PRIMARY KEY CHECK(id=1), limits TEXT)")
        db.execute("INSERT OR IGNORE INTO rest_config VALUES (1,?)", (self.settings,))

    def _metric(self, now, method, path, event, weight=0, wait=0, header=0):
        self._metrics.append((int(now // 60), self.service, method, path, event, weight, wait, header))
        if monotonic() >= self._next_flush:
            self.flush_metrics()

    def flush_metrics(self):
        # Telemetry may wait for a later batch. It does not own reservations or cooldowns.
        if not self._metrics:
            return
        self._next_flush = monotonic() + 5
        started = monotonic()
        batch = list(islice(self._metrics, 128))
        try:
            with self._transaction(timeout=0) as db:
                db.executemany("""INSERT INTO metrics VALUES (?,?,?,?,?,1,?,?,?)
                    ON CONFLICT DO UPDATE SET count=count+1, weight=weight+excluded.weight,
                    wait_seconds=wait_seconds+excluded.wait_seconds,
                    header_max=max(header_max,excluded.header_max)""", batch)
        except (sqlite3.Error, RestDeferred) as error:
            if not self._busy(error):
                self._log_storage("metrics", error, started)
        else:
            for _ in batch:
                self._metrics.popleft()

    @staticmethod
    def _busy(error):
        code = getattr(error, "sqlite_errorcode", 0)
        return (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

    def _log_storage(self, phase, error, started, *, path="-", status="-"):
        LOG.warning("Binance REST coordinator %s failed: %s %s HTTP %s %s in %.1fms",
                    phase, self.service, path, status,
                    getattr(error, "sqlite_errorname", type(error).__name__),
                    (monotonic() - started) * 1000)

    def _scopes(self, product, weight, orders):
        yield product, 60, weight, self.limits[product], "product", product, "weight", "x-mbx-used-weight-1m"
        if orders:
            for seconds, suffix in ((10, "10s"), (60, "1m")):
                yield ("orders:" + self.account, seconds, 1, self.limits[f"orders{seconds}"],
                       "account", self.account, "orders", "x-mbx-order-count-" + suffix)

    def reserve(self, method, path, params, *, background=False):
        weight = request_weight(method, path, params)
        product = "spot" if path.startswith("/api/") else "futures"
        orders = method == "POST"
        while self._pending_observations:
            if not self._write_observation(*self._pending_observations[0]):
                raise RestDeferred("coordinator_response_pending", 1)
            self._pending_observations.popleft()
        error = None
        started = monotonic()
        cleanup = started >= self._next_cleanup
        try:
            with self._transaction() as db:
                now = self.clock()
                if cleanup:
                    db.execute("DELETE FROM reservations WHERE stamp <= ?", (now - 90,))
                    db.execute("DELETE FROM floors WHERE (win+1)*seconds < ?", (now - 60,))
                    db.execute("DELETE FROM metrics WHERE minute < ?", (int(now // 60) - 1440,))
                ban = db.execute("SELECT max(until) FROM cooldown WHERE scope IN (?,?,?)",
                                 ("ip", product, "orders:" + self.account if orders else "")).fetchone()[0]
                if ban is not None and ban > now:
                    error = RestDeferred("venue_cooldown", ban - now)
                for scope, span, cost, cap, field, key, units, _ in self._scopes(product, weight, orders):
                    if error is not None:
                        break
                    floor = db.execute("SELECT used FROM floors WHERE scope=? AND seconds=? AND win=?",
                                       (scope, span, int(now // span))).fetchone()
                    rolling, oldest = db.execute(
                        f"SELECT coalesce(sum({units}),0), min(stamp+CASE done WHEN 0 THEN 30 ELSE 0 END) "
                        f"FROM reservations WHERE {field}=? AND stamp>? "
                        f"AND (stamp>? OR done=0) AND {units}>0",
                        (key, now - span - 30, now - span)).fetchone()
                    # Keep a lane for recovery; even zero-weight POSTs respect a saturated
                    # lane and the product cooldown. Account caps reserve the same margin.
                    ceiling = cap * 7 // 10 if background else cap
                    observed = floor[0] if floor else 0
                    if max(rolling, observed) + cost > ceiling or (not cost and max(rolling, observed) >= ceiling):
                        waits = ([span - now % span] if observed + cost > ceiling or (not cost and observed >= ceiling) else []) + (
                        [oldest + span - now] if oldest is not None and
                        (rolling + cost > ceiling or (not cost and rolling >= ceiling)) else [])
                        error = RestDeferred("order_budget" if units == "orders" else "weight_budget",
                                             max(.1, min(waits)))
                if error is None:
                    # The reservation survives a crash/timeout, so never refund it.
                    row = db.execute("INSERT INTO reservations(stamp,product,account,weight,orders) VALUES(?,?,?,?,?)",
                                     (now, product, self.account, weight, int(orders)))
                    for scope, span, cost, *_ in self._scopes(product, weight, orders):
                        db.execute("""INSERT INTO floors VALUES (?,?,?,?) ON CONFLICT DO UPDATE
                                   SET used=used+excluded.used""", (scope, span, int(now // span), cost))
                    ticket = row.lastrowid, now, method, path, product, weight, orders
        except sqlite3.Error as db_error:
            self._log_storage("reservation", db_error, started, path=path)
            raise RestDeferred("coordinator_unavailable", 1) from None
        if cleanup:
            self._next_cleanup = started + 60
        if error is not None:
            self._metric(now, method, path, error.reason, wait=error.delay)
            raise error
        self._metric(now, method, path, "reserved", weight=weight)
        return ticket

    def observe(self, ticket, response, payload=None):
        """Post-send persistence must never classify a transmitted order as deferred."""
        status, now = response.status_code, self.clock()
        headers = {str(k).lower(): v for k, v in getattr(response, "headers", {}).items()}
        code = payload.get("code") if isinstance(payload, dict) else None
        throttle = status in (418, 429) or code in (-1003, -1015)
        match = re.search(r"banned until (\d{13})", str(payload.get("msg", "")), re.I) if (
            status == 418 and isinstance(payload, dict)) else None
        ban_until = int(match[1]) / 1000 if match else 0
        observation = (ticket, status, now, headers, ban_until, code, throttle)
        if not self._write_observation(*observation):
            # Retry the database write before this client sends anything else.
            # The caller still receives the actual HTTP result.
            self._pending_observations.append(observation)

    def _write_observation(self, ticket, status, now, headers, ban_until, code, throttle):
        started = monotonic()
        for attempt in range(2):
            try:
                metrics = self._observe_once(ticket, now, headers, ban_until, status, code, throttle)
                break
            except (sqlite3.Error, RestDeferred) as failure:
                if not self._busy(failure) or attempt == 1:
                    self._log_storage("observation", failure, started, path=ticket[3], status=status)
                    return False
        for event, values in metrics:
            self._metric(now, ticket[2], ticket[3], event, **values)
        self._metric(now, ticket[2], ticket[3], f"http_{status}")
        return True

    def _observe_once(self, ticket, now, headers, ban_until, status, code, throttle):
        rid, sent, _, path, product, weight, orders = ticket
        metrics = []
        with self._transaction() as db:
            db.execute("UPDATE reservations SET done=1, stamp=? WHERE id=?", (now, rid))
            for scope, span, _, _, field, key, units, header in self._scopes(product, weight, orders):
                if path == "/fapi/v1/ticker/bookTicker":
                    continue  # Binance documents this route's weight header as inaccurate.
                value = integer(headers.get(header))
                if value is None:
                    continue
                metrics.append((header, {"header": value}))
                stamp = now
                try:
                    stamp = parsedate_to_datetime(headers.get("date", "")).timestamp()
                except (ValueError, TypeError, OverflowError):
                    if int(sent // span) != int(now // span):
                        continue  # Cannot attribute a response spanning a boundary.
                pending = db.execute(
                    f"SELECT coalesce(sum({units}),0) FROM reservations WHERE {field}=? AND done=0 AND stamp>?",
                    (key, now - 30)).fetchone()[0]
                db.execute("""INSERT INTO floors VALUES(?,?,?,?) ON CONFLICT DO UPDATE
                           SET used=max(used,excluded.used)""", (scope, span, int(stamp // span), value + pending))
            if throttle:
                retry = integer(headers.get("retry-after"))
                if retry is None:
                    try:
                        retry = max(0, parsedate_to_datetime(headers.get("retry-after", "")).timestamp() - now)
                    except (ValueError, TypeError, OverflowError):
                        pass
                if status == 418:
                    expires = max(now + (retry if retry is not None else 0), ban_until)
                    if expires <= now:
                        expires = now + 3 * 86400
                    scope = "ip"
                else:
                    scope = "orders:" + self.account if code == -1015 else product if code == -1003 else "ip"
                    expires = now + (retry if retry is not None else 60)
                expires = max(expires, now + 1)
                db.execute("""INSERT INTO cooldown VALUES (?,?) ON CONFLICT DO UPDATE
                           SET until=max(until,excluded.until)""", (scope, expires))
                metrics.append(("cooldown", {"wait": expires - now}))
        return metrics

    def transport_failure(self, ticket):
        _, _, method, path, *_ = ticket
        self._metric(self.clock(), method, path, "transport_unknown")

    def acquire(self, method, path, params, *, background=False):
        return self.reserve(method, path, params, background=background)
