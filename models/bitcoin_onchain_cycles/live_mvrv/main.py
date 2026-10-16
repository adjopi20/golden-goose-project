"""Resident daily worker and explicit operator CLI. No real exchange orders."""
import argparse
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time

import pandas as pd

from . import data
from .engine import apply_day, configuration, fill, initialize
from .notify import deliver, discover, test_notification
from .report import report
from .storage import archive, backup, connect, get_meta, message, set_meta


def cycle(c, config, capital, mode, now=None, input_path=None, mark_price=None, btc_dust="1e-12"):
    now = data.utc(now)
    when = now.isoformat()
    with c:
        initialize(c, config, capital, mode, when)
        set_meta(c, "heartbeat", when)
    expected = (now.date() - timedelta(days=1)).isoformat()
    last = get_meta(c, "last_data_date")
    start = (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d") if last else data.ORIGIN
    incoming = data.normalize(pd.read_csv(input_path)) if input_path else data.fetch(start, expected)
    # Archive even a subsequently rejected revision before signal transaction.
    with c:
        archive(c, incoming, when)
    with c:
        c.execute("BEGIN IMMEDIATE")
        raw = data.merged_history(c, incoming, when)
        daily = data.prepare(raw, config)
        latest = daily.iloc[-1]
        if not data.fresh(latest["date"], now):
            raise data.DataError(f"Expected complete UTC date {expected}; newest is {latest['date']}")
        if daily.iloc[-1]["percentile_4y_p5_p95_entry"] != daily.iloc[-1]["percentile_4y_p5_p95_entry"]:
            raise data.DataError("Insufficient full percentile warmup")
        if mark_price is not None:
            latest = latest.copy()
            latest["price"] = mark_price
        changed = apply_day(c, latest, config, when, mode, btc_dust=btc_dust)
        set_meta(c, "last_data_date", latest["date"])
        set_meta(c, "last_success_at", when)
        previous_error = get_meta(c, "error")
        set_meta(c, "error", "")
        if previous_error:
            message(c, "recovered:" + when, "BTC MVRV monitor recovered: fresh validated data accepted")
    return changed


def checked_cycle(c, config, capital, mode, **kwargs):
    when = data.utc(kwargs.get("now")).isoformat()
    try:
        return cycle(c, config, capital, mode, **kwargs)
    except (ValueError, RuntimeError, OSError) as error:
        # Do not include external credential-bearing URLs or raw responses.
        label = str(error) if isinstance(error, (data.DataError, ValueError)) else type(error).__name__
        with c:
            set_meta(c, "error", label)
            set_meta(c, "heartbeat", when)
            key = hashlib.sha256((when[:10] + label).encode()).hexdigest()
            message(c, "error:" + key, "BTC MVRV monitor paused: " + label)
        raise


def health(c, now=None):
    now = data.utc(now)
    beat = get_meta(c, "heartbeat")
    latest = get_meta(c, "last_data_date")
    return bool(beat and 0 <= (now - data.utc(beat)).total_seconds() < 300
                and latest and 0 <= (now.date() - pd.Timestamp(latest).date()).days <= 2
                and not get_meta(c, "error") and not get_meta(c, "venue_error")
                and not get_meta(c, "notification_error"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "once", "report", "health", "confirm-fill", "cancel-action", "backup", "telegram-discover", "telegram-test"])
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE", "/data/mvrv.sqlite")))
    parser.add_argument("--capital", default=os.getenv("INITIAL_CAPITAL", "1000"))
    parser.add_argument("--mode", choices=["paper", "alerts"], default=os.getenv("MODE", "paper"))
    parser.add_argument("--input", type=Path, help="Offline full raw CSV; still subject to live freshness checks")
    parser.add_argument("--action-id")
    parser.add_argument("--fill-id")
    parser.add_argument("--units")
    parser.add_argument("--price")
    parser.add_argument("--fee-usd")
    parser.add_argument("--filled-at", help="Actual operator fill timestamp including timezone")
    parser.add_argument("--partial", action="store_true", help="Keep action pending for additional fills")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    # Setup/test commands never open or initialize a trading database.
    if args.action in {"telegram-discover", "telegram-test"}:
        try:
            result = discover() if args.action == "telegram-discover" else test_notification(data.utc().isoformat())
        except (ValueError, RuntimeError) as error:
            parser.error(str(error))
        print(json.dumps(result, indent=2, allow_nan=False))
        return
    if args.action == "health" and not args.database.exists():
        raise SystemExit(1)
    config = configuration()
    c = connect(args.database)
    try:
        if args.action == "health":
            raise SystemExit(0 if health(c) else 1)
        elif args.action == "report":
            print(json.dumps(report(c), indent=2, allow_nan=False))
        elif args.action == "backup":
            if args.destination is None:
                parser.error("backup requires --destination")
            backup(c, args.destination)
            print("Database backup complete")
        elif args.action in {"confirm-fill", "cancel-action"}:
            if get_meta(c, "mode") != "alerts":
                parser.error("Operator commands require an initialized alerts-mode account")
            if not args.action_id:
                parser.error("--action-id required")
            now = data.utc().isoformat()
            with c:
                if args.action == "confirm-fill":
                    if any(x is None for x in (args.fill_id, args.units, args.price, args.fee_usd, args.filled_at)):
                        parser.error("confirm-fill requires fill-id, units, price, fee-usd and filled-at")
                    fill(c, args.action_id, args.fill_id, args.units, args.price, args.fee_usd, now, args.filled_at, not args.partial)
                else:
                    changed = c.execute("UPDATE actions SET status='cancelled' WHERE id=? AND status='pending'", (args.action_id,)).rowcount
                    if not changed:
                        raise ValueError("No matching pending action")
                    message(c, "cancel:" + args.action_id, "Operator cancelled proposal " + args.action_id)
            print(json.dumps(report(c), indent=2, allow_nan=False))
        elif args.action == "once":
            checked_cycle(c, config, args.capital, args.mode, input_path=args.input)
            deliver(c, data.utc().isoformat())
            print(json.dumps(report(c), indent=2, allow_nan=False))
        else:
            if args.input:
                parser.error("--input is only for once; live run fetches its own source")
            # Fail before looping if account mode/capital/config was changed.
            with c:
                initialize(c, config, args.capital, args.mode, data.utc().isoformat())
            stopped = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
            attempted = -float("inf")
            while not stopped.is_set():
                now = data.utc()
                with c:
                    set_meta(c, "heartbeat", now.isoformat())
                expected = (now.date() - timedelta(days=1)).isoformat()
                if now.hour >= 1 and get_meta(c, "last_decision_date") != expected and time.monotonic() - attempted >= 3600:
                    attempted = time.monotonic()
                    try:
                        checked_cycle(c, config, args.capital, args.mode, now=now)
                        print(json.dumps(report(c), allow_nan=False), flush=True)
                    except (ValueError, RuntimeError, OSError):
                        print("MVRV data check failed; see persisted status, retry in one hour", flush=True)
                try:
                    deliver(c, now.isoformat())
                except (ValueError, RuntimeError):
                    print("Telegram delivery unavailable; outbox retained", flush=True)
                stopped.wait(60)
    finally:
        c.close()


if __name__ == "__main__":
    main()
