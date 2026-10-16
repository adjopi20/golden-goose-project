from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from live_mvrv.binance_spot import Spot, SpotError, UnknownExecution, dust_limit, order_size
from live_mvrv.binance_execution import book, execute, inventory, reconcile, reserve, venue_report
from live_mvrv.engine import apply_day, configuration, initialize
from live_mvrv.storage import connect, get_meta

NOW = "2020-01-03T00:00:10+00:00"
INFO = dict(symbol="BTCUSDT", baseAsset="BTC", quoteAsset="USDT", status="TRADING", isSpotTradingAllowed=True,
            quoteOrderQtyMarketAllowed=True, orderTypes=["MARKET"], quoteAssetPrecision=2,
            filters=[dict(filterType="LOT_SIZE", minQty="0.001", maxQty="100", stepSize="0.001"),
                     dict(filterType="MARKET_LOT_SIZE", minQty="0", maxQty="100", stepSize="0"),
                     dict(filterType="MIN_NOTIONAL", minNotional="5", applyToMarket=True)])


class Fake:
    environment = "testnet"
    key = "test-key"
    execution_enabled = True
    def __init__(self):
        self.cash, self.btc = Decimal(2000), Decimal(0)
        self.orders, self.fills, self.posts = {}, {}, []
        self.unknown = False
        self.opens = []
    def account(self):
        return dict(canTrade=True, balances=[dict(asset="USDT", free=str(self.cash), locked="0"),
                                            dict(asset="BTC", free=str(self.btc), locked="0")])
    def open_orders(self, symbol): return self.opens
    def quote(self, symbol): return Decimal(100), Decimal(100)
    def order(self, symbol, cid): return self.orders.get(cid)
    def trades(self, symbol, oid): return self.fills[oid]
    def market(self, symbol, cid, side, size):
        self.posts.append((cid, side, size))
        if self.unknown: raise UnknownExecution()
        q = Decimal(size.get("quantity", "0")) or Decimal(size["quoteOrderQty"]) / 100
        quote = q * 100
        tid = len(self.posts)
        order = dict(symbol=symbol, clientOrderId=cid, side=side.upper(), status="FILLED", orderId=tid,
                     executedQty=str(q), cummulativeQuoteQty=str(quote))
        trade = dict(symbol=symbol, orderId=tid, id=tid, qty=str(q), quoteQty=str(quote), price="100",
                     commission="0", commissionAsset="USDT", time=1578009600000, isBuyer=side == "buy")
        self.orders[cid] = order
        self.fills[tid] = [trade]
        self.cash += quote if side == "sell" else -quote
        self.btc += q if side == "buy" else -q
        return order


def ledger(tmp_path):
    c = connect(tmp_path / "testnet.sqlite")
    spot = Fake()
    reserve(c, spot, "1000", "2020-01-02T01:00:00+00:00")
    return c, spot


def signal(c, *, z=-.1, day="2020-01-01", when="2020-01-02T01:00:00+00:00", dust="1e-12"):
    row = dict(date=day, price=100, z=z, fixed_0_7_entry=0, fixed_0_7_exit=7,
               percentile_4y_p5_p95_entry=-.5, percentile_4y_p5_p95_exit=3)
    with c:
        apply_day(c, row, configuration(), when, "alerts", btc_dust=dust)
    return c.execute("SELECT * FROM actions WHERE status='pending' AND account='fixed_0_7'").fetchone()


def journal(c, action):
    with c:
        c.execute("INSERT INTO venue_orders VALUES(?,?,?,?,?,?,?)", (action["id"], "mv1_"+action["id"], "BTCUSDT", action["side"], "{}", "unknown", None))
    return c.execute("SELECT * FROM venue_orders WHERE action_id=?", (action["id"],)).fetchone()


