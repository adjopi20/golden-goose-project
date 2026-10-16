"""Durable environment-bound Spot attribution and native BTC/USDT fees."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .binance_spot import balances, dec, order_size, SpotError, UnknownExecution
from .data import utc
from .engine import configuration, initialize
from .storage import get_meta, message, set_meta

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS venue_orders(
 action_id TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL, symbol TEXT NOT NULL,
 side TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL, response TEXT);
CREATE TABLE IF NOT EXISTS venue_trades(
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL, order_id TEXT NOT NULL,
 qty TEXT NOT NULL, quote_qty TEXT NOT NULL, fee_asset TEXT NOT NULL,
 fee_qty TEXT NOT NULL, traded_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS venue_basis(
 account TEXT PRIMARY KEY, cost_quote TEXT NOT NULL, realized_quote TEXT NOT NULL);
"""


def identity(spot):
    return json.dumps({"environment": spot.environment, "symbol": "BTCUSDT",
                       "key_hash": hashlib.sha256(spot.key.encode()).hexdigest()}, sort_keys=True)


def require_bound(c, spot):
    if spot.environment not in {"testnet", "production"} or get_meta(c, "venue_identity") != identity(spot):
        raise ValueError("Wrong venue/key or uninitialized exchange ledger; never reuse another environment's DB")


def reserve(c, spot, capital, when):
    if spot.environment not in {"testnet", "production"} or not spot.key:
        raise ValueError("Only a dedicated authenticated exchange account can fund this ledger")
    existing = get_meta(c, "venue_identity")
    if existing:
        require_bound(c, spot)
        with c:
            initialize(c, configuration(), capital, "alerts", when)
        return
    if get_meta(c, "contract") or c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
        raise ValueError("Existing paper/alerts DB cannot be converted; choose a NEW exchange DB")
    account = spot.account()
    if not account.get("canTrade", False):
        raise ValueError("Exchange account does not permit trading")
    if spot.open_orders("BTCUSDT"):
        raise ValueError("Use a dedicated exchange account without open BTCUSDT orders")
    free = balances(account).get("USDT", {}).get("free", Decimal(0))
    if free < dec(capital) or dec(capital) <= 0:
        raise ValueError("Insufficient free USDT for initial allocation")
    c.executescript(SCHEMA)
    with c:
        c.execute("BEGIN IMMEDIATE")
        # Guard again inside the write lock against competing initialization.
        if get_meta(c, "contract"):
            raise ValueError("Ledger was initialized concurrently; retry safely")
        initialize(c, configuration(), capital, "alerts", when)
        for row in c.execute("SELECT id FROM accounts").fetchall():
            c.execute("INSERT INTO venue_basis VALUES(?,?,?)", (row[0], "0", "0"))
        set_meta(c, "venue_identity", identity(spot))
        set_meta(c, "quote_asset", "USDT")
        set_meta(c, "execution", "binance_spot_" + spot.environment)
        actual = balances(account)
        unallocated = {"BTC": str(actual.get("BTC", {}).get("free", Decimal(0)) + actual.get("BTC", {}).get("locked", Decimal(0))),
                       "USDT": str(actual["USDT"]["free"] + actual["USDT"]["locked"] - dec(capital))}
        set_meta(c, "venue_unallocated", json.dumps(unallocated, sort_keys=True))
        message(c, "venue-funded", f"{spot.environment.upper()}: allocated {capital} USDT 50:50 in ledger. No order placed. Extra funds remain unallocated.")


def inventory(c, spot):
    require_bound(c, spot)
    account = spot.account()
    if not account.get("canTrade", False):
        raise ValueError("Venue trading permission disabled; execution paused")
    actual = balances(account)
    known = {r["client_id"] for r in c.execute("SELECT client_id FROM venue_orders")}
    open_orders = spot.open_orders("BTCUSDT")
    if any(r.get("clientOrderId") not in known for r in open_orders):
        raise ValueError("Unattributed open order; automatic execution paused")
    sums = {"USDT": sum((dec(r[0]) for r in c.execute("SELECT cash FROM accounts")), Decimal(0)),
            "BTC": sum((dec(r[0]) for r in c.execute("SELECT btc FROM accounts")), Decimal(0))}
    extras = json.loads(get_meta(c, "venue_unallocated"))
    for asset, total in sums.items():
        found = actual.get(asset, {"free": Decimal(0), "locked": Decimal(0)})
        if found["free"] + found["locked"] != total + dec(extras[asset]):
            raise ValueError("Exchange inventory mismatch: deposit, withdrawal, external trade or Testnet reset; paused, no automatic reallocation")
    return actual


