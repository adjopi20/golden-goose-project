"""ETHUSDC Quiet Breakout, P85 plus P5 and conditional initiative sizing."""

import argparse
from contextlib import ExitStack
from decimal import Decimal
import json
import os
from pathlib import Path
import time

from live_trading.burst_runtime import HOUR, report, run_stream
from live_trading.live_model.quiet_breakout.hypeusdt.strategy import (
    BASE_RISK, NOTIONAL_CAP, QuietBreakout, QuietFuturesExecutor,
)
from utils.engine.adapters.binance_execution import FuturesOrders
from utils.engine.adapters.binance_public import BinancePublic
from utils.engine.contracts import canonical_json
from utils.engine.execution.burst_futures import BurstFuturesStore
from utils.engine.execution.futures_preflight import positive
from utils.model_registry import verify_model


class Strategy(QuietBreakout):
    SYMBOL = "ETHUSDC"
    MARGIN_ASSET = "USDC"
    VARIANT = "stable"
    CONTRACT = "QUIET-ETH-STABLE-P85-P5-INIT15-RME24-LIVE-001"
    ALLOCATION_ID = "quiet_breakout_ethusdc_stable"
    DATABASE = "quiet_breakout_ethusdc_stable.sqlite"
    LOOKBACK_HOURS = 24
    RISK_MANAGEMENT = "rm-e"
    RISK_P85 = True
    HOLD_HOURS = 24

    @classmethod
    def execution_config(cls, allocation, armed):
        package = verify_model(__file__)
        if "model_config.json" not in package["model_files"]:
            raise ValueError("quiet_variant_configuration_not_pinned")
        policy = cls.model_config()
        pinned_policy = json.loads(Path(__file__).with_name("model_config.json").read_text(encoding="utf-8"))
        if (
            (package["symbol"], package["pnl_unit"], package["risk_fraction"], package["notional_equity_cap"])
            != (cls.SYMBOL, cls.MARGIN_ASSET, float(BASE_RISK), NOTIONAL_CAP)
            or (package["contract"], package["allocation_id"], package["journal"], pinned_policy)
            != (cls.CONTRACT, cls.ALLOCATION_ID, cls.DATABASE, policy)
        ):
            raise ValueError("quiet_contract_identity_mismatch")
        return dict(
            symbol=cls.SYMBOL, contract=cls.CONTRACT, allocation_id=cls.ALLOCATION_ID,
            initial_equity=str(positive(allocation)), execution_mode="live", margin_asset=cls.MARGIN_ASSET,
            execution_enabled=armed, accept_new_entries=armed, model_config=policy,
            # The shared partial exit places TP1; QuietFuturesExecutor supplies the RM-E stop.
            risk_management="rm-d",
        )