def example(j, *, q="1", quote="100", fee="0", asset="USDT", status="FILLED", tid=1, buyer=True):
    order = dict(symbol="BTCUSDT", clientOrderId=j["client_id"], side=j["side"].upper(), status=status,
                 orderId=10, executedQty=q, cummulativeQuoteQty=quote)
    trade = dict(symbol="BTCUSDT", orderId=10, id=tid, qty=q, quoteQty=quote, price=str(Decimal(quote)/Decimal(q)),
                 commission=fee, commissionAsset=asset, time=1578009600000, isBuyer=buyer)
    return order, trade


def test_production_post_blocked_and_secrets_not_required():
    spot = Spot(environment="production_readonly", key="", secret="")
    with pytest.raises(ValueError, match="blocked"):
        spot.request("POST", "/api/v3/order")
    with pytest.raises(ValueError, match="disabled"):
        spot.market("BTCUSDT", "x", "buy", {})


def test_testnet_requires_explicit_switch(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET_EXECUTION_ENABLED", raising=False)
    with pytest.raises(ValueError, match="disabled"):
        Spot(environment="testnet").market("BTCUSDT", "x", "buy", {})


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_redacts_network_errors(method):
    class Session:
        def request(self, *args, **kwargs): raise requests.ConnectionError("secret-api-key?signature=SECRET")
    spot = Spot(environment="testnet", key="secret-api-key", secret="SECRET", session=Session())
    with pytest.raises(SpotError) as error:
        spot.request(method, "/api/v3/order", signed=True)
    assert "SECRET" not in str(error.value) and "secret-api-key" not in str(error.value)
    assert isinstance(error.value, UnknownExecution) == (method == "POST")


def test_signed_request_has_hmac_no_redirect():
    class Response:
        status_code = 200
        def json(self): return {"ok": True}
    class Session:
        def request(self, *args, **kwargs): self.kwargs = kwargs; return Response()
    session = Session()
    Spot(environment="testnet", key="key", secret="secret", session=session).account()
    assert len(session.kwargs["params"]["signature"]) == 64
    assert session.kwargs["allow_redirects"] is False


def test_daily_mark_exact_utc_close(monkeypatch):
    spot = Spot(environment="testnet", key="", secret="")
    start = 1577836800000
    def request(method, path, params):
        assert params["startTime"] == start and params["endTime"] == start + 86400000 - 1
        return [[start, "1", "1", "1", "100", "0", start + 86400000 - 1]]
    monkeypatch.setattr(spot, "request", request)
    assert spot.daily_close("BTCUSDT", "2020-01-01") == Decimal(100)


def test_daily_mark_rejects_other_date(monkeypatch):
    spot = Spot(environment="testnet", key="", secret="")
    monkeypatch.setattr(spot, "request", lambda *a: [[0, "1", "1", "1", "100", "0", 0]])
    with pytest.raises(ValueError, match="exact"): spot.daily_close("BTCUSDT", "2020-01-01")


def test_size_reserve_rounding_minimum_and_dust():
    assert order_size(INFO, "buy", 500, 0, 100, 100) == {"quoteOrderQty": "495.00"}
    assert order_size(INFO, "sell", 0, "1.0009", 100, 100) == {"quantity": "1.000"}
    assert Decimal("0.0009") <= dust_limit(INFO)
    with pytest.raises(ValueError, match="minimum"):
        order_size(INFO, "sell", 0, ".0009", 100, 100)
    with pytest.raises(ValueError): order_size(INFO, "buy", 1, 0, 100, 100)


def test_no_conversion_of_existing_alerts(tmp_path):
    c = connect(tmp_path / "old.sqlite")
    with c: initialize(c, configuration(), "1000", "alerts", NOW)
    with pytest.raises(ValueError, match="cannot be converted"): reserve(c, Fake(), "1000", NOW)
    assert get_meta(c, "venue_identity") is None


def test_funding_bound_and_unallocated(tmp_path):
    c, spot = ledger(tmp_path)
    inventory(c, spot)
    assert json.loads(get_meta(c, "venue_unallocated")) == {"BTC": "0", "USDT": "1000"}
    spot.cash += 1
    with pytest.raises(ValueError, match="mismatch"): inventory(c, spot)
    spot.key = "other"
    with pytest.raises(ValueError, match="Wrong"): inventory(c, spot)


def test_btc_fee_and_compounding_no_other_branch(tmp_path):
    c, spot = ledger(tmp_path)
    j = journal(c, signal(c))
    order, trade = example(j, q="4", quote="400", fee=".004", asset="BTC")
    book(c, j, order, [trade], NOW)
    first = c.execute("SELECT * FROM accounts WHERE id='fixed_0_7'").fetchone()
    assert Decimal(first["cash"]) == 100 and Decimal(first["btc"]) == Decimal("3.996")
    book(c, j, order, [trade], NOW)  # Idempotent.
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    a = signal(c, z=8, day="2020-01-02", when=NOW)
    j2 = journal(c, a)
    order2, trade2 = example(j2, q="3.996", quote="799.2", fee=".7992", buyer=False, tid=2)
    trade2["time"] = 1578009610000
    order2["orderId"] = trade2["orderId"] = 11
    book(c, j2, order2, [trade2], NOW)
    first = c.execute("SELECT * FROM accounts WHERE id='fixed_0_7'").fetchone()
    assert Decimal(first["cash"]) == Decimal("898.4008")
    assert c.execute("SELECT cash FROM accounts WHERE id='percentile_4y_p5_p95'").fetchone()[0] == "500.0"
    basis = c.execute("SELECT * FROM venue_basis WHERE account='fixed_0_7'").fetchone()
    assert Decimal(basis["realized_quote"]) == Decimal("398.4008")
    assert Decimal(order_size(INFO, "buy", first["cash"], 0, 100, 100)["quoteOrderQty"]) > 495


def test_partial_fill_then_full_does_not_double_count(tmp_path):
    c, spot = ledger(tmp_path)
    j = journal(c, signal(c))
    order, trade = example(j, status="PARTIALLY_FILLED")
    book(c, j, order, [trade], NOW)
    assert c.execute("SELECT status FROM actions WHERE id=?", (j["action_id"],)).fetchone()[0] == "pending"
    order["executedQty"], order["cummulativeQuoteQty"], order["status"] = "2", "200", "FILLED"
    trade2 = dict(trade, id=2)
    book(c, j, order, [trade, trade2], NOW)
    assert c.execute("SELECT cash FROM accounts WHERE id='fixed_0_7'").fetchone()[0] == "300.0"
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["BNB", "wrong_order", "missing_fill", "overspend"])
def test_bad_fills_fail_atomically(tmp_path, change):
    c, spot = ledger(tmp_path)
    j = journal(c, signal(c))
    order, trade = example(j)
    if change == "BNB": trade.update(commission=".001", commissionAsset="BNB")
    if change == "wrong_order": trade["orderId"] = 999
    if change == "missing_fill": order["executedQty"] = "2"
    if change == "overspend": order["cummulativeQuoteQty"] = trade["quoteQty"] = "600"
    with pytest.raises(ValueError): book(c, j, order, [trade], NOW)
    assert c.execute("SELECT COUNT(*) FROM venue_trades").fetchone()[0] == 0
    assert c.execute("SELECT cash FROM accounts WHERE id='fixed_0_7'").fetchone()[0] == "500.0"


