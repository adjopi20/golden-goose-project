"""Explicit virtual Testnet acceptance, NOT a strategy or historical signal."""
import argparse
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import uuid

from .binance_spot import Spot, UnknownExecution, balances, dec, dust_limit, order_size
from .binance_execution import execute, inventory, reconcile, require_bound, reserve, venue_report
from .data import utc
from .storage import backup, connect, get_meta, message, set_meta

PURPOSE = "virtual-execution-acceptance-v1"
TABLES = ("accounts", "actions", "decisions", "fills", "marks", "raw", "vintages",
          "venue_orders", "venue_trades", "venue_basis")


def frozen(c):
    result = {}
    for table in TABLES:
        digest = hashlib.sha256()
        for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            digest.update(json.dumps(dict(row), sort_keys=True).encode())
            digest.update(b"\n")
        result[table] = digest.hexdigest()
    result["contract"] = get_meta(c, "contract")
    result["last_decision_date"] = get_meta(c, "last_decision_date")
    return result


def totals(spot):
    values = balances(spot.account())
    return {asset: values.get(asset, {}).get("free", Decimal(0)) + values.get(asset, {}).get("locked", Decimal(0))
            for asset in ("BTC", "USDT")}


def guard(c, main, spot):
    if spot.environment != "testnet" or get_meta(c, "probe_purpose") != PURPOSE:
        raise ValueError("Only a dedicated virtual acceptance ledger is allowed")
    require_bound(c, spot)
    require_bound(main, spot)
    if frozen(main) != json.loads(get_meta(c, "strategy_frozen")):
        raise ValueError("Strategy records changed during probe; halt, do not adjust surplus")
    pid = get_meta(c, "probe_id")
    if get_meta(main, "active_testnet_probe") != pid and not get_meta(main, "probe_adjustment:" + pid):
        raise ValueError("Strategy is not bound to this active probe")


def init_probe(c, main, spot, capital, when, backup_path):
    if spot.environment != "testnet" or dec(capital) <= 0 or dec(capital) > 50:
        raise ValueError("Probe capital must be positive and at most 50 virtual USDT")
    if get_meta(c, "contract") or get_meta(main, "active_testnet_probe"):
        raise ValueError("Use a NEW probe ledger; do not erase or reuse an active one")
    require_bound(main, spot)
    if get_meta(main, "error"):
        raise ValueError("Strategy error must be investigated before probe")
    if main.execute("SELECT 1 FROM actions WHERE status='pending'").fetchone() or main.execute(
            "SELECT 1 FROM venue_orders WHERE state!='terminal'").fetchone():
        raise ValueError("Outstanding strategy intent/order; no probe allowed")
    inventory(main, spot)
    extra = json.loads(get_meta(main, "venue_unallocated"))
    if dec(extra["USDT"]) < dec(capital):
        raise ValueError("Insufficient unallocated virtual USDT; no strategy capital permitted")
    before = totals(spot)
    snapshot = frozen(main)
    backup(main, backup_path)
    reserve(c, spot, str(capital), when)
    pid = uuid.uuid4().hex
    with c:
        set_meta(c, "probe_purpose", PURPOSE)
        set_meta(c, "probe_id", pid)
        set_meta(c, "strategy_frozen", json.dumps(snapshot, sort_keys=True))
        set_meta(c, "strategy_surplus_before", json.dumps(extra, sort_keys=True))
        set_meta(c, "venue_before", json.dumps({k: str(v) for k, v in before.items()}, sort_keys=True))
        set_meta(c, "strategy_backup", str(backup_path))
    with main:
        if frozen(main) != snapshot or get_meta(main, "active_testnet_probe"):
            raise ValueError("Concurrent strategy change; probe not armed")
        set_meta(main, "active_testnet_probe", pid)
    return pid