def _check_order(journal, order):
    if (order.get("symbol"), order.get("clientOrderId"), order.get("side")) != (
            journal["symbol"], journal["client_id"], journal["side"].upper()):
        raise ValueError("Order attribution mismatch; paused")
    if order.get("status") not in TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
        raise ValueError("Unknown exchange order status; paused")


def book(c, journal, order, trades, when):
    """Atomic import: actual fill IDs are unique and native fees never vanish."""
    _check_order(journal, order)
    action = c.execute("SELECT * FROM actions WHERE id=?", (journal["action_id"],)).fetchone()
    if action is None:
        raise ValueError("Missing attributed strategy action")
    validated = []
    ids = set()
    with localcontext() as ctx:
        ctx.prec = 40
        for trade in trades:
            if (trade.get("symbol"), str(trade.get("orderId")), trade.get("isBuyer")) != (
                    journal["symbol"], str(order["orderId"]), journal["side"] == "buy"):
                raise ValueError("Trade attribution mismatch")
            environment = "production" if get_meta(c, "execution") == "binance_spot_production" else "testnet"
            tid = "binance-" + environment + ":" + journal["symbol"] + ":" + str(trade["id"])
            if tid in ids:
                raise ValueError("Duplicate trade in venue response")
            ids.add(tid)
            q, quote, price, fee = map(dec, (trade["qty"], trade["quoteQty"], trade["price"], trade["commission"]))
            asset = trade["commissionAsset"]
            if q <= 0 or price <= 0 or quote <= 0:
                raise ValueError("Invalid exchange fill")
            if fee and asset not in {"BTC", "USDT"}:
                raise ValueError("Third-asset commission needs an accounting adapter; paused, not silently omitted")
            traded = utc(datetime.fromtimestamp(int(trade["time"]) / 1000, timezone.utc))
            if not utc(action["created_at"]) <= traded <= utc(when):
                raise ValueError("Fill timestamp outside proposal/recording bounds")
            validated.append((tid, trade, q, quote, price, fee, asset, traded.isoformat()))
        if sum((t[2] for t in validated), Decimal(0)) != dec(order["executedQty"]):
            raise ValueError("Trade history incomplete: executed quantity mismatch")
        if sum((t[3] for t in validated), Decimal(0)) != dec(order["cummulativeQuoteQty"]):
            raise ValueError("Trade history incomplete: quote quantity mismatch")
        with c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT * FROM venue_orders WHERE action_id=?", (action["id"],)).fetchone()
            known_ids = {r[0] for r in c.execute("SELECT id FROM venue_trades WHERE action_id=?", (action["id"],))}
            if not known_ids.issubset(ids) or (current["state"] == "terminal" and order["status"] not in TERMINAL):
                raise ValueError("Stale order/trade response cannot roll ledger state backward")
            other = c.execute("SELECT action_id FROM venue_trades WHERE order_id=? AND action_id!=?", (str(order["orderId"]), action["id"])).fetchone()
            if other:
                raise ValueError("Venue order ID already attributed to another action")
            account = c.execute("SELECT * FROM accounts WHERE id=?", (action["account"],)).fetchone()
            cash, btc = dec(account["cash"]), dec(account["btc"])
            basis_row = c.execute("SELECT * FROM venue_basis WHERE account=?", (action["account"],)).fetchone()
            cost, realized = dec(basis_row["cost_quote"]), Decimal(basis_row["realized_quote"])
            for tid, trade, q, quote, price, fee, asset, traded in validated:
                payload = json.dumps(trade, sort_keys=True)
                old = c.execute("SELECT payload FROM venue_trades WHERE id=?", (tid,)).fetchone()
                if old:
                    if old[0] != payload:
                        raise ValueError("Exchange trade revision detected")
                    continue
                if action["side"] == "buy":
                    cash -= quote
                    btc += q
                    cost += quote + (fee if asset == "USDT" else 0)
                else:
                    disposed = q + (fee if asset == "BTC" else 0)
                    if disposed > btc or btc <= 0:
                        raise ValueError("Sell exceeds attributed BTC")
                    removed = cost * disposed / btc
                    cost -= removed
                    realized += quote - (fee if asset == "USDT" else 0) - removed
                    cash += quote
                    btc -= q
                if fee:
                    if asset == "BTC":
                        btc -= fee
                    else:
                        cash -= fee
                if min(cash, btc) < 0:
                    raise ValueError("Fill would consume another submodel's funds; reconciliation halted")
                c.execute("INSERT INTO venue_trades VALUES(?,?,?,?,?,?,?,?,?)", (
                    tid, action["id"], str(order["orderId"]), str(q), str(quote), asset, str(fee), traded, payload))
                # Native fee asset/qty and exact quote value live in venue_trades.
                c.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)", (
                    tid, action["id"], when, traded, str(q), str(price), str(fee if asset == "USDT" else 0), action["side"]))
                message(c, "fill:" + tid, f"{environment.upper()} {action['account']} {action['side'].upper()}: {q} BTC; quote={quote} USDT; fee={fee} {asset}; trade={trade['id']}")
            c.execute("UPDATE accounts SET cash=?,btc=? WHERE id=?", (str(cash), str(btc), action["account"]))
            c.execute("UPDATE venue_basis SET cost_quote=?,realized_quote=? WHERE account=?", (str(cost), str(realized), action["account"]))
            terminal = order["status"] in TERMINAL
            c.execute("UPDATE venue_orders SET state=?,response=? WHERE action_id=?", (
                "terminal" if terminal else "open", json.dumps(order, sort_keys=True), action["id"]))
            if terminal:
                status = "filled" if order["status"] == "FILLED" else "venue_" + order["status"].lower()
                c.execute("UPDATE actions SET status=? WHERE id=?", (status, action["id"]))