def test_unknown_order_never_reposts_after_restart(tmp_path):
    c, spot = ledger(tmp_path)
    signal(c)
    spot.unknown = True
    with pytest.raises(ValueError, match="unknown"): execute(c, spot, INFO, NOW, enabled=True)
    assert len(spot.posts) == 1
    c.close()
    c = connect(tmp_path / "testnet.sqlite")
    with pytest.raises(ValueError, match="unknown"): execute(c, spot, INFO, NOW, enabled=True)
    assert len(spot.posts) == 1


def test_unknown_post_that_filled_recovers_without_repost(tmp_path):
    c, spot = ledger(tmp_path)
    signal(c)
    original = spot.market
    def accepted_but_timeout(*args):
        original(*args)
        raise UnknownExecution()
    spot.market = accepted_but_timeout
    with pytest.raises(ValueError, match="unknown"): execute(c, spot, INFO, NOW, enabled=True)
    c.close()
    c = connect(tmp_path / "testnet.sqlite")
    execute(c, spot, INFO, NOW, enabled=True)
    assert len(spot.posts) == 1
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    inventory(c, spot)


def test_order_not_sent_before_due_or_after_stale_window(tmp_path):
    c, spot = ledger(tmp_path)
    signal(c)
    execute(c, spot, INFO, "2020-01-02T05:00:00+00:00", enabled=True)
    assert not spot.posts
    execute(c, spot, INFO, "2020-01-03T02:00:00+00:00", enabled=True)
    assert not spot.posts
    assert c.execute("SELECT status FROM actions").fetchone()[0] == "expired"


