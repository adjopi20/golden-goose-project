"""Quiet Breakout HYPEUSDT live policies validated by the combined P5 and 1.5x runs."""

import argparse
from contextlib import ExitStack
from decimal import Decimal
import json
import os
from pathlib import Path
import time

import numpy as np
import pandas as pd

from indicator.volume_profile import build_basic_volume_profile
from live_trading.burst_runtime import HOUR, MINUTE, NativeBurst, report, run_stream
from live_trading.burst_signals import assess_breakout_flow, confirm_flow_entries
from live_trading.live_model.quiet_breakout.position_addition import PositionAddition, PositionAdditionVenue
from utils.engine.adapters.binance_execution import FuturesOrders
from utils.engine.adapters.binance_public import BinancePublic
from utils.engine.execution.burst_futures import DAY, HOLD_MS, BurstFuturesExecutor, BurstFuturesStore
from utils.engine.execution.futures_preflight import positive
from utils.model_registry import verify_model

BASE_RISK = Decimal("0.01")
INITIATIVE_MULTIPLIER = Decimal("1.5")
NOTIONAL_CAP = 5
HISTORY_SIZE = 100
MINIMUM_HISTORY = 30
PATH_QUANTILE = .05
RISK_QUANTILE = .85


def risk_multiplier(mechanisms):
    if "initiative_candidate" in mechanisms and "exhaustion_candidate" not in mechanisms:
        return INITIATIVE_MULTIPLIER
    return Decimal(1)


def exceeds_stop_cap(intention, price):
    cap = intention.get("max_entry_stop_fraction")
    return cap is not None and abs(price - positive(intention["initial_stop"])) / price > positive(cap)


class QuietFuturesExecutor(BurstFuturesExecutor):
    """Bind Quiet's entry sizing, distance cap and RM-E to the shared lifecycle."""

    def entry_risk_fraction(self, intention):
        return BASE_RISK * risk_multiplier(intention["confirmation_mechanisms"])

    def quote(self, now_ms):
        quote = super().quote(now_ms)
        session = self.s.active()
        intention = json.loads(session["intention"])
        if int(time.time() * 1000) > intention["expires_timestamp_ms"]:
            raise ValueError("stale_after_entry_preflight")
        price = positive(quote["askPrice" if intention["direction"] == "long" else "bidPrice"])
        if exceeds_stop_cap(intention, price):
            raise ValueError("executable_quote_exceeds_stop_distance_p85")
        return quote

    def _cycle(self, now_ms, *, new_entries_ok=True):
        super()._cycle(now_ms, new_entries_ok=new_entries_ok)
        session = self.s.active()
        if session is None or session["state"] != "OPEN":
            return
        intention = json.loads(session["intention"])
        if intention.get("max_entry_stop_fraction") is None:
            return
        plan = json.loads(session["plan"])
        if exceeds_stop_cap(intention, positive(plan["actual_entry_price"])):
            # A native market fill can slip through the quote gate. The shared
            # executor has already reconciled its initial native stop.
            amount, _ = self.position()
            sign = 1 if plan["direction"] == "long" else -1
            if 0 < sign * amount <= positive(plan["quantity"]):
                self._exit(session, amount, "actual_fill_exceeds_stop_distance_p85")

    def _trail_stop(self, session, plan, price):
        if self.c["model_config"]["risk_management"] == "rm-e" and price is not None:
            entry = positive(plan["actual_entry_price"])
            distance = abs(entry - positive(plan["stop"]))
            sign = 1 if plan["direction"] == "long" else -1
            if sign * (positive(price) - entry) < 2 * distance:
                price = None
        return super()._trail_stop(session, plan, price)


class QuietAdditionExecutor(PositionAddition, QuietFuturesExecutor):
    pass