def phase(c, main, spot, side, when):
    guard(c, main, spot)
    if get_meta(c, "restored_at") or get_meta(main, "probe_adjustment:" + get_meta(c, "probe_id")):
        raise ValueError("Completed probe cannot place new orders")
    if not spot.execution_enabled:
        raise ValueError("Explicit Testnet execution switch required")
    reconcile(c, spot, when)
    accounts = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    # Repeated commands never refresh the due time or create another intent.
    existing = c.execute("SELECT COUNT(*) FROM actions WHERE side=?", (side,)).fetchone()[0]
    if not existing:
        if side == "buy" and c.execute("SELECT 1 FROM actions").fetchone():
            raise ValueError("Buy phase already passed")
        if side == "sell":
            completed = c.execute("SELECT COUNT(*) FROM actions WHERE side='buy' AND status='filled'").fetchone()[0]
            if completed != len(accounts):
                raise ValueError("All probe buys must be reconciled as filled before sell")
        info = spot.rules("BTCUSDT")
        bid, ask = spot.quote("BTCUSDT")
        for account in accounts:
            order_size(info, side, account["cash"], account["btc"], bid, ask)
        with c:
            for account in accounts:
                aid = "pr" + hashlib.sha256((get_meta(c, "probe_id") + account["id"] + side).encode()).hexdigest()[:22]
                c.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?)", (
                    aid, account["id"], "PROBE_NOT_MVRV", when, when, side, "pending"))
    execute(c, spot, spot.rules("BTCUSDT"), when, enabled=True)
    if c.execute("SELECT COUNT(*) FROM actions WHERE side=? AND status='filled'", (side,)).fetchone()[0] != len(accounts):
        raise ValueError("Probe phase incomplete; preserve state and reconcile, never blindly resend")


def verify(c, main, spot, when):
    guard(c, main, spot)
    reconcile(c, spot, when)
    if c.execute("SELECT 1 FROM venue_orders WHERE state!='terminal'").fetchone():
        raise ValueError("Probe has an unresolved order")
    before = json.loads(get_meta(c, "venue_before"))
    actual = totals(spot)
    with localcontext() as ctx:
        ctx.prec = 40
        cash = sum((dec(r[0]) for r in c.execute("SELECT cash FROM accounts")), Decimal(0))
        btc = sum((dec(r[0]) for r in c.execute("SELECT btc FROM accounts")), Decimal(0))
        expected_delta = {"USDT": cash - dec(get_meta(c, "initial_capital")), "BTC": btc}
        delta = {asset: actual[asset] - dec(before[asset]) for asset in actual}
        if delta != expected_delta:
            raise ValueError("Venue delta differs from native-fee ledger; no surplus adjustment")
    result = venue_report(c, spot)
    result.update(purpose=PURPOSE, strategy_performance=False, strategy_records_unchanged=True,
                  probe_id=get_meta(c, "probe_id"), restored_at=get_meta(c, "restored_at"),
                  venue_delta={k: str(v) for k, v in delta.items()})
    return result


