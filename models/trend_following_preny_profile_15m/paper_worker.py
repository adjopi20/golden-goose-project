"""Local Binance public-feed collection and opt-in paper evaluation. No real orders."""
import argparse
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import statistics
import time

import pandas as pd
import requests
from websockets.exceptions import ConnectionClosed

from backtest_engine.replay_aggtrades import Config
from live_engine.adapters.binance import BinancePublic
from live_engine.market_data import MarketData
from live_engine.order_manager import PaperOrderManager
from live_engine.state_store import StateStore, encode
from .audit_opportunity import hourly_indicators, trend_indicators_15m, trend_snapshot_15m
from .paper_bridge import ACCOUNT_IDS, NotReady, evaluate_completed_bar
from .strategy import BAR_MS, NY, clock_ms, evaluate_session


def profile_key(stamp):
    local = datetime.fromtimestamp(stamp/1000, NY)
    return str(local.date()) if 1 <= local.hour < 9 else None


def make_profile(day, prices):
    """Same equal-width 50-bin / contiguous 70% algorithm as prepare_aggtrades."""
    if not prices:
        raise NotReady("No native profile trades")
    low, high = min(prices), max(prices)
    weights, width = [0.]*50, (high-low)/50
    for price, qty in sorted(prices.items()):
        weights[min(49, int((price-low)/width)) if width else 0] += qty
    poc = max(range(50), key=lambda i:weights[i])
    left = right = poc
    held, target = weights[poc], sum(weights)*.70
    while held < target and (left > 0 or right < 49):
        if left == 0 or (right < 49 and weights[right+1] > weights[left-1]):
            right += 1
            held += weights[right]
        else:
            left -= 1
            held += weights[left]
    return dict(session_day=str(day), profile_window="pre_ny",
                profile_start_timestamp_ms=clock_ms(day, 1), profile_end_timestamp_ms=clock_ms(day, 9),
                val=low+left*width, poc=low+(poc+.5)*width if width else low,
                vah=low+(right+1)*width if width else high, profile_trade_volume=sum(weights),
                profile_bins=50, value_fraction=.70)


def quarter_bars(minutes):
    indexed = minutes.set_index(pd.to_datetime(minutes.timestamp_ms, unit="ms", utc=True))
    q = indexed.resample("15min").agg(open=("open","first"), high=("high","max"),
        low=("low","min"), close=("close","last"), volume=("volume","sum"), delta=("delta","sum"))
    output = []
    for stamp, r in q.dropna(subset=["close"]).iterrows():
        opening = int(stamp.timestamp()*1000)
        output.append(dict(open_timestamp_ms=opening, close_timestamp_ms=opening+BAR_MS,
            open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close),
            buy_volume=max(0., float((r.volume+r.delta)/2)),
            sell_volume=max(0., float((r.volume-r.delta)/2))))
    return output


def prepare_context(minutes, day, asof):
    """Reuse research indicator formulas; only completed input is accepted."""
    if minutes.empty or int(minutes.timestamp_ms.max())+60_000 > asof:
        raise NotReady("Missing or future minute history")
    all_bars = quarter_bars(minutes)
    start = clock_ms(day, 9)
    bars = [b for b in all_bars if start <= b["open_timestamp_ms"] < asof]
    history = defaultdict(lambda:deque(maxlen=20))
    for b in all_bars:
        local = datetime.fromtimestamp(b["open_timestamp_ms"]/1000, NY)
        if local.date() < day and 9 <= local.hour < 12:
            history[(local.hour-9)*4+local.minute//15].append(dict(
                volume=b["buy_volume"]+b["sell_volume"], buy_volume=b["buy_volume"],
                sell_volume=b["sell_volume"], body=abs(b["close"]-b["open"])))
    refs = {}
    for slot in range(12):
        past = history[slot]
        refs[slot] = dict(prior_sessions=len(past))
        if len(past) >= 10:
            refs[slot].update({k:statistics.median(x[k] for x in past)
                               for k in ("volume","buy_volume","sell_volume","body")})
    hours = hourly_indicators(minutes)
    atr_before = {}
    for b in bars:
        stamp = b["open_timestamp_ms"]
        past = hours.loc[(hours.end_ms <= stamp) & (hours.end_ms > stamp-3_600_000)]
        if not past.empty and math.isfinite(float(past.iloc[-1].atr14)) and past.iloc[-1].atr14 > 0:
            atr_before[stamp] = float(past.iloc[-1].atr14)
    trend = trend_snapshot_15m(trend_indicators_15m(minutes), asof, "long")
    # Direction-independent fields drive sizing; actual alignment is set below.
    return dict(bars=bars, references=dict(through_session_day=str(day-timedelta(days=1)), slots=refs),
                atr_before=atr_before, trend=trend)