class QuietBreakout(NativeBurst):
    PACKAGE_SOURCE = __file__
    VARIANT_FILE = None
    POSITION_ADDITION = False
    SYMBOL = "HYPEUSDT"
    MARGIN_ASSET = "USDT"
    FLOW = (3, 12)
    HOLD_HOURS = 48

    @classmethod
    def model_config(cls):
        step, window = cls.FLOW
        config = dict(
            lookback_hours=cls.LOOKBACK_HOURS, flow_timeframes=f"{step}m_{window}m",
            risk_management=cls.RISK_MANAGEMENT, risk_exit_hours=cls.HOLD_HOURS,
            one_entry_per_utc_day=True, require_volume_p75=True,
            entry_filter="risk_p85" if cls.RISK_P85 else "path_p5",
            initiative_risk_multiplier=float(INITIATIVE_MULTIPLIER),
        )
        if cls.RISK_P85:
            config["additional_path_filter"] = "path_p5"
        if cls.POSITION_ADDITION:
            config.update(pyramid_add=True, pyramid_balance="lock_1r")
        return config

    @classmethod
    def execution_config(cls, allocation, armed):
        source = Path(cls.PACKAGE_SOURCE).resolve()
        package = verify_model(source)
        config_file = cls.VARIANT_FILE or f"{cls.VARIANT}/model_config.json"
        if config_file not in package["model_files"]:
            raise ValueError("quiet_variant_configuration_not_pinned")
        variant = json.loads((source.parent / config_file).read_text(
            encoding="utf-8"))
        policy = cls.model_config()
        if (
            (package["symbol"], package["pnl_unit"], package["risk_fraction"], package["notional_equity_cap"])
            != (cls.SYMBOL, cls.MARGIN_ASSET, float(BASE_RISK), NOTIONAL_CAP)
            or (variant["contract"], variant["allocation_id"], variant["journal"], variant["model_config"])
            != (cls.CONTRACT, cls.ALLOCATION_ID, cls.DATABASE, policy)
            or HOLD_MS != cls.HOLD_HOURS * HOUR
        ):
            raise ValueError("quiet_contract_identity_mismatch")
        return dict(
            symbol=cls.SYMBOL, contract=cls.CONTRACT, allocation_id=cls.ALLOCATION_ID,
            initial_equity=str(positive(allocation)), execution_mode="live", margin_asset=cls.MARGIN_ASSET,
            execution_enabled=armed, accept_new_entries=armed, model_config=policy,
            # The shared RM-D adapter owns the 10% +1R limit. Quiet supplies
            # its RM-E stop policy through QuietFuturesExecutor._trail_stop.
            risk_management="rm-d" if cls.RISK_MANAGEMENT == "rm-e" else cls.RISK_MANAGEMENT,
        )

    def reset_profile(self, reason):
        with self.store.transaction():
            super().reset_profile(reason)
            if self.POSITION_ADDITION:
                self.put("position_addition_trigger", None)

    def setup(self, closing):
        first_hour = closing - (self.lookback_hours + 1) * HOUR
        frame = self.minutes(first_hour, closing)
        identity = f"{self.ALLOCATION_ID}-{closing}"
        if frame.empty:
            self.store.audit("hourly_decision", dict(setup_id=identity, reason="profile_warmup"))
            return None
        frame["volume"] = frame.buy_volume + frame.sell_volume
        hourly = frame.groupby(frame.timestamp_ms // HOUR).agg(
            open=("open", "first"), close=("close", "last"), high=("high", "max"),
            low=("low", "min"), volume=("volume", "sum"),
        )
        if len(hourly) != self.lookback_hours + 1:
            self.store.audit("hourly_decision", dict(
                setup_id=identity, reason="profile_warmup_or_missing_hour", complete_hours=len(hourly)))
            return None
        history, candle = hourly.iloc[:-1], hourly.iloc[-1]
        high, low = float(history.high.max()), float(history.low.min())
        volume_p75 = float(history.volume.quantile(.75))
        event = dict(setup_id=identity, closing_ms=closing, open=float(candle.open),
                     close=float(candle.close), profile_high=high, profile_low=low,
                     volume=float(candle.volume), volume_p75=volume_p75)
        if not low <= candle.open <= high or low <= candle.close <= high or candle.volume < volume_p75:
            self.store.audit("hourly_decision", dict(event, reason="breakout_or_volume_requirement_failed"))
            return None
        prices = self.store.db.execute(
            "SELECT price,SUM(qty) AS qty FROM burst_prices WHERE hour>=? AND hour<? GROUP BY price ORDER BY price",
            (first_hour, closing - HOUR),
        )
        profile = build_basic_volume_profile(pd.DataFrame([dict(row) for row in prices]),
                                             n_bins=50, value_area_pct=.70)
        direction = "long" if candle.close > high else "short"
        stop = float(profile["poc_price"])
        self.store.audit("hourly_decision", dict(
            event, reason="quiet_breakout", direction=direction, poc=stop,
            val=profile["val"], vah=profile["vah"],
        ))
        return dict(sample_id=identity, direction=direction, stop=stop, entry_reference=float(candle.close),
                    feature_available_at_ms=closing)

    def _confirmation(self, signal, frame, received_ms, *, allow_entry):
        eligible = signal["entry_eligible_timestamp_ms"]
        if eligible <= self.get("quiet_last_confirmation_ms", -1):
            self.store.audit("confirmation_replayed", dict(setup_id=signal["sample_id"], eligible_ms=eligible))
            return
        path = frame[frame.timestamp_ms >= eligible - self.window_minutes * MINUTE]
        if len(path) != self.window_minutes:
            raise ValueError("confirmed_flow_missing_minute_prices")
        start = path.open.iloc[0]
        travel = abs(path.close.iloc[0] - start) + path.close.diff().abs().sum()
        efficiency = float(abs(path.close.iloc[-1] - start) / travel) if travel else 0.
        reference = signal["entry_reference"]
        distance = abs(reference - signal["stop"]) / reference
        history = self.get("quiet_confirmation_history", [])
        p5 = p85 = None
        if len(history) >= MINIMUM_HISTORY:
            p5 = float(np.quantile([entry["efficiency"] for entry in history], PATH_QUANTILE))
            if self.RISK_P85:
                p85 = float(np.quantile([entry["distance"] for entry in history], RISK_QUANTILE))
        # Both filters use the same prior C confirmations, even if no order was offered.
        self.put("quiet_confirmation_history", (
            history + [dict(efficiency=efficiency, distance=distance)])[-HISTORY_SIZE:])
        self.put("quiet_last_confirmation_ms", eligible)
        mechanisms = signal["confirmation_mechanisms"]
        risk_fraction = float(BASE_RISK * risk_multiplier(mechanisms))
        passed = (p5 is None or efficiency >= p5) and (p85 is None or distance <= p85)
        identity = signal["sample_id"]
        self.store.audit("confirmation_filters", dict(
            setup_id=identity, eligible_ms=eligible, history_count=len(history),
            efficiency=efficiency, path_p5=p5, distance=distance, risk_p85=p85,
            passed=passed, risk_fraction=risk_fraction, confirmation_mechanisms=mechanisms,
        ))
        if passed and allow_entry and eligible <= received_ms < eligible + MINUTE:
            self.executor.offer(dict(
                sample_id=identity, direction=signal["direction"], initial_stop=signal["stop"],
                reference_close=reference, observed_at_ms=received_ms, eligible_timestamp_ms=eligible,
                expires_timestamp_ms=eligible + MINUTE - 1, confirmation_mechanisms=mechanisms,
                max_entry_stop_fraction=p85, entry_risk_fraction=risk_fraction,
            ), received_ms)
        elif passed:
            self.store.audit("entry_skipped", dict(setup_id=identity, reason="stale_or_repaired_confirmation"))

    def evaluate(self, closing, received_ms, *, allow_entry):
        active = self.get("active_setup")
        if active:
            setup_ms = active["feature_available_at_ms"]
            elapsed = (closing - setup_ms) // MINUTE
            if self.window_minutes <= elapsed <= 60 and elapsed % self.step_minutes == 0:
                preceding_window = (self.window_minutes - self.step_minutes) * MINUTE
                frame = self.minutes(setup_ms - DAY - preceding_window, closing)
                if not frame.empty:
                    trace = assess_breakout_flow(frame, [active], step_minutes=self.step_minutes,
                                                 window_minutes=self.window_minutes)
                    trace = trace[trace.assessment_timestamp_ms <= closing]
                    confirmed, trace = confirm_flow_entries(trace, [active], closing + 1,
                                                            window_minutes=self.window_minutes)
                    row = json.loads(trace.iloc[-1].to_json())
                    self.store.audit("flow_assessment", row)
                    if confirmed:
                        self._confirmation(confirmed[0], frame, received_ms, allow_entry=allow_entry)
                        active = None
                    elif row["decision"] in ("cancelled", "expired", "unavailable"):
                        active = None
            if active and elapsed >= 60:
                self.store.audit("entry_skipped", dict(setup_id=active["sample_id"], reason="observation_timeout"))
                active = None
        if closing % HOUR == 0:
            active = self.setup(closing)
            # Even the 16H profile needs the entire 24H flow baseline plus window warmup.
            self.store.db.execute("DELETE FROM burst_minutes WHERE timestamp_ms<?", (closing - 26 * HOUR,))
            self.store.db.execute("DELETE FROM burst_prices WHERE hour<?",
                                  (closing - (self.lookback_hours + 2) * HOUR,))
        self.put("active_setup", active)


