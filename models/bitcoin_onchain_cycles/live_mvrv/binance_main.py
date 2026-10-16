"""Isolated Spot worker; production execution defaults OFF."""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import threading
import time

from . import data
from .binance_spot import Spot, SpotError, balances, dust_limit
from .binance_execution import execute, inventory, reconcile, reserve, venue_report
from .engine import configuration
from .main import checked_cycle
from .notify import deliver
from .storage import connect, get_meta, message, set_meta


def safe_failure(error):
    # External exceptions may contain signed URLs or tokens. Never print them.
    if isinstance(error, (SpotError, data.DataError)):
        return str(error)
    known = {
        "Exchange inventory mismatch: deposit, withdrawal, external trade or Testnet reset; paused, no automatic reallocation": "Inventory mismatch; inspect deposits, manual trades and venue reset",
        "Unattributed open order; automatic execution paused": "Unattributed open order; operator review required",
        "Order outcome unknown/not found; no automatic resubmission. Operator investigation required": "Order UNKNOWN/not found; retain intent, never resend blindly",
        "POST outcome unknown; intent retained. Reconcile by client ID; NEVER blindly resend": "POST outcome UNKNOWN; reconcile retained client ID",
        "Completed order history missing: possible Testnet reset; preserve ledger, pause and investigate": "Completed venue history missing; preserve ledger and investigate",
        "Production order exceeds configured cap or is empty; no order sent": "Order exceeds configured production cap or is empty",
        "Disable third-asset fee discount before production orders; native BTC/USDT fees only": "Disable third-asset fee discount; native BTC/USDT fees only",
    }
    if type(error) is ValueError and str(error) in known:
        return known[str(error)]
    return type(error).__name__


def stage(c, name):
    with c:
        set_meta(c, "check_stage", name)


def tick(c, spot, capital, *, evaluate=True):
    # Preserve Binance's Retry-After deadline across once/run and worker restarts.
    saved = float(get_meta(c, "venue_cooldown_until") or 0)
    spot.cooldown_until = max(getattr(spot, "cooldown_until", 0), saved)
    try:
        return _tick(c, spot, capital, evaluate=evaluate)
    except (ValueError, RuntimeError, OSError) as error:
        label = f"{get_meta(c, 'check_stage', 'preflight')}: {safe_failure(error)}"
        with c:
            set_meta(c, "venue_error", label)
            set_meta(c, "heartbeat", data.utc().isoformat())
            message(c, "venue-error:" + data.utc().date().isoformat() + ":" + get_meta(c, 'check_stage', 'preflight'),
                    f"{spot.environment.upper()} check failed; new orders paused. {label}. No blind order retry.")
        raise
    finally:
        if spot.cooldown_until != saved:
            with c:
                set_meta(c, "venue_cooldown_until", str(spot.cooldown_until))


def _tick(c, spot, capital, *, evaluate=True):
    stage(c, "probe_guard")
    if get_meta(c, "active_testnet_probe"):
        raise ValueError("Testnet acceptance probe active; strategy worker must remain paused")
    now = data.utc()
    when = now.isoformat()
    stage(c, "binance_time")
    spot.sync_time()
    stage(c, "exchange_filters")
    info = spot.rules("BTCUSDT")
    stage(c, "account_binding")
    reserve(c, spot, capital, when)
    # Import exchange fills before constructing a new strategy decision/mark.
    stage(c, "exchange_reconcile")
    reconcile(c, spot, when)
    # Old releases collapsed exchange/notification failures into the data flag.
    legacy_error = get_meta(c, "error") == "Testnet exchange/data check failed; new orders paused"
    evaluate = evaluate or legacy_error
    if evaluate:
        day = (now.date() - timedelta(days=1)).isoformat()
        stage(c, "daily_binance_close")
        price = spot.daily_close("BTCUSDT", day)
        stage(c, "coinmetrics_validation")
        checked_cycle(c, configuration(), capital, "alerts", now=now,
                      mark_price=price, btc_dust=str(dust_limit(info)))
    stage(c, "order_reconcile_execute")
    # A data failure stays blocked until a full successful data validation.
    enabled = spot.execution_enabled and not get_meta(c, "error")
    execute(c, spot, info, data.utc().isoformat(), enabled=enabled)
    with c:
        if get_meta(c, "venue_error"):
            message(c, "venue-recovered:" + data.utc().isoformat(), spot.environment.upper() + " exchange checks recovered; no fabricated fill.")
        set_meta(c, "venue_error", "")
        set_meta(c, "heartbeat", data.utc().isoformat())
    deliver_safely(c)
    stage(c, "complete")
    return venue_report(c, spot)


