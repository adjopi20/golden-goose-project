"""Local Binance public-feed collection and opt-in paper evaluation. No real orders."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime
import json
from pathlib import Path
import time

import pandas as pd
import requests
from websockets.exceptions import ConnectionClosed

from backtest_engine.replay_aggtrades import Config
from live_engine.adapters.binance import BinancePublic
from live_engine.market_data import MarketData
from live_engine.order_manager import PaperOrderManager
from live_engine.state_store import StateStore, encode
from .runtime.evaluator import ACCOUNT_IDS, NotReady, evaluate_completed_bar, hype_candidate_features
from .runtime.observer import SessionObserver
from .runtime.baseline import BAR_MS, NY, clock_ms, evaluate_session


from .runtime.preparation import profile_key, make_profile, quarter_bars, prepare_context, paper_activation_signal


class Worker(SessionObserver):
    def __init__(self, store, manager, *, paper=False, calibration=None, now_ms=None,
                 strict_coverage=False, min_native_reference_sessions=0):
        self.manager, self.paper = manager, paper
        super().__init__(store, calibration=calibration, now_ms=now_ms,
            strict_coverage=strict_coverage,
            min_native_reference_sessions=min_native_reference_sessions,
            activate=self._activate if paper else None)

    def _activate(self, evaluated, ready_ms):
        paper_signal = paper_activation_signal(evaluated["signal"], ready_ms)
        return dict(paper_signal=paper_signal,
            paper_result=self.manager.submit(ACCOUNT_IDS[paper_signal["symbol"]], paper_signal,
                snapshot=evaluated["snapshot"], risk_fraction=evaluated["risk_fraction"],
                selected=evaluated["selected"]))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).parent/"deploy/binance.paper.yaml")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", choices=("warmup","run","status"), required=True)
    parser.add_argument("--paper", action="store_true", help="Enable simulated orders; default collects/evaluates only")
    parser.add_argument("--c1-calibration", type=Path, help="Valid venue-native HYPE calibration; otherwise HYPE waits")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if cfg["mode"] != "paper" or cfg["venue"] != "binance" or cfg["schema_version"] != 1:
        raise ValueError("This worker supports Binance public-data paper mode only")
    symbols = [a["symbol"] for a in cfg["accounts"]]
    if set(symbols) != set(ACCOUNT_IDS) or len(symbols) != 3:
        raise ValueError("Expected the three frozen contract markets")
    calibration = json.loads(args.c1_calibration.read_text()) if args.c1_calibration else None
    if calibration is not None and calibration.get("venue") != "binance":
        raise ValueError("HYPE C1 calibration must be Binance-native")
    with_store = StateStore(args.database, "binance")
    try:
        if args.action != "status":
            with_store.acquire_writer()
        manager = PaperOrderManager(with_store)
        for a in cfg["accounts"]:
            if a["id"] != ACCOUNT_IDS[a["symbol"]]:
                raise ValueError("Account ID differs from contract")
            manager.register(a["id"], a["symbol"], Config(**a["execution"]),
                             allowed_risks=a["allowed_risks"], contract=a["contract"])
        market = MarketData(with_store, manager)
        if args.action == "status":
            feeds = {s:market.state(s) for s in symbols}
            latest = {s:with_store.db.execute("SELECT payload FROM evaluations WHERE symbol=? ORDER BY asof DESC LIMIT 1",(s,)).fetchone() for s in symbols}
            mode = with_store.db.execute("SELECT value FROM metadata WHERE key='execution_mode'").fetchone()
            print(json.dumps(dict(execution_mode=mode[0] if mode else "unknown", accounts=manager.status(), feeds=feeds,
                latest_evaluation={s:json.loads(r[0]) if r else None for s,r in latest.items()},
                feed_age_seconds={s:(int(time.time()*1000)-v["cursor"]["timestamp_ms"])/1000 if v else None for s,v in feeds.items()},
                completed_minutes={s:with_store.db.execute("SELECT COUNT(*) FROM minutes WHERE symbol=?",(s,)).fetchone()[0] for s in symbols}), indent=2))
            return
        api = BinancePublic()
        print(encode(dict(markets=api.validate_markets(symbols), real_orders=False)), flush=True)
        now = api.server_time()
        if abs(now-int(time.time()*1000)) > 5000:
            raise ValueError("Computer clock differs from exchange by >5s; synchronize clock first")
        if args.action == "warmup":
            end = now//60_000*60_000
            for symbol in symbols:
                print(f"{symbol}: preparing 25 days of native minute candles (not raw trades)", flush=True)
                market.seed_minutes(symbol, api.minute_history(symbol, end-25*86_400_000, end))
            print("Warmup complete. Run collector before next 01:00 NY for a complete profile.", flush=True)
            return
        with with_store.transaction():
            with_store.db.execute("INSERT INTO metadata(key,value) VALUES ('execution_mode',?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                  ("simulated" if args.paper else "observation",))
        worker = Worker(with_store, manager, paper=args.paper, calibration=calibration)
        buffers = defaultdict(list)
        last_flush = time.monotonic()

        def flush():
            nonlocal last_flush
            for symbol in symbols:
                if buffers[symbol]:
                    market.consume(symbol, buffers[symbol], account=ACCOUNT_IDS[symbol],
                                   profile_key=profile_key, on_boundary=worker.boundary)
                    buffers[symbol].clear()
            last_flush = time.monotonic()

        delay = 1
        while True:
            try:
                for incoming in api.stream(symbols):
                    if incoming is not None:
                        symbol = incoming["symbol"]
                        previous = buffers[symbol][-1] if buffers[symbol] else (market.state(symbol) or {}).get("cursor")
                        if previous:
                            if incoming["agg_trade_id"] < previous["agg_trade_id"]:
                                continue
                            if incoming["agg_trade_id"] == previous["agg_trade_id"]:
                                if incoming != previous:
                                    raise ValueError("Conflicting stream duplicate")
                                continue
                            for repaired in api.repair(symbol, previous, incoming):
                                buffers[symbol].append(repaired)
                                if len(buffers[symbol]) >= 1000:
                                    flush()
                        buffers[symbol].append(incoming)
                    if time.monotonic()-last_flush >= 1 or sum(map(len,buffers.values())) >= 1000:
                        flush()
                        delay = 1
            except (ConnectionClosed, OSError, requests.RequestException) as exc:
                flush()
                print(encode(dict(state="FEED_DISCONNECTED", error=str(exc), retry_seconds=delay)), flush=True)
                time.sleep(delay)
                delay = min(30, delay*2)
            except KeyboardInterrupt:
                flush()
                print("Stopped; committed feed checkpoint and paper state.", flush=True)
                break
    finally:
        with_store.close()


if __name__ == "__main__":
    try:
        main()
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise SystemExit(
            f"Binance connection failed after bounded retries ({type(exc).__name__}). "
            "Could not reach https://fapi.binance.com. Check internet/firewall/proxy or try another network. "
            "No real orders were sent. Keep the database; rerun the failed command after connectivity recovers."
        ) from None
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "unknown"
        raise SystemExit(f"Binance HTTP {code}: request refused/failed. Do not bypass access restrictions. "
                         "Check the exchange response and retry only when access/rate limits permit.") from None