class Stable(QuietBreakout):
    VARIANT = "stable"
    CONTRACT = "QUIET-HYPE-STABLE-P5-INIT15-LIVE-001"
    ALLOCATION_ID = "quiet_breakout_stable"
    DATABASE = "quiet_breakout_stable.sqlite"
    LOOKBACK_HOURS = 24
    RISK_MANAGEMENT = "rm-e"
    RISK_P85 = False


class TailDependent(QuietBreakout):
    VARIANT = "tail_dependent"
    CONTRACT = "QUIET-HYPE-TAIL-P5-INIT15-ADD-LIVE-002"
    ALLOCATION_ID = "quiet_breakout_tail_dependent_position_addition"
    DATABASE = "quiet_breakout_tail_dependent_position_addition.sqlite"
    POSITION_ADDITION = True
    LOOKBACK_HOURS = 16
    RISK_MANAGEMENT = "rm-a"
    RISK_P85 = True


SUBMODELS = {model.VARIANT: model for model in (Stable, TailDependent)}


def run_worker(models, collateral, argv=None):
    parser = argparse.ArgumentParser(description=f"Quiet Breakout {collateral} worker")
    if len(models) > 1:
        parser.add_argument("--variant", choices=models, required=True)
    else:
        parser.set_defaults(variant=next(iter(models)))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(f"--allocation-{collateral.lower()}", dest="allocation", type=Decimal)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--private-key-file", type=Path)
    parser.add_argument("--passphrase-file", type=Path)
    parser.add_argument("--market-nats-url")
    parser.add_argument("--duration", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--armed", action="store_true")
    args = parser.parse_args(argv)
    model_type = models[args.variant]
    if args.database.name != model_type.DATABASE:
        parser.error(f"Use the journal filename {model_type.DATABASE}")
    if args.report:
        report(args.database)
        return 0
    if args.allocation is None or not args.allocation.is_finite() or args.allocation <= 0:
        parser.error("Allocation must be finite and positive")
    if args.duration is not None and (args.armed or args.preflight or args.duration <= 0):
        parser.error("Duration is only supported for a positive disarmed smoke test")
    if (args.armed or args.preflight) and not all((args.api_key_file, args.private_key_file)):
        parser.error("Live preflight and execution require external Ed25519 secret mounts")
    config = model_type.execution_config(args.allocation, args.armed)
    if not (args.armed or args.preflight):
        config["allocation_id"] = "shadow:" + config["allocation_id"]
    venue_type = PositionAdditionVenue if model_type.POSITION_ADDITION else FuturesOrders
    venue = venue_type(
        execution_enabled=args.armed, acknowledgement=os.getenv("BINANCE_PRODUCTION_ACK", ""),
        api_key_file=args.api_key_file, private_key_file=args.private_key_file, passphrase_file=args.passphrase_file,
    )
    with ExitStack() as resources:
        resources.callback(venue.close)
        if args.armed and not venue.armed:
            parser.error("Server real-money acknowledgement is required for execution")
        public = BinancePublic()
        resources.callback(public.http.close)
        symbol = model_type.SYMBOL
        if public.validate_markets([symbol])[symbol]["marginAsset"] != model_type.MARGIN_ASSET:
            raise ValueError(f"unexpected_{symbol.lower()}_collateral")
        store = BurstFuturesStore(args.database, config)
        resources.callback(store.close)
        store.acquire_writer()
        executor_type = QuietAdditionExecutor if model_type.POSITION_ADDITION else QuietFuturesExecutor
        executor = executor_type(store, venue, config)
        model = model_type(store, executor)
        if args.armed or args.preflight:
            venue.sync_time()
            if store.active() is None:
                executor.entry_guard(int(time.time() * 1000))
            elif args.preflight:
                raise ValueError("preflight_requires_flat_ledger")
        print(json.dumps(dict(event="startup", symbol=symbol, variant=args.variant, contract=config["contract"],
                              armed=venue.armed, initial_equity=config["initial_equity"])), flush=True)
        if not args.preflight:
            market = public
            if args.market_nats_url:
                from utils.engine.adapters.burst_market import BurstMarketInput
                market = BurstMarketInput(args.market_nats_url, model)
            run_stream({symbol: model}, venue, market, duration=args.duration)
            if args.duration is not None and model.get("public_cursor") is None:
                raise ValueError(f"smoke_no_public_trades:{symbol}")
    return 0


def main(argv=None):
    return run_worker(SUBMODELS, "USDT", argv)


if __name__ == "__main__":
    raise SystemExit(main())
