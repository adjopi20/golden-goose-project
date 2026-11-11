"""Frozen confirmation and sizing policy for the selected Burst packages."""
from decimal import Decimal
import json
import sys

import numpy as np

from live_trading.burst_runtime import MINUTE, NativeBurst
from live_trading.live_model.quiet_breakout.hypeusdt.strategy import QuietBreakout, risk_multiplier
from live_trading.live_model.quiet_breakout.position_addition import PositionAddition
from utils.engine.execution.burst_futures import BurstFuturesExecutor
from utils.engine.execution.futures_preflight import number, positive
from utils.model_registry import verify_model


class SelectedBurstExecutor(BurstFuturesExecutor):
    def entry_risk_fraction(self, intention):
        return positive(self.c["risk_fraction"]) * risk_multiplier(intention["confirmation_mechanisms"])

    def _sync_sizing_fills(self):
        session = self.s.active()
        if session is None:
            return
        orders = self.s.db.execute(
            "SELECT * FROM orders WHERE session_id=? AND role IN ('TP1','ADD') AND response IS NOT NULL",
            (session["session"],),
        ).fetchall()
        for row in orders:
            native = json.loads(row["response"])
            executed = number(native["executedQty"])
            if executed:
                fills = self.s.db.execute(
                    "SELECT quantity FROM fills WHERE order_id=? AND session_id=?",
                    (str(native["orderId"]), session["session"]),
                )
                if sum((positive(fill["quantity"]) for fill in fills), Decimal(0)) != executed:
                    self.sync_fills(row)

    def cycle(self, now_ms, *, new_entries_ok=True):
        super().cycle(now_ms, new_entries_ok=new_entries_ok)
        if self.v.armed:
            self._sync_sizing_fills()


class SelectedBurstAdditionExecutor(PositionAddition, SelectedBurstExecutor):
    pass


class BurstPackage(NativeBurst):
    # This closed-window evaluator delegates setup and confirmation to Burst.
    evaluate = QuietBreakout.evaluate
    POSITION_SIZING = "initiative_1p5"
    EXECUTOR = SelectedBurstExecutor

    @classmethod
    def model_config(cls):
        return dict(super().model_config(), entry_filter=cls.PATH_FILTER[0], position_sizing=cls.POSITION_SIZING)

    @classmethod
    def execution_config(cls, allocation, armed):
        contract = verify_model(sys.modules[cls.__module__].__file__)
        policy = cls.model_config()
        if (contract["symbol"], contract["contract"], contract["pnl_unit"], contract["model_config"],
                contract["allocation_id"], contract["journal"], contract["risk_fraction"],
                contract["notional_equity_cap"]) != (
            cls.SYMBOL, cls.CONTRACT, cls.MARGIN_ASSET, policy, cls.ALLOCATION_ID,
            cls.DATABASE, float(cls.BASE_RISK), 5
        ):
            raise ValueError("burst_account_configuration_mismatch")
        return dict(symbol=cls.SYMBOL, contract=cls.CONTRACT, allocation_id=cls.ALLOCATION_ID,
                    initial_equity=str(positive(allocation)), execution_mode="live", margin_asset=cls.MARGIN_ASSET,
                    execution_enabled=armed, accept_new_entries=armed, model_config=policy,
                    risk_management=cls.RISK_MANAGEMENT, risk_fraction=float(cls.BASE_RISK))

    def _confirmation(self, signal, frame, received_ms, *, allow_entry):
        eligible = signal["entry_eligible_timestamp_ms"]
        if eligible <= self.get("burst_last_confirmation_ms", -1):
            return
        path = frame.loc[frame.timestamp_ms.between(eligible - self.window_minutes * MINUTE,
                                                    eligible - MINUTE)]
        if len(path) != self.window_minutes:
            raise ValueError("confirmed_flow_missing_minute_prices")
        start = path.open.iloc[0]
        travel = abs(path.close.iloc[0] - start) + path.close.diff().abs().sum()
        score = float(abs(path.close.iloc[-1] - start) / travel) if travel else 0.0
        history = self.get("burst_efficiency_history", [])
        threshold = float(np.quantile(history, self.PATH_FILTER[1])) if len(history) >= 30 else None
        self.put("burst_efficiency_history", (history + [score])[-100:])
        self.put("burst_last_confirmation_ms", eligible)
        mechanisms = signal["confirmation_mechanisms"]
        fraction = self.BASE_RISK * risk_multiplier(mechanisms)
        passed = threshold is None or score >= threshold
        self.store.audit("confirmation_filters", dict(
            setup_id=signal["sample_id"], eligible_ms=eligible, history_count=len(history),
            efficiency=score, threshold=threshold, entry_filter=self.PATH_FILTER[0], passed=passed,
            confirmation_mechanisms=mechanisms, risk_fraction=float(fraction),
        ))
        if not passed:
            return
        if allow_entry and eligible <= received_ms < eligible + MINUTE:
            self.executor.offer(dict(
                sample_id=f'{self.ALLOCATION_ID}-{signal["sample_id"]}', direction=signal["direction"],
                initial_stop=signal["stop"], reference_close=signal["entry_reference"],
                observed_at_ms=received_ms, eligible_timestamp_ms=eligible,
                expires_timestamp_ms=eligible + MINUTE - 1, confirmation_mechanisms=mechanisms,
                entry_risk_fraction=float(fraction),
            ), received_ms)
        else:
            self.store.audit("entry_skipped", dict(setup_id=signal["sample_id"],
                                                  reason="stale_or_repaired_confirmation"))