class Worker:
    def __init__(self, store, manager, *, paper=False, calibration=None, now_ms=None,
                 strict_coverage=False, min_native_reference_sessions=0):
        self.store, self.manager = store, manager
        self.paper, self.calibration = paper, calibration
        self.now_ms = now_ms or (lambda:int(time.time()*1000))
        self.strict_coverage = strict_coverage
        self.min_native_reference_sessions = min_native_reference_sessions

    def boundary(self, symbol, asof, state):
        local = datetime.fromtimestamp(asof/1000, NY)
        day = local.date()
        start, cutoff = clock_ms(day, 9), clock_ms(day, 12)
        if not start <= asof <= cutoff:
            return
        db = self.store.db
        if db.execute("SELECT 1 FROM evaluations WHERE symbol=? AND asof=?", (symbol,asof)).fetchone():
            return
        result = dict(state="NOT_READY", session_day=str(day), as_of_ms=asof, candidate=False)
        try:
            if self.strict_coverage and state["coverage_start_ms"] > clock_ms(day,1):
                raise NotReady("Collector did not cover full 01:00-09:00 profile; wait for next session")
            if asof > start and self.min_native_reference_sessions:
                prior_minutes = db.execute(
                    "SELECT timestamp_ms FROM minutes WHERE symbol=? AND source='aggregate_trades' AND timestamp_ms>=? AND timestamp_ms<?",
                    (symbol, asof-25*86_400_000, start)).fetchall()
                counts = Counter()
                for row in prior_minutes:
                    local_minute = datetime.fromtimestamp(row[0]/1000, NY)
                    if local_minute.date() < day and 9 <= local_minute.hour < 12:
                        counts[local_minute.date()] += 1
                if sum(count >= 60 for count in counts.values()) < self.min_native_reference_sessions:
                    raise NotReady("Need earlier native 09:00-12:00 sessions before relative order-flow evaluation")
            stored = db.execute("SELECT payload FROM profiles WHERE symbol=? AND session=?", (symbol,str(day))).fetchone()
            if stored:
                profile = json.loads(stored[0])
            else:
                if state["coverage_start_ms"] > clock_ms(day,1):
                    raise NotReady("Collector did not cover full 01:00-09:00 profile; wait for next session")
                prices = {r[0]:r[1] for r in db.execute(
                    "SELECT price,quantity FROM profile_prices WHERE symbol=? AND session=? ORDER BY price", (symbol,str(day)))}
                profile = make_profile(day, prices)
                db.execute("INSERT INTO profiles VALUES (?,?,?)", (symbol,str(day),encode(profile)))
                # Frozen profiles remain; old per-price buckets are no longer required.
                db.execute("DELETE FROM profile_prices WHERE symbol=? AND session<?", (symbol,str(day)))
            if asof == start:
                result.update(state="PROFILE_READY", profile=profile)
            else:
                prior = db.execute("SELECT payload FROM evaluations WHERE symbol=? AND asof>=? AND asof<?",
                                   (symbol,start,asof)).fetchall()
                if any(json.loads(r[0]).get("candidate") for r in prior):
                    result["state"] = "SESSION_LOCKED"
                else:
                    rows = [json.loads(r[0]) for r in db.execute(
                        "SELECT payload FROM minutes WHERE symbol=? AND timestamp_ms<? ORDER BY timestamp_ms", (symbol,asof))]
                    if not rows:
                        raise NotReady("No completed history")
                    context = prepare_context(pd.DataFrame(rows), day, asof)
                    signals, _ = evaluate_session(profile, context["bars"], symbol=symbol)
                    result["candidate"] = bool(signals)
                    if signals:
                        signal = signals[0]
                        if signal["feature_as_of_ms"] != asof:
                            raise NotReady("First candidate was missed; no replacement entry")
                        # Backfill restores state, never creates retrospective fills.
                        if self.now_ms()-asof > 300_000 or self.now_ms()-state["next_event_ms"] > 5000:
                            raise NotReady("Historical candidate: observe only, no retrospective entry")
                        trend = context["trend"]
                        structure = trend.get("ma_structure")
                        if structure in ("bullish_stack", "bearish_stack"):
                            trend["trade_alignment"] = "aligned" if (structure == "bullish_stack") == (signal["direction"] == "long") else "opposed"
                    evaluated = evaluate_completed_bar(symbol=symbol, profile=profile, as_of_ms=asof,
                        c1_calibration=self.calibration, **context)
                    result.update(evaluated)
                    if evaluated["signal"] is not None:
                        if self.paper:
                            result["paper_result"] = self.manager.submit(ACCOUNT_IDS[symbol], evaluated["signal"],
                                snapshot=evaluated["snapshot"], risk_fraction=evaluated["risk_fraction"], selected=evaluated["selected"])
                        else:
                            result["paper_result"] = "collection_only_no_order"
        except NotReady as exc:
            result.update(state="NOT_READY", reason=str(exc))
        db.execute("INSERT INTO evaluations VALUES (?,?,?)", (symbol,asof,encode(result)))
        print(encode(dict(symbol=symbol, session=str(day), asof=asof, state=result["state"],
                          reason=result.get("reason"), paper=self.paper)), flush=True)


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
            print(json.dumps(dict(accounts=manager.status(), feeds=feeds,
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