def reconcile(c, spot, when):
    require_bound(c, spot)
    for journal in c.execute("SELECT * FROM venue_orders WHERE state != 'terminal'").fetchall():
        order = spot.order(journal["symbol"], journal["client_id"])
        if order is None:
            # Even a stable ID may be reused by Binance after an earlier fill.
            # Therefore absence never gives permission to retry a POST.
            raise ValueError("Order outcome unknown/not found; no automatic resubmission. Operator investigation required")
        with c:
            c.execute("UPDATE venue_orders SET response=? WHERE action_id=?", (json.dumps(order, sort_keys=True), journal["action_id"]))
        trades = spot.trades(journal["symbol"], order["orderId"])
        book(c, journal, order, trades, when)
    # Balance equality alone cannot detect a reset that happens to restore the
    # same inventory. Once per UTC day, verify our completed order history too.
    day = utc(when).date().isoformat()
    if get_meta(c, "venue_history_audit_date") != day:
        for journal in c.execute("SELECT * FROM venue_orders WHERE state='terminal'").fetchall():
            order = spot.order(journal["symbol"], journal["client_id"])
            if order is None:
                raise ValueError("Completed order history missing: possible Testnet reset; preserve ledger, pause and investigate")
            _check_order(journal, order)
            previous = json.loads(journal["response"])
            if (order.get("orderId"), order.get("status"), dec(order["executedQty"]), dec(order["cummulativeQuoteQty"])) != (
                    previous.get("orderId"), previous.get("status"), dec(previous["executedQty"]), dec(previous["cummulativeQuoteQty"])):
                raise ValueError("Completed order history changed: possible Testnet reset/reused ID; paused")
        with c:
            set_meta(c, "venue_history_audit_date", day)
    inventory(c, spot)