def deliver_safely(c):
    try:
        deliver(c, data.utc().isoformat())
        with c: set_meta(c, "notification_error", "")
    except (ValueError, RuntimeError, OSError) as error:
        with c: set_meta(c, "notification_error", "Telegram delivery failed: " + type(error).__name__)
        print("Telegram delivery failed; outbox retained. Exchange/data state unchanged.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "once", "run", "report", "backup"])
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE", "/data/binance-testnet.sqlite")))
    parser.add_argument("--capital", default=os.getenv("INITIAL_CAPITAL"))
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    spot = Spot()
    spot.prompt_passphrase = args.action == "status"
    try:
        if args.action == "status":
            # No DB, no capital reservation, no POST, no Telegram.
            spot.sync_time()
            info = spot.rules("BTCUSDT")
            account = spot.account()
            assets = balances(account)
            print(json.dumps(dict(environment=spot.environment, symbol=info["symbol"], can_trade=account.get("canTrade"),
                                  balances={k: {x: str(v) for x, v in assets.get(k, {}).items()} for k in ("BTC", "USDT")},
                                  open_order_count=len(spot.open_orders("BTCUSDT")), mutation=False), indent=2))
            return
        if spot.environment not in {"testnet", "production"}:
            parser.error("Only status supports production_readonly")
        if args.action in {"report", "backup"} and not args.database.exists():
            parser.error("No exchange ledger yet")
        if spot.environment == "production" and args.database.name == "binance-testnet.sqlite":
            parser.error("Set a separate production DATABASE; Testnet path refused")
        if spot.environment == "production" and args.action in {"once", "run"} and not args.capital:
            parser.error("Production requires explicit INITIAL_CAPITAL or --capital; no funding default")
        args.capital = args.capital or "1000"
        c = connect(args.database)
        try:
            if args.action == "backup":
                from .storage import backup
                if not args.destination: parser.error("Backup needs a NEW --destination")
                backup(c, args.destination)
                print("Online exchange ledger backup complete; copy it off-server.")
            elif args.action == "report":
                print(json.dumps(venue_report(c, spot), indent=2, allow_nan=False))
            elif args.action == "once":
                print(json.dumps(tick(c, spot, args.capital), indent=2, allow_nan=False))
            else:
                stopped = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                signal.signal(signal.SIGINT, lambda *_: stopped.set())
                next_evaluation = 0
                while not stopped.is_set():
                    try:
                        now = data.utc()
                        expected = (now.date() - timedelta(days=1)).isoformat()
                        evaluate = now.hour >= 1 and get_meta(c, "last_decision_date") != expected and time.monotonic() >= next_evaluation
                        if evaluate:
                            next_evaluation = time.monotonic() + 3600
                        result = tick(c, spot, args.capital, evaluate=evaluate)
                        if evaluate:
                            print(json.dumps(result, allow_nan=False), flush=True)
                    except (ValueError, RuntimeError, OSError) as error:
                        deliver_safely(c)
                        print(f"{spot.environment.upper()} {get_meta(c, 'check_stage', 'preflight')}: {safe_failure(error)}; new orders paused.", flush=True)
                    stopped.wait(60)
        finally:
            c.close()
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