def restore_surplus(c, main, spot, when):
    result = verify(c, main, spot, when)
    if c.execute("SELECT COUNT(*) FROM actions WHERE status='filled'").fetchone()[0] != 4 or c.execute(
            "SELECT COUNT(*) FROM actions").fetchone()[0] != 4:
        raise ValueError("Need exactly two completed buys and two completed sells")
    info = spot.rules("BTCUSDT")
    if any(dec(r[0]) > dust_limit(info) for r in c.execute("SELECT btc FROM accounts")):
        raise ValueError("Tradable probe BTC remains; do not release strategy")
    pid = get_meta(c, "probe_id")
    audit_key = "probe_adjustment:" + pid
    old = json.loads(get_meta(c, "strategy_surplus_before"))
    with localcontext() as ctx:
        ctx.prec = 40
        new = {asset: str(dec(old[asset]) + Decimal(result["venue_delta"][asset])) for asset in old}
        if any(Decimal(value) < 0 for value in new.values()):
            raise ValueError("Probe would consume strategy capital")
    record = dict(purpose=PURPOSE, real_money=False, probe_id=pid, when=when,
                  old=old, new=new, delta=result["venue_delta"], strategy_records_unchanged=True)
    with main:
        main.execute("BEGIN IMMEDIATE")
        previous = get_meta(main, audit_key)
        if previous:
            if json.loads(previous)["new"] != new or json.loads(get_meta(main, "venue_unallocated")) != new:
                raise ValueError("Prior surplus adjustment differs; halt")
        else:
            if json.loads(get_meta(main, "venue_unallocated")) != old:
                raise ValueError("Surplus changed independently; no overwrite")
            set_meta(main, "venue_unallocated", json.dumps(new, sort_keys=True))
            set_meta(main, audit_key, json.dumps(record, sort_keys=True))
            set_meta(main, "active_testnet_probe", "")
            message(main, audit_key, "TESTNET acceptance probe completed; surplus adjusted from actual fills. NOT MVRV performance; strategy capital unchanged.")
        inventory(main, spot)
        if frozen(main) != json.loads(get_meta(c, "strategy_frozen")):
            raise ValueError("Strategy records changed; rollback surplus adjustment")
    with c:
        set_meta(c, "restored_at", json.loads(get_meta(main, audit_key))["when"])
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "buy", "verify", "sell", "restore", "report", "backup"])
    parser.add_argument("--database", type=Path, default=Path("/data/probe.sqlite"))
    parser.add_argument("--strategy-database", type=Path, default=Path("/strategy/binance-testnet.sqlite"))
    parser.add_argument("--capital", default="50")
    parser.add_argument("--confirm-virtual-testnet", action="store_true")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--simulate-lost-buy-response", action="store_true",
                        help="Probe only: discard the first accepted buy response to test recovery")
    args = parser.parse_args()
    if not args.confirm_virtual_testnet:
        parser.error("Explicit --confirm-virtual-testnet required; no production probe exists")
    if not args.strategy_database.exists() or (args.action != "init" and not args.database.exists()):
        parser.error("Existing strategy/probe DB required; never create a replacement ledger")
    spot = Spot()
    if spot.environment != "testnet":
        parser.error("Probe is virtual Testnet only")
    if args.simulate_lost_buy_response:
        if args.action != "buy": parser.error("Lost-response simulation is buy-only")
        actual_market = spot.market
        def lost_response(*arguments):
            actual_market(*arguments)
            raise UnknownExecution()
        spot.market = lost_response
    c, strategy = connect(args.database), connect(args.strategy_database)
    try:
        spot.cooldown_until = max(float(get_meta(c, "venue_cooldown_until") or 0),
                                  float(get_meta(strategy, "venue_cooldown_until") or 0))
        if args.action == "backup":
            guard(c, strategy, spot)
            if not args.destination: parser.error("Backup needs a new --destination")
            backup(c, args.destination)
            result = {"backup": str(args.destination), "purpose": PURPOSE}
        elif args.action == "report":
            guard(c, strategy, spot)
            result = venue_report(c, spot)
            result.update(purpose=PURPOSE, strategy_performance=False, restored_at=get_meta(c, "restored_at"))
        else:
            spot.sync_time()
            when = utc().isoformat()
            if args.action == "init":
                pid = init_probe(c, strategy, spot, args.capital, when,
                                 args.strategy_database.with_name("before_probe_" + uuid.uuid4().hex + ".sqlite"))
                result = {"probe_id": pid, "virtual_capital": args.capital, "purpose": PURPOSE}
            elif args.action in {"buy", "sell"}:
                phase(c, strategy, spot, args.action, when)
                result = verify(c, strategy, spot, utc().isoformat())
            elif args.action == "verify": result = verify(c, strategy, spot, when)
            else: result = restore_surplus(c, strategy, spot, when)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    finally:
        with c: set_meta(c, "venue_cooldown_until", str(spot.cooldown_until))
        c.close()
        strategy.close()


if __name__ == "__main__":
    main()