class EthFuturesExecutor(QuietFuturesExecutor):
    """Apply ETH's 24h clock to the pinned Burst order lifecycle."""

    def _bind_deadline(self, session):
        if session is None or session["state"] not in ("ENTRY_PENDING", "OPEN"):
            return None
        plan = json.loads(session["plan"])
        first_fill = plan.get("first_fill_ms")
        if first_fill is None:
            first_fill = self.s.db.execute("""
                SELECT MIN(json_extract(fill.payload, '$.time'))
                FROM fills AS fill JOIN orders AS entry
                  ON fill.session_id = entry.session_id
                 AND fill.order_id = CAST(json_extract(entry.response, '$.orderId') AS TEXT)
                WHERE entry.session_id = ? AND entry.role = 'ENTRY'
            """, (session["session"],)).fetchone()[0]
        anchor = first_fill if first_fill is not None else json.loads(session["intention"])["expires_timestamp_ms"]
        deadline = anchor + Strategy.HOLD_HOURS * HOUR
        if plan["force_exit_ms"] != deadline:
            plan["force_exit_ms"] = deadline
            with self.s.transaction():
                self.s.db.execute("UPDATE sessions SET plan=? WHERE session=?",
                                  (canonical_json(plan), session["session"]))
                self.s.audit("time_exit_deadline", dict(
                    setup_id=session["id"], first_fill_ms=first_fill, force_exit_ms=deadline))
        return plan

    def _cycle(self, now_ms, *, new_entries_ok=True):
        # Rebind on recovery and again after the parent reconciles new fills.
        self._bind_deadline(self.s.active())
        super()._cycle(now_ms, new_entries_ok=new_entries_ok)
        session = self.s.active()
        plan = self._bind_deadline(session)
        if plan is not None and now_ms >= plan["force_exit_ms"]:
            amount, _ = self.position()
            sign = 1 if plan["direction"] == "long" else -1
            if 0 < sign * amount <= positive(plan["quantity"]):
                self._exit(session, amount, "time_exit_24h")

    def _partial(self, session, plan):
        # During late fill reconciliation a position may already be due for exit.
        if int(time.time() * 1000) < plan["first_fill_ms"] + Strategy.HOLD_HOURS * HOUR:
            super()._partial(session, plan)

    def _exit(self, session, quantity, reason):
        if reason in ("time_exit_24h", "time_exit_48h"):
            stop = self.s.db.execute(
                "SELECT 1 FROM orders WHERE session_id=? AND role='STOP'", (session["session"],)).fetchone()
            if stop is None:
                # A late restart can discover an overdue fill before placing its native stop.
                self._trail_stop(session, json.loads(session["plan"]), None)
            reason = "time_exit_24h"
        return super()._exit(session, quantity, reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--allocation-usdc", type=Decimal)
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
    if args.database.name != Strategy.DATABASE:
        parser.error(f"Use the journal filename {Strategy.DATABASE}")
    if args.report:
        report(args.database)
        return 0
    if args.allocation_usdc is None or not args.allocation_usdc.is_finite() or args.allocation_usdc <= 0:
        parser.error("Allocation must be finite and positive")
    if args.duration is not None and (args.armed or args.preflight or args.duration <= 0):
        parser.error("Duration is only supported for a positive disarmed smoke test")
    if (args.armed or args.preflight) and not all((args.api_key_file, args.private_key_file)):
        parser.error("Live preflight and execution require external Ed25519 secret mounts")

    config = Strategy.execution_config(args.allocation_usdc, args.armed)
    if not (args.armed or args.preflight):
        config["allocation_id"] = "shadow:" + config["allocation_id"]
    venue = FuturesOrders(
        execution_enabled=args.armed, acknowledgement=os.getenv("BINANCE_PRODUCTION_ACK", ""),
        api_key_file=args.api_key_file, private_key_file=args.private_key_file, passphrase_file=args.passphrase_file,
    )
    with ExitStack() as resources:
        resources.callback(venue.close)
        if args.armed and not venue.armed:
            parser.error("Server real-money acknowledgement is required for execution")
        public = BinancePublic()
        resources.callback(public.http.close)
        symbol = Strategy.SYMBOL
        if public.validate_markets([symbol])[symbol]["marginAsset"] != Strategy.MARGIN_ASSET:
            raise ValueError("unexpected_ethusdc_collateral")
        store = BurstFuturesStore(args.database, config)
        resources.callback(store.close)
        store.acquire_writer()
        executor = EthFuturesExecutor(store, venue, config)
        model = Strategy(store, executor)
        if args.armed or args.preflight:
            venue.sync_time()
            if store.active() is None:
                executor.entry_guard(int(time.time() * 1000))
            elif args.preflight:
                raise ValueError("preflight_requires_flat_ledger")
        print(json.dumps(dict(event="startup", symbol=symbol, variant=Strategy.VARIANT, contract=config["contract"],
                              armed=venue.armed, initial_equity=config["initial_equity"])), flush=True)
        if not args.preflight:
            market = public
            if args.market_nats_url:
                from utils.engine.adapters.burst_market import BurstMarketInput
                market = BurstMarketInput(args.market_nats_url, model)
            run_stream({symbol: model}, venue, market, duration=args.duration)
            if args.duration is not None and model.get("public_cursor") is None:
                raise ValueError("smoke_no_public_trades:ETHUSDC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