def test_disabled_execution_still_does_readonly_reconciliation(tmp_path):
    c, spot = ledger(tmp_path)
    signal(c)
    execute(c, spot, INFO, NOW, enabled=False)
    assert not spot.posts
    report = venue_report(c, spot)
    assert report["quote_asset"] == "USDT" and report["real_money"] is False


def test_exchange_dust_does_not_block_next_buy(tmp_path):
    c, spot = ledger(tmp_path)
    with c: c.execute("UPDATE accounts SET btc='.0009' WHERE id='fixed_0_7'")
    action = signal(c, dust=str(dust_limit(INFO)))
    assert action["side"] == "buy"
    assert c.execute("SELECT btc FROM accounts WHERE id='fixed_0_7'").fetchone()[0] == ".0009"


def test_successful_execution_import_and_no_repost(tmp_path):
    c, spot = ledger(tmp_path)
    signal(c)
    execute(c, spot, INFO, NOW, enabled=True)
    assert len(spot.posts) == 1
    assert spot.posts[0][2] == {"quoteOrderQty": "495.00"}
    execute(c, spot, INFO, NOW, enabled=True)
    assert len(spot.posts) == 1
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    inventory(c, spot)


def test_stale_fill_response_cannot_roll_back(tmp_path):
    c, spot = ledger(tmp_path)
    j = journal(c, signal(c))
    order, trade = example(j, status="PARTIALLY_FILLED")
    full = dict(order, status="FILLED", executedQty="2", cummulativeQuoteQty="200")
    book(c, j, full, [trade, dict(trade, id=2)], NOW)
    with pytest.raises(ValueError, match="Stale"): book(c, j, order, [trade], NOW)
    assert c.execute("SELECT state FROM venue_orders").fetchone()[0] == "terminal"


def test_cancelled_partial_fill_keeps_actual_holdings(tmp_path):
    c, spot = ledger(tmp_path)
    j = journal(c, signal(c))
    order, trade = example(j, status="CANCELED")
    book(c, j, order, [trade], NOW)
    assert c.execute("SELECT status FROM actions").fetchone()[0] == "venue_canceled"
    assert c.execute("SELECT btc FROM accounts WHERE id='fixed_0_7'").fetchone()[0] == "1"


def test_inventory_blocks_unattributed_order(tmp_path):
    c, spot = ledger(tmp_path)
    spot.opens = [{"clientOrderId": "manual"}]
    with pytest.raises(ValueError, match="Unattributed"): inventory(c, spot)


def test_cli_status_never_opens_db(monkeypatch, capsys):
    from live_mvrv import binance_main
    spot = Fake()
    spot.sync_time = lambda: None
    spot.rules = lambda symbol: INFO
    monkeypatch.setattr(binance_main, "Spot", lambda: spot)
    monkeypatch.setattr(binance_main, "connect", lambda *a: pytest.fail("status must not open DB"))
    monkeypatch.setattr(sys, "argv", ["binance_main", "status"])
    binance_main.main()
    assert json.loads(capsys.readouterr().out)["mutation"] is False
