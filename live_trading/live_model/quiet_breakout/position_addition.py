"""Quiet's single +2R add on the existing dedicated native Futures lane."""

from decimal import Decimal
import json
import time

from utils.engine.adapters.binance_execution import FuturesOrders
from utils.engine.contracts import canonical_json
from utils.engine.execution.burst_futures import CoordinatedBurstAccount, EntryAdmissionBusy
from utils.engine.execution.futures_preflight import number, positive, quantity_rules
from utils.engine.execution.preny_futures import FuturesExecutor, TERMINAL, order_id, rounded

ADD_FRACTION = Decimal("0.8")
ADD_WINDOW_MS = 59_999


class PositionAdditionVenue(FuturesOrders):
    def submit(self, role, params):
        return super().submit("ENTRY" if role == "ADD" else role, params)


class PositionAddition:
    """Extend the lane only once it has a separately journaled add order."""

    def _validate_response(self, response, row):
        if row["role"] != "ADD":
            return super()._validate_response(response, row)
        if not isinstance(response, dict) or response.get("reduceOnly") is not False:
            raise ValueError("Invalid native position-addition response")
        return super()._validate_response(response, dict(row, role="ENTRY"))

    def _trail_stop(self, session, plan, price):
        previous_lock = plan.get("risk_lock_r", -1)
        state = super()._trail_stop(session, plan, price)
        if state == "protected" and price is not None and previous_lock < 1 <= plan.get("risk_lock_r", -1):
            mark = json.loads(self.s.meta("live_excursion", "null"))
            if mark and mark["setup_id"] == session["id"]:
                trigger = dict(setup_id=session["id"], timestamp_ms=mark["timestamp_ms"],
                               agg_trade_id=mark["agg_trade_id"], locked_r=plan["risk_lock_r"])
                with self.s.transaction():
                    self.s.set_meta("position_addition_trigger", canonical_json(trigger))
                    self.s.audit("position_addition_trigger", trigger)
        return state

    def _addition_order(self, session):
        return self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='ADD'",
                                 (session["session"],)).fetchone()

    def _cycle(self, now_ms, *, new_entries_ok=True):
        session = self.s.active()
        if session is not None and self._addition_order(session) is not None:
            return self._cycle_added(session, now_ms)
        super()._cycle(now_ms, new_entries_ok=new_entries_ok)
        session = self.s.active()
        if (new_entries_ok and self.v.armed and self.c["accept_new_entries"]
                and not self.s.meta("entries_blocked") and session is not None and session["state"] == "OPEN"):
            self._offer_addition(session, now_ms)

    def _addition_available(self, now_ms):
        if isinstance(self.shared_account, CoordinatedBurstAccount):
            return self.shared_account.balance(self.s.asset, now_ms, include_local=True)[1]
        return self.wallet()[1]

    def _offer_addition(self, session, now_ms):
        if not isinstance(self.shared_account, CoordinatedBurstAccount):
            return self._submit_addition(session, now_ms)
        trigger = json.loads(self.s.meta("position_addition_trigger", "null"))
        if (not trigger or trigger["setup_id"] != session["id"]
                or now_ms > trigger["timestamp_ms"] + ADD_WINDOW_MS):
            return
        try:
            with self.shared_account.admission():
                return self._submit_addition(session, now_ms)
        except EntryAdmissionBusy as error:
            with self.s.transaction():
                self.s.audit("addition_deferred", dict(setup_id=session["id"], reason=str(error)))

    def _submit_addition(self, session, now_ms):
        plan = json.loads(session["plan"])
        trigger = json.loads(self.s.meta("position_addition_trigger", "null"))
        cursor = json.loads(self.s.meta("public_cursor", "null"))
        if (not trigger or trigger["setup_id"] != session["id"] or not cursor
                or cursor["agg_trade_id"] <= trigger["agg_trade_id"]
                or not 0 <= now_ms - cursor["timestamp_ms"] <= 10_000):
            return
        deadline = min(trigger["timestamp_ms"] + ADD_WINDOW_MS, plan["force_exit_ms"] - 1)
        if now_ms > deadline or plan.get("risk_lock_r", -1) < 1:
            return
        stop_id = order_id(session["id"], f'sl-{plan["risk_lock_r"]}')
        stop_row = self.s.db.execute("SELECT * FROM orders WHERE client_id=?", (stop_id,)).fetchone()
        if stop_row is None or stop_row["state"] != "NEW":
            return

        expected = positive(plan["actual_entry_quantity"])
        if self.c.get("risk_management") in ("rm-d", "rm-e"):
            tp1 = self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='TP1'",
                                     (session["session"],)).fetchone()
            if tp1 is None or tp1["state"] != "FILLED":
                return
            expected -= positive(json.loads(tp1["response"])["executedQty"])
            if isinstance(self.shared_account, CoordinatedBurstAccount):
                self.sync_fills(tp1)
        spec, settings = self.market_settings()
        available = self._addition_available(now_ms)
        quote = FuturesExecutor.quote(self, now_ms)
        sign = 1 if plan["direction"] == "long" else -1
        price = positive(quote["askPrice" if sign == 1 else "bidPrice"])
        stop = positive(json.loads(stop_row["params"])["triggerPrice"])
        if sign * (price - stop) <= 0 or sign * (Decimal(str(cursor["price"])) - stop) <= 0:
            return

        adverse = price * (1 + sign * Decimal(".0005"))
        distance = sign * (adverse - stop)
        notional_price = max(price, adverse)
        cap = min(positive(plan["notional_cap"]), positive(settings["maxNotionalValue"]))
        risk_cap = positive(plan["actual_initial_price_risk"]) * ADD_FRACTION
        rules = quantity_rules(spec)
        quantity = rounded(min(
            ADD_FRACTION * positive(plan["actual_entry_quantity"]), risk_cap / distance,
            cap / notional_price - expected, available * positive(settings["leverage"]) / notional_price,
            rules["max_qty"],
        ), rules["step"])
        if quantity < rules["min_qty"] or quantity * price < rules["min_notional"]:
            return

        # Recheck the exchange position and the exact stop immediately before opening risk.
        amount, other = self.position()
        if other or sign * amount != expected:
            return
        observed_stop = self.drive(stop_id)
        if (observed_stop["state"] != "NEW" or observed_stop["error"]
                or int(time.time() * 1000) > deadline):
            return
        with self.s.transaction():
            cid = self._market(session, "ADD", quantity)
            self.s.set_meta("position_addition_plan", canonical_json(dict(
                setup_id=session["id"], client_id=cid, stop=str(stop), risk_cap=str(risk_cap),
                notional_cap=str(cap), base_remaining=str(expected), expires_ms=deadline,
            )))
            self.s.audit("position_addition_prepared", dict(
                setup_id=session["id"], client_id=cid, quantity=str(quantity), stop=str(stop),
                trigger_agg_trade_id=trigger["agg_trade_id"], observed_agg_trade_id=cursor["agg_trade_id"],
            ))
        row = self.s.db.execute("SELECT * FROM orders WHERE client_id=?", (cid,)).fetchone()
        if int(time.time() * 1000) > deadline:
            self.cancel(row)
            return
        self.drive(cid)
        self._cycle_added(self.s.active(), now_ms)

    def _exit(self, session, quantity, reason):
        addition = self._addition_order(session)
        if addition is not None and addition["state"] not in TERMINAL:
            # Persist exit intent before trying to cancel a potentially in-flight add.
            with self.s.transaction():
                self.s.session_state(session["session"], "EXIT_PENDING", reason)
            self.cancel(addition)
        return super()._exit(session, quantity, reason)

    def _cycle_added(self, session, now_ms):
        if not self.v.armed:
            raise ValueError("active_position_requires_armed_venue")
        plan = json.loads(session["plan"])
        addition_plan = json.loads(self.s.meta("position_addition_plan", "null"))
        addition = self._addition_order(session)
        if (not addition_plan or addition_plan["setup_id"] != session["id"]
                or addition_plan["client_id"] != addition["client_id"]):
            raise ValueError("position_addition_plan_missing")

        for row in self.s.db.execute("SELECT * FROM orders WHERE session_id=?", (session["session"],)).fetchall():
            if row["state"] in TERMINAL:
                continue
            if row["role"] == "ADD" and (row["state"] == "PREPARED" or now_ms > addition_plan["expires_ms"]):
                self.cancel(row)
            elif row["state"] != "PREPARED" or row["role"] == "CLOSE":
                self.drive(row["client_id"])
        addition = self._addition_order(session)
        native = json.loads(addition["response"] or "{}")
        filled = number(native.get("executedQty", "0"))
        add_upper = filled if addition["state"] in TERMINAL else positive(json.loads(addition["params"])["quantity"])
        amount, other = self.position()
        sign = 1 if plan["direction"] == "long" else -1
        if other:
            self.block("unexpected_other_instrument_position")
        if sign * amount < 0 or abs(amount) > positive(plan["actual_entry_quantity"]) + add_upper:
            self.block("unexpected_position_direction_or_size")
            return
        if not amount:
            if addition["state"] not in TERMINAL:
                self.cancel(addition)
                return
            if not session["reason"]:
                stop = self.s.db.execute("""
                    SELECT 1 FROM orders WHERE session_id=? AND role='STOP'
                    AND CAST(json_extract(response, '$.actualOrderId') AS INTEGER)>0
                """, (session["session"],)).fetchone()
                reason = "trailing_stop" if stop is not None else "flat_after_position_addition"
                with self.s.transaction():
                    self.s.session_state(session["session"], "EXIT_PENDING", reason)
                session = self.s.active()
            self._cleanup(session, now_ms)
            self._record_closures()
            return
        if session["state"] == "EXIT_PENDING":
            return self._exit(session, amount, session["reason"])
        if now_ms >= plan["force_exit_ms"]:
            return self._exit(session, amount, "time_exit_48h")

        protection = self._trail_stop(session, plan, None)
        if protection == "unprotected":
            return self._exit(session, amount, "protective_stop_not_confirmed")
        if protection == "trigger_finished":
            return self._exit(session, amount, "stop_left_unclosed_quantity")
        if protection in {"triggering", "uncertain"}:
            return
        if filled:
            fill_price = positive(native["avgPrice"])
            distance = sign * (fill_price - Decimal(addition_plan["stop"]))
            if (distance <= 0 or filled * distance > Decimal(addition_plan["risk_cap"])
                    or (Decimal(addition_plan["base_remaining"]) + filled) * fill_price
                    > Decimal(addition_plan["notional_cap"])):
                return self._exit(session, amount, "actual_add_fill_exceeds_risk_or_notional_cap")

        trade = json.loads(self.s.meta("live_excursion", "null"))
        if trade and trade["setup_id"] == session["id"] and trade["timestamp_ms"] >= plan["first_fill_ms"]:
            protection = self._trail_stop(session, plan, trade["price"])
            if protection == "unprotected":
                self._exit(session, amount, "protective_stop_not_confirmed")
            elif protection == "trigger_finished":
                self._exit(session, amount, "stop_left_unclosed_quantity")
