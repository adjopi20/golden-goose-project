"""Public-feed paper worker. No credentials, private endpoints or real orders."""
import argparse
from datetime import datetime, time as day_time, timedelta
import hashlib
import inspect
import json
import math
from pathlib import Path
import signal
import sqlite3
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from indicator.volume_profile import build_basic_volume_profile
from live_engine.adapters.binance import BinancePublic, normalize, UnrecoverableGap
from live_engine.order_manager import PaperOrderManager, save_state
from live_engine.state_store import StateStore, encode
from trading_core.paper_execution import Config

ACCOUNT = "mr-ltcusdc-intersection-r025-v1"
SYMBOL = "LTCUSDC"
NY = ZoneInfo("America/New_York")
CONTRACT = dict(version=ACCOUNT, mode="paper", venue="binance_usdm", symbol=SYMBOL,
                initial_equity=10000, risk_fraction=.025, fee_bps_per_side=4,
                max_notional_equity=5, bins=50, value_area_pct=.7,
                ny_open="09:30", location="strictly_outside_previous_value_area",
                distance_pct_min=1, target="frozen_previous_poc", reward_risk=1,
                max_candidates_per_session=1, execution="next_public_print",
                fresh_entry_max_age_ms=5000, max_entry_delay_ms=300000)


def session_bounds(ts):
    now = datetime.fromtimestamp(ts / 1000, NY)
    day = now.date() - timedelta(days=now.time() < day_time(9, 30))
    times = [int(datetime.combine(day + timedelta(days=i), day_time(9, 30), NY)
                 .timestamp() * 1000) for i in (-1, 0, 1)]
    return str(day), *times


class PocPaperManager(PaperOrderManager):
    """Resolve a contingent symmetric bracket, then use the unchanged shared engine."""
    def tick(self, account, tick, *, allow_entry=True):
        with self.store.transaction():
            row, p, cfg = self._load(account)
            cursor = json.loads(row["cursor"]) if row["cursor"] else None
            new = cursor is None or tick["agg_trade_id"] > cursor["agg_trade_id"]
            if new and p.signals and p.position is None:
                candidate = p.signals[p.next_signal]
                reason = None
                eligible = tick["timestamp_ms"] >= candidate["entry_eligible_timestamp_ms"]
                if not allow_entry:
                    reason = "pending_entry_cancelled_during_recovery_or_stale_feed"
                elif eligible and tick["timestamp_ms"] <= candidate["entry_deadline_timestamp_ms"]:
                    price, poc = tick["price"], candidate["frozen_poc"]
                    side = 1 if candidate["direction"] == "long" else -1
                    risk = side * (poc - price)
                    if risk <= 0 or 2 * price - poc <= 0 or 100 * risk / price < 1:
                        reason = "distance_or_geometry_invalid_at_actual_fill"
                    else:
                        candidate["stop"] = 2 * price - poc
                        p.target_r = 1.0
                        self.store.record(account, "execution_bracket_resolved", dict(
                            sample_id=candidate["sample_id"], timestamp_ms=tick["timestamp_ms"],
                            agg_trade_id=tick["agg_trade_id"], entry=price,
                            stop=candidate["stop"], frozen_poc=poc))
                if reason:
                    p.next_signal += 1
                    self.store.record(account, "decision", dict(sample_id=candidate["sample_id"],
                        decision="SKIP", reason=reason))
                self.store.db.execute("UPDATE accounts SET state=? WHERE id=?",
                                      (save_state(p, cfg), account))
            return super().ticks(account, [tick])[0]


