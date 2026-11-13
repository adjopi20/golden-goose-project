"""Archive Binance USD-M account evidence independently of order execution."""

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
from threading import Event
import time

from utils.engine.adapters.binance_futures_readonly import BinanceFuturesReadOnly, PreflightError

DAY = 86_400_000
HISTORY = {
    "trade": ("/fapi/v1/userTrades", "id"),
    "order": ("/fapi/v1/allOrders", "orderId"),
    "algo": ("/fapi/v1/allAlgoOrders", "algoId"),
    "income": ("/fapi/v1/income", "tranId"),
}
SNAPSHOTS = {
    "positions": "/fapi/v3/positionRisk",
    "open_orders": "/fapi/v1/openOrders",
    "open_algos": "/fapi/v1/openAlgoOrders",
}


class BinanceAuditReads(BinanceFuturesReadOnly):
    PRIVATE = BinanceFuturesReadOnly.PRIVATE | {endpoint for endpoint, _ in HISTORY.values()}


def history(client, endpoint, symbol, start, end):
    # Binance caps history pages. Split time ranges before treating a page as complete.
    params = dict(symbol=symbol) if symbol else {}
    rows = client.get(endpoint, startTime=start, endTime=end, limit=1000, **params)
    if not isinstance(rows, list):
        raise ValueError("invalid_broker_history")
    if len(rows) < 1000:
        yield from rows
        return
    if start == end:
        raise ValueError("broker_history_saturated_at_one_millisecond")
    middle = (start + end) // 2
    yield from history(client, endpoint, symbol, start, middle)
    yield from history(client, endpoint, symbol, middle + 1, end)


def capture(db, client, symbols, since, now):
    client.load_credentials()
    identity = hashlib.sha256(client._key.encode()).hexdigest()
    meta = dict(db.execute("SELECT key,value FROM metadata"))
    if meta and (meta.get("owner"), meta.get("key_hash"), meta.get("symbols")) != (
        "binance_broker_audit", identity, ",".join(symbols)
    ):
        raise ValueError("broker_audit_account_or_symbols_changed")
    if int(meta.get("coverage_through_ms", since)) > now:
        raise ValueError("broker_audit_clock_moved_backward")
    start = max(since, int(meta.get("coverage_through_ms", since)) - 3 * DAY)
    if not 0 <= now - start <= 89 * DAY:
        raise ValueError("broker_audit_history_outside_retention_or_clock")
    with db:
        for kind, (endpoint, field) in HISTORY.items():
            for symbol in (("",) if kind == "income" else symbols):
                for left in range(start, now + 1, 7 * DAY):
                    for row in history(client, endpoint, symbol, left, min(now, left + 7 * DAY - 1)):
                        if not isinstance(row, dict) or (symbol and row.get("symbol") != symbol):
                            raise ValueError("broker_history_symbol_mismatch")
                        native_id = str(row[field])
                        if not native_id.isdigit():
                            raise ValueError("invalid_broker_history_id")
                        if kind == "income":
                            native_id = row["incomeType"] + ":" + native_id
                        payload = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                                   (kind, row.get("symbol", ""), native_id, payload, now))
        for kind, endpoint in SNAPSHOTS.items():
            rows = client.get(endpoint)
            if not isinstance(rows, list):
                raise ValueError("invalid_broker_snapshot")
            payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
            previous = db.execute("SELECT payload FROM events WHERE kind=? ORDER BY observed_ms DESC LIMIT 1",
                                  (kind,)).fetchone()
            if previous is None or previous[0] != payload:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (kind, "", str(now), payload, now))
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", (
            ("owner", "binance_broker_audit"), ("key_hash", identity), ("symbols", ",".join(symbols)),
            ("coverage_from_ms", meta.get("coverage_from_ms", since)), ("coverage_through_ms", now),
            ("worker_heartbeat_ms", now),
        ))


