"""Shared market input, evaluation loop and portfolio launcher for Burst packages."""
import argparse
from contextlib import ExitStack, closing
from decimal import Decimal
import json
import os
import sqlite3
import sys
from pathlib import Path
import time

import numpy as np
import pandas as pd
from websockets.exceptions import ConnectionClosed

from indicator.volume_profile import build_basic_volume_profile
from live_trading.burst_signals import (
    _burst_signals_from_features, assess_breakout_flow, confirm_flow_entries,
)
from utils.engine.adapters.binance_execution import FuturesOrders, VenueError
from utils.engine.adapters.binance_futures_readonly import PreflightError
from utils.engine.adapters.binance_public import BinancePublic, UnrecoverableGap
from utils.engine.execution.burst_futures import BurstFuturesExecutor, BurstFuturesStore, SharedBurstAccount, DAY
from utils.model_registry import verify_model

MINUTE = 60_000
HOUR = 60 * MINUTE


class NativeBurst:
    LOOKBACK_HOURS = 24
    FLOW = (3, 12)
    RISK_MANAGEMENT = "rm-c"
    HOLD_HOURS = 48

    def __init__(self, store, executor):
        self.store, self.executor = store, executor
        self.lookback_hours = self.LOOKBACK_HOURS
        self.step_minutes, self.window_minutes = self.FLOW
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS burst_minutes(
                timestamp_ms INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
                buy_volume REAL, sell_volume REAL);
            CREATE TABLE IF NOT EXISTS burst_prices(
                hour INTEGER, price REAL, qty REAL, PRIMARY KEY(hour,price));
        """)

    @classmethod
    def model_config(cls):
        step, window = cls.FLOW
        return dict(mode="profile_burst", setup_variant="b", lookback_hours=cls.LOOKBACK_HOURS,
                    flow_entry_confirmation=True, flow_timeframes=f"{step}m_{window}m",
                    flow_interpretation="static", entry_gate=False, one_entry_per_utc_day=True,
                    risk_management=cls.RISK_MANAGEMENT, risk_exit_hours=cls.HOLD_HOURS)

    @classmethod
    def execution_config(cls, allocation, armed):
        contract = verify_model(sys.modules[cls.__module__].__file__)
        policy = cls.model_config()
        if (contract["symbol"], contract["contract"], contract["pnl_unit"], contract["model_config"]) != (
            cls.SYMBOL, cls.CONTRACT, cls.MARGIN_ASSET, policy
        ):
            raise ValueError("contract_identity_mismatch")
        config = dict(symbol=cls.SYMBOL, contract=cls.CONTRACT, allocation_id=cls.ALLOCATION_ID,
                      initial_equity=str(allocation), execution_mode="live",
                      execution_enabled=armed, accept_new_entries=armed)
        if cls.SYMBOL != "HYPEUSDT":
            config.update(margin_asset=cls.MARGIN_ASSET, risk_management=cls.RISK_MANAGEMENT,
                          model_config=policy)
        return config

    def get(self, name, default=None):
        return json.loads(self.store.meta(name, json.dumps(default)))

    def put(self, name, value):
        self.store.set_meta(name, json.dumps(value, allow_nan=False))

    def minutes(self, lower, upper):
        rows = self.store.db.execute(
            "SELECT * FROM burst_minutes WHERE timestamp_ms>=? AND timestamp_ms<? ORDER BY timestamp_ms", (lower, upper))
        return pd.DataFrame([dict(row) for row in rows])

    def setup(self, closing):
        first_hour = closing - (self.lookback_hours + 1) * HOUR
        frame = self.minutes(first_hour, closing)
        event = dict(setup_id=f"profile-burst-{closing}", closing_ms=closing)
        if frame.empty:
            self.store.audit("hourly_decision", dict(event, reason="profile_warmup"))
            return None
        hourly = frame.groupby(frame.timestamp_ms // HOUR).agg(
            open=("open", "first"), close=("close", "last"),
            buy=("buy_volume", "sum"), sell=("sell_volume", "sum"))
        if len(hourly) != self.lookback_hours + 1:
            self.store.audit("hourly_decision", dict(event, reason="profile_warmup_or_missing_hour",
                                                    complete_hours=len(hourly)))
            return None
        history, candle = hourly.iloc[:-1], hourly.iloc[-1]
        price_rows = self.store.db.execute(
            "SELECT price,SUM(qty) AS qty FROM burst_prices WHERE hour>=? AND hour<? GROUP BY price ORDER BY price",
            (first_hour, closing - HOUR))
        profile = build_basic_volume_profile(pd.DataFrame([dict(row) for row in price_rows]),
                                             n_bins=50, value_area_pct=.70)
        feature = dict(closing=closing, open=float(candle.open), close=float(candle.close),
                       body=float(abs(candle.close - candle.open)), volume=float(candle.buy + candle.sell),
                       body_p85=float(np.quantile(abs(history.close - history.open), .85)),
                       volume_p85=float(np.quantile(history.buy + history.sell, .85)),
                       val=profile["val"], vah=profile["vah"], poc=profile["poc_price"])
        signals = _burst_signals_from_features(pd.DataFrame([feature]), closing + 1)
        reasons = []
        if feature["val"] >= feature["vah"]:
            reasons.append("invalid_value_area")
        if feature["val"] <= feature["close"] <= feature["vah"]:
            reasons.append("close_inside_value_area")
        if feature["body"] <= feature["body_p85"]:
            reasons.append("body_below_p85")
        if feature["volume"] <= feature["volume_p85"]:
            reasons.append("volume_below_p85")
        self.store.audit("hourly_decision", dict(event, reason="burst" if signals else "rejected",
                                                checks=reasons, feature=feature,
                                                direction=signals[0]["direction"] if signals else None))
        return signals[0] if signals else None

    def evaluate(self, closing, received_ms, *, allow_entry):
        active = self.get("active_setup")
        if active:
            setup_ms = active["feature_available_at_ms"]
            elapsed = (closing - setup_ms) // MINUTE
            if self.window_minutes <= elapsed <= 60 and elapsed % self.step_minutes == 0:
                frame = self.minutes(setup_ms - DAY, closing)
                if not frame.empty:
                    trace = assess_breakout_flow(frame, [active], step_minutes=self.step_minutes,
                                                 window_minutes=self.window_minutes)
                    trace = trace[trace.assessment_timestamp_ms <= closing].copy()
                    confirmed, trace = confirm_flow_entries(trace, [active], closing + 1,
                                                            window_minutes=self.window_minutes)
                    if not trace.empty:
                        row = json.loads(trace.iloc[-1].to_json())
                        if not row["decision_reason"]:
                            checks = [
                                (row["window_complete"], "flow_window_incomplete"),
                                ((row["progress_from_anchor_pct"] or 0) > 0, "price_not_beyond_anchor"),
                                ((row["progress_window_pct"] or 0) > 0, "window_progress_not_aligned"),
                                ((row["progress_bar_pct"] or 0) > 0, "last_bar_progress_not_aligned"),
                                ((row["urgency_percentile"] or 0) > 75, "urgency_at_or_below_p75"),
                                (any(row[name] for name in ("initiative_candidate", "absorption_candidate",
                                                            "exhaustion_candidate", "low_volume_expansion")),
                                 "no_supporting_flow_mechanism"),
                            ]
                            row["decision_reason"] = "|".join(reason for passed, reason in checks if not passed)
                        self.store.audit("flow_assessment", row)
                        if confirmed:
                            signal = confirmed[0]
                            eligible = signal["entry_eligible_timestamp_ms"]
                            if allow_entry and eligible <= received_ms < eligible + MINUTE:
                                identity = active["sample_id"]
                                if self.executor.symbol != "HYPEUSDT":
                                    identity = f"{self.executor.symbol}-{identity}"
                                self.executor.offer(dict(sample_id=identity,
                                    direction=signal["direction"], initial_stop=signal["stop"],
                                    reference_close=signal["entry_reference"], observed_at_ms=received_ms,
                                    eligible_timestamp_ms=eligible, expires_timestamp_ms=eligible + MINUTE - 1,
                                    confirmation_mechanisms=signal["confirmation_mechanisms"]), received_ms)
                            else:
                                self.store.audit("entry_skipped", dict(setup_id=active["sample_id"],
                                    reason="stale_or_repaired_confirmation"))
                            active = None
                        elif row["decision"] in ("cancelled", "expired", "unavailable"):
                            active = None
            if active and elapsed >= 60:
                self.store.audit("entry_skipped", dict(setup_id=active["sample_id"], reason="observation_timeout"))
                active = None
        if closing % HOUR == 0:
            active = self.setup(closing)
            self.store.db.execute("DELETE FROM burst_minutes WHERE timestamp_ms<?",
                                  (closing - (self.lookback_hours + 3) * HOUR,))
            self.store.db.execute("DELETE FROM burst_prices WHERE hour<?",
                                  (closing - (self.lookback_hours + 2) * HOUR,))
        self.put("active_setup", active)

    def ingest(self, trade, received_ms, *, allow_entry):
        stamp = trade["timestamp_ms"] // MINUTE * MINUTE
        with self.store.transaction():
            previous = self.get("public_cursor")
            if previous:
                if trade["agg_trade_id"] == previous["agg_trade_id"] and trade == previous:
                    return
                if trade["agg_trade_id"] != previous["agg_trade_id"] + 1 or trade["timestamp_ms"] < previous["timestamp_ms"]:
                    raise ValueError("unreconciled_public_stream_sequence")
                last = previous["timestamp_ms"] // MINUTE * MINUTE
                for closing in range(last + MINUTE, stamp + MINUTE, MINUTE):
                    self.evaluate(closing, received_ms, allow_entry=allow_entry and closing == stamp)
            else:
                self.put("complete_from_ms", (stamp // HOUR + 1) * HOUR)
            if stamp >= self.get("complete_from_ms"):
                buy, sell = (trade["quantity"], 0.) if trade["buy"] else (0., trade["quantity"])
                self.store.db.execute("""
                    INSERT INTO burst_minutes VALUES (?,?,?,?,?,?,?) ON CONFLICT(timestamp_ms)
                    DO UPDATE SET high=MAX(high,excluded.high), low=MIN(low,excluded.low), close=excluded.close,
                    buy_volume=buy_volume+excluded.buy_volume, sell_volume=sell_volume+excluded.sell_volume
                    """, (stamp, trade["price"], trade["price"], trade["price"], trade["price"], buy, sell))
                self.store.db.execute("""
                    INSERT INTO burst_prices VALUES (?,?,?) ON CONFLICT(hour,price)
                    DO UPDATE SET qty=qty+excluded.qty
                    """, (stamp // HOUR * HOUR, trade["price"], trade["quantity"]))
            self.put("public_cursor", trade)
            session = self.store.active()
            if allow_entry and session and session["state"] == "OPEN":
                plan = json.loads(session["plan"])
                if trade["timestamp_ms"] >= plan["first_fill_ms"]:
                    mark = self.get("live_excursion")
                    better = mark is None or mark["setup_id"] != session["id"]
                    if not better:
                        sign = 1 if plan["direction"] == "long" else -1
                        better = sign * (trade["price"] - mark["price"]) > 0
                    if better:
                        self.put("live_excursion", dict(trade, setup_id=session["id"]))

    def reset_profile(self, reason):
        with self.store.transaction():
            self.store.db.execute("DELETE FROM burst_minutes")
            self.store.db.execute("DELETE FROM burst_prices")
            self.put("public_cursor", None)
            self.put("active_setup", None)
            self.put("live_excursion", None)
            self.store.audit("feed_reset", dict(reason=reason))


def run_stream(models, venue, public, *, duration=None):
    started = last_heartbeat = last_cycle = last_sync = time.monotonic()
    last_live_ms = dict.fromkeys(models, 0)

    def manage(now, *, allow_entry):
        if not venue.armed:
            return
        for symbol, model in models.items():
            try:
                model.executor.cycle(now, new_entries_ok=allow_entry and now - last_live_ms[symbol] <= 10_000)
            except (ValueError, OSError, TimeoutError, VenueError, PreflightError) as error:
                with model.store.transaction():
                    model.store.audit("cycle_error", dict(symbol=symbol, error_type=type(error).__name__,
                                                          reason=str(error) if type(error) is ValueError else None))
                print(json.dumps(dict(event="cycle_error", symbol=symbol, error_type=type(error).__name__)), flush=True)

    while duration is None or time.monotonic() - started < duration:
        try:
            for trade in public.stream(list(models)):
                now = int(time.time() * 1000)
                if trade is not None:
                    symbol = trade["symbol"]
                    model = models[symbol]
                    previous = model.get("public_cursor")
                    if previous and trade["agg_trade_id"] > previous["agg_trade_id"] + 1:
                        with model.store.transaction():
                            model.store.audit("feed_gap", dict(expected=previous["agg_trade_id"] + 1,
                                                              incoming=trade["agg_trade_id"]))
                        try:
                            for missing in public.repair(symbol, previous, trade):
                                now = int(time.time() * 1000)
                                model.ingest(missing, now, allow_entry=False)
                                if venue.armed and time.monotonic() - last_cycle >= 2:
                                    manage(now, allow_entry=False)
                                    last_cycle = time.monotonic()
                        except UnrecoverableGap:
                            model.reset_profile("unrecoverable_public_gap")
                    now = int(time.time() * 1000)
                    fresh = -1000 <= now - trade["timestamp_ms"] <= 10_000
                    model.ingest(trade, now, allow_entry=fresh)
                    if fresh:
                        last_live_ms[symbol] = now
                if time.monotonic() - last_sync >= 60 and venue.armed:
                    venue.sync_time()
                    last_sync = time.monotonic()
                if venue.armed and time.monotonic() - last_cycle >= 2:
                    # An idle feed can manage exits, but only a recent print permits an entry.
                    manage(now, allow_entry=True)
                    last_cycle = time.monotonic()
                if time.monotonic() - last_heartbeat >= 60:
                    for symbol, model in models.items():
                        print(json.dumps(dict(event="heartbeat", symbol=symbol, status=model.store.status(),
                                              public_cursor=model.get("public_cursor"))), flush=True)
                    last_heartbeat = time.monotonic()
                if duration is not None and time.monotonic() - started >= duration:
                    return
        except (OSError, TimeoutError, ConnectionClosed, VenueError, PreflightError) as error:
            last_live_ms = dict.fromkeys(models, 0)
            for model in models.values():
                with model.store.transaction():
                    model.store.audit("connection_error", dict(error_type=type(error).__name__))
            print(json.dumps(dict(event="connection_error", error_type=type(error).__name__)), flush=True)
            if venue.armed:
                # An exchange-native stop remains in place while public data reconnects.
                manage(int(time.time() * 1000), allow_entry=False)
            time.sleep(2)


def report(database):
    """Read-only export of the audit journal and its source tables as JSONL."""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for table in ("sessions", "orders", "fills", "funding", "audit"):
            for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                print(json.dumps(dict(table=table, **dict(row)), allow_nan=False))


def main(argv=None):
    from live_trading.live_model.burst_hype.strategy import Strategy as Hype
    from live_trading.live_model.burst_inj.strategy import Strategy as Inj
    from live_trading.live_model.burst_uni.strategy import Strategy as Uni

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--allocation-usdt", type=Decimal)
    parser.add_argument("--allocation-usdc", type=Decimal)
    parser.add_argument("--portfolio", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--private-key-file", type=Path)
    parser.add_argument("--passphrase-file", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--armed", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    parser.add_argument("--duration", type=int)
    args = parser.parse_args(argv)
    if args.report:
        report(args.database)
        return 0
    if args.allocation_usdt is None or not args.allocation_usdt.is_finite() or args.allocation_usdt <= 0:
        parser.error("Allocation must be finite and positive")
    if args.portfolio and (args.allocation_usdc is None or not args.allocation_usdc.is_finite()
                           or args.allocation_usdc <= 0):
        parser.error("Portfolio mode requires an approved positive USDC allocation")
    if args.allocation_usdc is not None and not args.portfolio:
        parser.error("USDC allocation requires portfolio mode")
    if args.duration is not None and (args.armed or args.duration <= 0):
        parser.error("Duration is only supported for a positive disarmed smoke test")
    if (args.armed or args.preflight) and not all((args.api_key_file, args.private_key_file)):
        parser.error("Live preflight and execution require external Ed25519 secret mounts")
    specs = {}
    for package in (Hype, Inj, Uni) if args.portfolio else (Hype,):
        allocation = args.allocation_usdt if package.MARGIN_ASSET == "USDT" else args.allocation_usdc
        config = package.execution_config(allocation, args.armed)
        database = args.database if package is Hype else args.database.with_name(package.DATABASE)
        specs[package.SYMBOL] = (database, config, package)
    venue = FuturesOrders(execution_enabled=args.armed,
                          acknowledgement=os.getenv("BINANCE_PRODUCTION_ACK", ""),
                          api_key_file=args.api_key_file, private_key_file=args.private_key_file,
                          passphrase_file=args.passphrase_file)
    if args.armed and not venue.armed:
        parser.error("Server real-money acknowledgement is required for execution")
    public = BinancePublic()
    try:
        with ExitStack() as stores:
            markets = public.validate_markets(list(specs))
            models = {}
            for symbol, (database, config, package) in specs.items():
                if markets[symbol]["marginAsset"] != config.get("margin_asset", "USDT"):
                    raise ValueError(f"unexpected_{symbol}_collateral")
                store = BurstFuturesStore(database, config)
                stores.callback(store.close)
                store.acquire_writer()
                if not args.portfolio and store.meta("shared_burst_account") is not None:
                    raise ValueError("journal_requires_shared_account_mode")
                executor = BurstFuturesExecutor(store, venue, config)
                models[symbol] = package(store, executor)
            if args.preflight or args.armed:
                venue.sync_time()
                now = int(time.time() * 1000)
                if args.portfolio:
                    shared = SharedBurstAccount([model.executor for model in models.values()],
                                                dict(USDT=args.allocation_usdt, USDC=args.allocation_usdc))
                    shared.initialize(now)
                for model in models.values():
                    if model.store.active():
                        if args.preflight:
                            raise ValueError("preflight_requires_flat_ledger")
                        model.executor.cycle(now, new_entries_ok=False)
                    elif not args.portfolio or args.preflight:
                        model.executor.entry_guard(now)
            for symbol, (_, config, _) in specs.items():
                print(json.dumps(dict(event="startup", symbol=symbol, contract=config["contract"], armed=venue.armed,
                                      initial_equity=config["initial_equity"], stop_risk_fraction=.01)), flush=True)
            if not args.preflight:
                run_stream(models, venue, public, duration=args.duration)
            if args.duration is not None:
                for symbol, model in models.items():
                    cursor = model.get("public_cursor")
                    if cursor is None:
                        raise ValueError(f"smoke_no_public_trades:{symbol}")
                    print(json.dumps(dict(event="smoke_complete", symbol=symbol, last_agg_trade_id=cursor["agg_trade_id"],
                                          armed=venue.armed, fill_count=model.store.status()["fill_count"])), flush=True)
    finally:
        public.http.close()
        venue.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