class Model:
    def __init__(self, store):
        self.store = store
        if any(row[0] != ACCOUNT for row in store.db.execute("SELECT id FROM accounts")):
            raise ValueError("Use a dedicated database for this paper model")
        self.manager = PocPaperManager(store)
        identity = hashlib.sha256(encode(CONTRACT).encode()).hexdigest()
        self.manager.register(ACCOUNT, SYMBOL, Config(initial_equity=10000, risk_fraction=.025,
            fee_bps=4, max_leverage=5), contract=identity)
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS mr_ticks(
                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, price REAL NOT NULL,
                qty REAL NOT NULL, buy INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS mr_tick_time ON mr_ticks(ts);
            CREATE TABLE IF NOT EXISTS mr_sessions(day TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        files = [Path(__file__), *[Path(inspect.getfile(x)) for x in
                 (build_basic_volume_profile, PaperOrderManager, StateStore, Config, BinancePublic)]]
        code_hash = hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()
        with store.transaction():
            store.db.execute("INSERT OR IGNORE INTO metadata VALUES ('mr_code_hash',?)", (code_hash,))
            if store.db.execute("SELECT value FROM metadata WHERE key='mr_code_hash'").fetchone()[0] != code_hash:
                raise ValueError("Paper source changed: use a new version/database; do not reset the ledger")

    def cursor(self):
        row = self.store.db.execute("SELECT * FROM mr_ticks ORDER BY id DESC LIMIT 1").fetchone()
        return (dict(agg_trade_id=row["id"], timestamp_ms=row["ts"], price=row["price"],
                     quantity=row["qty"], buy=bool(row["buy"]), symbol=SYMBOL) if row else None)

    def freeze(self, tick):
        day, start, opening, end = session_bounds(tick["timestamp_ms"])
        rows = self.store.db.execute("SELECT * FROM mr_ticks WHERE ts>=? AND ts<? ORDER BY id",
                                     (start, opening)).fetchall()
        reason = "valid"
        if not rows:
            reason = "profile_no_data"
        elif max(rows[0]["ts"] - start, opening - rows[-1]["ts"]) > 60000:
            reason = "profile_boundary_gap"
        elif any(b["ts"] - a["ts"] > 300000 or b["id"] != a["id"] + 1
                 for a, b in zip(rows, rows[1:])):
            reason = "profile_internal_gap"
        elif tick["timestamp_ms"] - opening > 60000:
            reason = "ny_open_gap"
        state = dict(day=day, profile_start_ms=start, opening_ms=opening, end_ms=end,
                     opening_price=tick["price"], previous_price=tick["price"], consumed=False,
                     quality=reason, profile_rows=len(rows))
        if reason == "valid":
            vp = build_basic_volume_profile(pd.DataFrame(dict(
                price=[r["price"] for r in rows], qty=[r["qty"] for r in rows])),
                n_bins=50, value_area_pct=.7)
            poc, val, vah = (float(vp[k]) for k in ("poc_price", "val", "vah"))
            state.update(poc=poc, val=val, vah=vah, profile_volume=float(vp["total_volume"]))
            if not 0 < val < poc < vah:
                state["quality"] = "degenerate_profile"
            state.update(side=int(np.sign(poc - tick["price"])),
                         location=tick["price"] < val or tick["price"] > vah)
        self.store.record(ACCOUNT, "session_frozen", state)
        self.store.db.execute("DELETE FROM mr_ticks WHERE ts<?", (start,))
        return state

    def process(self, tick, *, origin="stream", now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if tick.get("symbol") != SYMBOL or type(tick.get("buy")) is not bool:
            raise ValueError("Invalid symbol/side")
        if any(type(tick.get(k)) is not int or tick[k] < 0 for k in ("timestamp_ms", "agg_trade_id")):
            raise ValueError("Invalid event identity")
        if any(isinstance(tick.get(k), bool) or not isinstance(tick.get(k), (float, int))
               or not math.isfinite(tick[k]) or tick[k] <= 0 for k in ("price", "quantity")):
            raise ValueError("Invalid price/quantity")
        with self.store.transaction():
            previous = self.cursor()
            if previous and tick["agg_trade_id"] <= previous["agg_trade_id"]:
                old = self.store.db.execute("SELECT * FROM mr_ticks WHERE id=?", (tick["agg_trade_id"],)).fetchone()
                if old and (old["ts"], old["price"], old["qty"], bool(old["buy"])) != (
                        tick["timestamp_ms"], tick["price"], tick["quantity"], tick["buy"]):
                    raise ValueError("Conflicting duplicate")
                return
            if previous and (tick["agg_trade_id"] != previous["agg_trade_id"] + 1
                             or tick["timestamp_ms"] < previous["timestamp_ms"]):
                raise UnrecoverableGap("Feed must be repaired before advancing the ledger")
            fresh = origin == "stream" and 0 <= now_ms - tick["timestamp_ms"] <= 5000
            self.manager.tick(ACCOUNT, tick, allow_entry=fresh)
            day, _, _, _ = session_bounds(tick["timestamp_ms"])
            old = self.store.db.execute("SELECT payload FROM mr_sessions WHERE day=?", (day,)).fetchone()
            state = json.loads(old[0]) if old else self.freeze(tick)
            if previous and tick["timestamp_ms"] - previous["timestamp_ms"] > 300000:
                state["quality"] = "observation_large_trade_gap"
            if state["quality"] == "valid" and not state["consumed"] and state["side"]:
                side = state["side"]
                boundary = state["val"] if side == 1 else state["vah"]
                price = tick["price"]
                touched = price == boundary or (state["previous_price"] - boundary) * (price - boundary) < 0
                if touched:
                    state["consumed"] = True
                    risk = side * (state["poc"] - price)
                    geometry = risk > 0 and 2 * price - state["poc"] > 0
                    selected = fresh and state["location"] and geometry and 100 * risk / price >= 1
                    ts = tick["timestamp_ms"]
                    deadline = min(ts + 300000, state["end_ms"] - 1)
                    if ts + 1 <= deadline and geometry:
                        intent = dict(sample_id=f"{ACCOUNT}:{day}", session_day=day, symbol=SYMBOL,
                            direction="long" if side == 1 else "short", stop=2 * price - state["poc"],
                            entry_reference=price, frozen_poc=state["poc"], feature_as_of_ms=ts,
                            entry_eligible_timestamp_ms=ts + 1, entry_deadline_timestamp_ms=deadline,
                            force_exit_timestamp_ms=state["end_ms"], strategy=ACCOUNT, route="paper")
                        self.manager.submit(ACCOUNT, intent, snapshot=dict(state,
                            feature_as_of_ms=ts, origin=origin, fresh=fresh),
                            risk_fraction=.025, selected=bool(selected))
                    else:
                        self.store.record(ACCOUNT, "first_touch_skipped", dict(
                            day=day, timestamp_ms=ts, reason="invalid_geometry_or_no_remaining_entry_time"))
            state["previous_price"] = tick["price"]
            self.store.db.execute("INSERT OR REPLACE INTO mr_sessions VALUES (?,?)", (day, encode(state)))
            self.store.db.execute("INSERT INTO mr_ticks VALUES (?,?,?,?,?)", (
                tick["agg_trade_id"], tick["timestamp_ms"], tick["price"], tick["quantity"], int(tick["buy"])))


def history(api, start, end, cursor=None):
    """Less-than-hour REST windows; fromId continuation retains same-ms prints."""
    while start < end:
        right = min(start + 3599999, end)
        params = dict(fromId=cursor["agg_trade_id"] + 1) if cursor else dict(startTime=start, endTime=right - 1)
        while True:
            rows = api.get("/fapi/v1/aggTrades", symbol=SYMBOL, limit=1000, **params)
            if not rows:
                if cursor:
                    return
                break
            if "fromId" in params and int(rows[0]["a"]) != params["fromId"]:
                raise UnrecoverableGap("REST continuation did not return the requested aggregate ID")
            for row in rows:
                tick = normalize(row, SYMBOL)
                if tick["timestamp_ms"] >= (end if cursor else right):
                    if cursor:
                        return
                    break
                yield tick
            else:
                if len(rows) == 1000:
                    params = dict(fromId=int(rows[-1]["a"]) + 1)
                    continue
                if cursor:
                    return
            break
        start = right


def status(path):
    path = Path(path).resolve()
    if not path.exists():
        raise ValueError(f"Paper database does not exist: {path}")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT state,cursor FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
        if row is None:
            raise ValueError("Not this paper account")
        state, cursor = json.loads(row[0]), json.loads(row[1]) if row[1] else None
        runtime = db.execute("SELECT value FROM metadata WHERE key='mr_runtime'").fetchone()
        session = db.execute("SELECT payload FROM mr_sessions ORDER BY day DESC LIMIT 1").fetchone()
        result = dict(account=ACCOUNT, mode="paper", closed_equity=state["equity"],
                      position=state["position"], pending_candidates=len(state["signals"]),
                      last_trade=cursor, runtime=json.loads(runtime[0]) if runtime else None,
                      session=json.loads(session[0]) if session else None,
                      completed_trades=db.execute("SELECT count(*) FROM journal WHERE kind='trade'").fetchone()[0])
        age = int(time.time() * 1000) - cursor["timestamp_ms"] if cursor else None
        heartbeat_age = int(time.time() * 1000) - result["runtime"]["wall_ms"] if result["runtime"] else None
        result.update(feed_age_ms=age, healthy=bool(age is not None and 0 <= age < 30000
            and heartbeat_age is not None and 0 <= heartbeat_age < 15000
            and result["runtime"]["phase"] == "RUNNING"))
        return result


def run(path):
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store = StateStore(path, "binance_usdm")
    store.acquire_writer()
    def heartbeat(phase, error=None):
        value = dict(phase=phase, wall_ms=int(time.time() * 1000), error=error)
        store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('mr_runtime',?)", (encode(value),))
    try:
        model = Model(store)
        api = BinancePublic()
        heartbeat("BOOTSTRAPPING")
        market = api.validate_markets([SYMBOL])[SYMBOL]
        if market["quoteAsset"] != "USDC" or market["marginAsset"] != "USDC" or market["baseAsset"] != "LTC":
            raise ValueError("Market identity mismatch")
        end = api.server_time()
        if abs(end - int(time.time() * 1000)) > 5000:
            raise ValueError("Local clock differs from Binance by more than five seconds")
        cursor = model.cursor()
        start = cursor["timestamp_ms"] if cursor else session_bounds(end)[1]
        if end - start >= 48 * 3600000:
            raise UnrecoverableGap("Checkpoint older than REST retention; do not reset an existing ledger")
        count = 0
        for tick in history(api, start, end, cursor):
            if stopping:
                break
            model.process(tick, origin="bootstrap")
            count += 1
            if count % 1000 == 0:
                heartbeat("BOOTSTRAPPING")
        while not stopping:
            try:
                heartbeat("CONNECTING")
                last_heartbeat = 0
                seen_stream = False
                for tick in api.stream([SYMBOL]):
                    if stopping:
                        break
                    if tick:
                        previous = model.cursor()
                        if previous:
                            for missing in api.repair(SYMBOL, previous, tick):
                                model.process(missing, origin="repair")
                        model.process(tick)
                        seen_stream = True
                    if time.monotonic() - last_heartbeat > 5:
                        heartbeat("RUNNING" if seen_stream else "WAITING_FOR_FIRST_STREAM_TRADE")
                        last_heartbeat = time.monotonic()
            except UnrecoverableGap:
                raise
            except (OSError, TimeoutError) as exc:
                heartbeat("DISCONNECTED", str(exc))
                print(encode(dict(event="disconnected", error=str(exc))), flush=True)
                for _ in range(10):
                    if stopping:
                        break
                    time.sleep(1)
        heartbeat("STOPPED")
    except BaseException as exc:
        heartbeat("FAILED", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--action", choices=("run", "status", "health"), default="status")
    args = parser.parse_args()
    if args.action == "run":
        run(args.database)
    else:
        result = status(args.database)
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.action == "health" and not result["healthy"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