def execute(c, spot, info, when, *, enabled=False):
    require_bound(c, spot)
    reconcile(c, spot, when)
    if not enabled:
        if spot.environment == "production":
            # OFF is observation, not a queue of missed orders to replay later.
            with c:
                for action in c.execute("SELECT * FROM actions WHERE status='pending'").fetchall():
                    if utc(action['due_close']) <= utc(when) and not c.execute(
                            "SELECT 1 FROM venue_orders WHERE action_id=?", (action['id'],)).fetchone():
                        c.execute("UPDATE actions SET status='expired_disabled' WHERE id=?", (action['id'],))
                        message(c, 'disabled:' + action['id'], 'PRODUCTION execution OFF: missed proposal expired; no backfilled order ' + action['id'])
        return
    # The transport has its own final guard. Keep it before intent creation too.
    if not spot.execution_enabled:
        raise ValueError("Exchange execution disabled")
    if get_meta(c, "error"):
        raise ValueError("Data error present; new exchange orders blocked")
    for action in c.execute("SELECT * FROM actions WHERE status='pending' ORDER BY account").fetchall():
        if c.execute("SELECT 1 FROM venue_orders WHERE action_id=?", (action["id"],)).fetchone():
            continue
        if utc(when) < utc(action["due_close"]):
            continue
        if (utc(when) - utc(action["due_close"])).total_seconds() > 3600:
            with c:
                c.execute("UPDATE actions SET status='expired' WHERE id=?", (action["id"],))
                message(c, "expired:" + action["id"], "TESTNET missed execution window; no stale order: " + action["id"])
            continue
        actual = inventory(c, spot)
        account = c.execute("SELECT * FROM accounts WHERE id=?", (action["account"],)).fetchone()
        bid, ask = spot.quote("BTCUSDT")
        size = order_size(info, action["side"], account["cash"], account["btc"], bid, ask)
        if spot.environment == "production":
            spot.check_production_order(dict(symbol="BTCUSDT", type="MARKET", side=action["side"].upper(), **size))
        needed_asset = "USDT" if action["side"] == "buy" else "BTC"
        needed = dec(account["cash"]) if action["side"] == "buy" else dec(size["quantity"])
        if actual.get(needed_asset, {}).get("free", Decimal(0)) < needed:
            raise ValueError("Insufficient free venue funds; no order sent")
        cid = "mv1_" + action["id"]  # 28 chars, stable action -> client ID.
        with c:
            c.execute("BEGIN IMMEDIATE")
            # One committed intent before any HTTP POST; no retries after crash.
            fresh = c.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()
            if fresh[0] != "pending" or c.execute("SELECT 1 FROM venue_orders WHERE action_id=?", (action["id"],)).fetchone():
                continue
            c.execute("INSERT INTO venue_orders VALUES(?,?,?,?,?,?,?)", (
                action["id"], cid, "BTCUSDT", action["side"], json.dumps(size, sort_keys=True), "unknown", None))
        try:
            order = spot.market("BTCUSDT", cid, action["side"], size)
        except UnknownExecution:
            raise ValueError("POST outcome unknown; intent retained. Reconcile by client ID; NEVER blindly resend") from None
        except SpotError:
            # Preserve even definitive rejection for operator audit; query first.
            raise ValueError("Order POST rejected; retained intent requires investigation, no automatic retry") from None
        with c:
            c.execute("UPDATE venue_orders SET response=? WHERE action_id=?", (json.dumps(order, sort_keys=True), action["id"]))
        reconcile(c, spot, utc().isoformat())


def venue_report(c, spot):
    from .report import report
    require_bound(c, spot)
    result = report(c)
    result.update(execution="binance_spot_" + spot.environment, quote_asset="USDT", real_money=spot.environment == "production",
                  execution_enabled=spot.execution_enabled,
                  venue_error=get_meta(c, "venue_error", ""), notification_error=get_meta(c, "notification_error", ""),
                  last_check_stage=get_meta(c, "check_stage", ""),
                  valuation="Daily Binance BTCUSDT UTC close; cash in USDT, NOT USD; MVRV inputs remain Coin Metrics",
                  timing="Signal after validated daily publication, order scheduled at next UTC close; actual venue fills",
                  portfolio_metrics_scope="Recorded daily exchange marks; actual venue fills affect the next accepted mark. Current balances are separately valued at the last reference price.",
                  extra_costs="Actual venue fill prices and native BTC/USDT fees recorded; taxes/cash interest not modeled; nonzero third-asset fees halt",
                  native_fees=[dict(r) for r in c.execute("SELECT fee_asset,SUM(CAST(fee_qty AS REAL)) AS quantity FROM venue_trades GROUP BY fee_asset")],
                  cost_basis=[dict(r) for r in c.execute("SELECT * FROM venue_basis ORDER BY account")],
                  venue_orders=[dict(r) for r in c.execute("SELECT action_id,client_id,symbol,side,state FROM venue_orders")])
    return result