def compare_journals(db, journals):
    """Join on Binance trade ID and order/client ID, with no inferred ownership."""
    account = db.execute("SELECT value FROM metadata WHERE key='key_hash'").fetchone()
    recorded = {}
    orders = {}
    for journal in journals:
        with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as ledger:
            ledger.execute("PRAGMA query_only=ON")
            ledger.execute("BEGIN")
            key = ledger.execute("SELECT value FROM metadata WHERE key='key_hash'").fetchone()
            if account and key and key[0] != account[0]:
                raise ValueError("journal_and_broker_archive_account_mismatch")
            for oid, session, payload in ledger.execute("SELECT order_id,session_id,payload FROM fills"):
                fill = json.loads(payload)
                recorded[(fill["symbol"], str(fill["id"]))] = (str(oid), session, journal.name, fill)
            for cid, role, session, params, response in ledger.execute(
                "SELECT client_id,role,session_id,params,response FROM orders"
            ):
                symbol = json.loads(params)["symbol"]
                native = json.loads(response or "{}")
                orders[(symbol, cid)] = (role, session, journal.name)
                native_id = native.get("actualOrderId" if role == "STOP" else "orderId")
                if native_id and str(native_id) != "0":
                    orders[(symbol, str(native_id))] = (role, session, journal.name)
    for symbol, payload in db.execute("SELECT symbol,payload FROM events WHERE kind IN ('order','algo') ORDER BY observed_ms"):
        row = json.loads(payload)
        cid = row.get("clientOrderId", row.get("clientAlgoId"))
        native_id = row.get("actualOrderId", row.get("orderId"))
        if native_id and str(native_id) != "0" and (symbol, cid) in orders:
            orders[(symbol, str(native_id))] = orders[(symbol, cid)]
    for symbol, native_id, payload in db.execute("SELECT symbol,native_id,payload FROM events WHERE kind='trade'"):
        trade = json.loads(payload)
        oid = str(trade["orderId"])
        known = recorded.get((symbol, native_id))
        owner = orders.get((symbol, oid))
        differences = []
        if known:
            differences = [field for field in ("qty", "price", "realizedPnl", "commission")
                           if Decimal(str(known[3][field])) != Decimal(str(trade[field]))]
            differences.extend(field for field in ("side", "time", "commissionAsset")
                               if known[3].get(field) != trade.get(field))
            if known[0] != oid:
                differences.append("orderId")
        yield dict(
            symbol=symbol, trade_id=native_id, venue=trade, linked_order=owner, journal_fill=known,
            differences=differences,
            status=("fill_mismatch" if differences else "recorded" if known else
                    "missing_ledger_fill" if owner else "unattributed"),
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path)
    signer = parser.add_mutually_exclusive_group()
    signer.add_argument("--private-key-file", type=Path)
    signer.add_argument("--hmac-secret-file", type=Path)
    parser.add_argument("--passphrase-file", type=Path)
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--since", type=date.fromisoformat)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--report", action="store_true")
    parser.add_argument("--journal", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    if args.passphrase_file and not args.private_key_file:
        parser.error("--passphrase-file requires --private-key-file")
    if args.report:
        with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for row in compare_journals(db, args.journal):
                print(json.dumps(row, allow_nan=False))
        return 0
    if not args.symbol or not args.api_key_file or not (args.private_key_file or args.hmac_secret_file):
        parser.error("Specify account mounts and at least one --symbol")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    os.umask(0o077)
    symbols = sorted(set(args.symbol))
    client = BinanceAuditReads(api_key_file=args.api_key_file, private_key_file=args.private_key_file,
                               passphrase_file=args.passphrase_file, hmac_secret_file=args.hmac_secret_file)
    with closing(client), closing(sqlite3.connect(args.database, timeout=5)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"metadata", "events"}:
            parser.error("Use a separate broker audit database")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                kind TEXT, symbol TEXT, native_id TEXT, payload TEXT, observed_ms INTEGER,
                PRIMARY KEY(kind,symbol,native_id,payload)
            ) WITHOUT ROWID;
        """)
        existing = db.execute("SELECT value FROM metadata WHERE key='coverage_from_ms'").fetchone()
        since = (int(datetime.combine(args.since, datetime.min.time(), timezone.utc).timestamp() * 1000)
                 if args.since else int(existing[0]) if existing else int(time.time() * 1000) - 7 * DAY)
        if existing and since != int(existing[0]):
            parser.error("--since differs from the existing archive start")
        while not stop.is_set():
            try:
                client.sync_time()
                now = int(time.time() * 1000) + client.offset
                capture(db, client, symbols, since, now)
                print(json.dumps(dict(event="broker_archive_complete", through_ms=now)), flush=True)
            except (PreflightError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                print(json.dumps(dict(event="broker_archive_error", error_type=type(error).__name__,
                                      reason=str(error) if isinstance(error, PreflightError) else None)), flush=True)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            stop.wait(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
