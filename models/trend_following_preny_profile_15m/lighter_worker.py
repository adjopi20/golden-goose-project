"""Venue-native Lighter feed with opt-in simulated fills. No real orders."""
import argparse
import json
from collections import defaultdict
from pathlib import Path
import time

import requests
from websockets.exceptions import ConnectionClosed

from backtest_engine.replay_aggtrades import Config
from live_engine.adapters.lighter_public import INTERNAL_SYMBOLS, LighterPublic
from live_engine.market_data import MarketData
from live_engine.order_manager import PaperOrderManager
from live_engine.state_store import StateStore, encode
from .paper_bridge import ACCOUNT_IDS
from .paper_worker import Worker, profile_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).parent/"deploy/lighter.paper.yaml")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", choices=("run", "status"), required=True)
    parser.add_argument("--paper", action="store_true", help="Enable simulated fills; default collects only")
    parser.add_argument("--c1-calibration", type=Path, help="Valid Lighter-native HYPE C1 calibration")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if cfg.get("mode") != "paper" or cfg.get("venue") != "lighter" or cfg.get("schema_version") != 1:
        raise ValueError("Expected schema 1 Lighter paper ledger config")
    symbols = [account["symbol"] for account in cfg["accounts"]]
    if set(symbols) != set(INTERNAL_SYMBOLS) or len(symbols) != 3:
        raise ValueError("Expected ETH, BNB and HYPE venue-native markets")
    calibration = json.loads(args.c1_calibration.read_text()) if args.c1_calibration else None
    if calibration is not None and calibration.get("venue") != "lighter":
        raise ValueError("HYPE C1 calibration must be Lighter-native")
    store = StateStore(args.database, "lighter")
    try:
        if args.action == "run":
            store.acquire_writer()
        manager = PaperOrderManager(store)
        for account in cfg["accounts"]:
            if account["id"] != ACCOUNT_IDS[account["symbol"]]:
                raise ValueError("Account ID differs from frozen model contract")
            manager.register(account["id"], account["symbol"], Config(**account["execution"]),
                             allowed_risks=account["allowed_risks"], contract=account["contract"])
        market = MarketData(store, manager)
        if args.action == "status":
            feeds = {symbol:market.state(symbol) for symbol in symbols}
            latest = {symbol:store.db.execute(
                "SELECT payload FROM evaluations WHERE symbol=? ORDER BY asof DESC LIMIT 1", (symbol,)).fetchone()
                for symbol in symbols}
            mode = store.db.execute("SELECT value FROM metadata WHERE key='execution_mode'").fetchone()
            print(json.dumps({"venue":"lighter", "execution_mode":mode[0] if mode else "unknown", "accounts":manager.status(),
                "feed_age_seconds":{symbol:(int(time.time()*1000)-state["cursor"]["timestamp_ms"])/1000
                                    if state else None for symbol,state in feeds.items()},
                "latest_evaluation":{symbol:json.loads(row[0]) if row else None for symbol,row in latest.items()},
                "completed_minutes":{symbol:store.db.execute(
                    "SELECT COUNT(*) FROM minutes WHERE symbol=?", (symbol,)).fetchone()[0]
                    for symbol in symbols}}, indent=2))
            return
        api = LighterPublic()
        print(encode({"markets":api.validate_markets(), "paper":args.paper, "real_orders":False}), flush=True)
        with store.transaction():
            store.db.execute("INSERT INTO metadata(key,value) VALUES ('execution_mode',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             ("simulated" if args.paper else "observation",))
        worker = Worker(store, manager, paper=args.paper, calibration=calibration, strict_coverage=True,
                        min_native_reference_sessions=10)
        buffers = defaultdict(list)
        last_flush = time.monotonic()

        def flush():
            nonlocal last_flush
            for symbol in symbols:
                if buffers[symbol]:
                    market.consume(symbol, buffers[symbol], account=ACCOUNT_IDS[symbol],
                                   profile_key=profile_key, on_boundary=worker.boundary,
                                   consecutive_ids=False)
                    buffers[symbol].clear()
            last_flush = time.monotonic()

        delay = 1
        while True:
            try:
                cursors = {symbol:(market.state(symbol) or {}).get("cursor", {}).get("agg_trade_id")
                           for symbol in symbols}
                for symbol, rows, reset in api.stream(cursors):
                    if reset:
                        flush()
                        market.reset_coverage(symbol)
                        print(encode({"state":"FEED_GAP", "symbol":symbol,
                                      "action":"reset_coverage_and_wait_for_new_full_profile"}), flush=True)
                    buffers[symbol].extend(rows)
                    if time.monotonic()-last_flush >= 1 or sum(map(len,buffers.values())) >= 1000:
                        flush()
                        delay = 1
            except (ConnectionClosed, TimeoutError, OSError, requests.RequestException) as exc:
                flush()
                print(encode({"state":"FEED_DISCONNECTED", "error":str(exc), "retry_seconds":delay}), flush=True)
                time.sleep(delay)
                delay = min(30, delay*2)
            except KeyboardInterrupt:
                flush()
                print("Stopped; committed Lighter feed checkpoint.", flush=True)
                return
    finally:
        store.close()


if __name__ == "__main__":
    main()
