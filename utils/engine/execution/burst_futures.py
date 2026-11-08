"""HYPE Burst rm-c lifecycle using the native Futures order journal."""
from decimal import Decimal
import hashlib
import json

from utils.engine.contracts import canonical_json
from utils.engine.execution.futures_preflight import number, one, positive, quantity_rules
from utils.engine.execution.preny_futures import FuturesExecutor, TERMINAL, order_id, rounded
from utils.engine.state.futures import FuturesStore

DAY = 86_400_000
HOLD_MS = 2 * DAY
ACTIVE_STOPS = {"NEW", "TRIGGERING", "TRIGGERED"}


def plan_entry(intention, spec, quote, equity, available, settings):
    rules = quantity_rules(spec)
    long = intention["direction"] == "long"
    entry = positive(quote["askPrice"] if long else quote["bidPrice"])
    stop = rounded(positive(intention["initial_stop"]), rules["tick_size"], up=not long)
    sign = 1 if long else -1
    sizing_entry = entry * (1 + sign * Decimal("0.0005"))
    distance = (sizing_entry - stop) * sign
    if stop <= 0 or distance <= 0:
        raise ValueError("poc_not_behind_executable_entry")
    budget = positive(equity) * Decimal("0.01")
    leverage = positive(settings["leverage"])
    cap = min(equity * 5, positive(settings["maxNotionalValue"]), min(available, equity) * leverage)
    qty = rounded(min(budget / distance, cap / max(sizing_entry, entry), rules["max_qty"]), rules["step"])
    if qty < rules["min_qty"] or qty * entry < rules["min_notional"]:
        raise ValueError("exchange_minimum_exceeds_risk_or_margin_budget")
    return dict(direction=intention["direction"], entry_reference=str(entry), stop=str(stop),
                quantity=str(qty), tick_size=str(rules["tick_size"]), risk_budget=str(budget),
                planned_price_risk=str(qty * distance), notional=str(qty * entry),
                notional_cap=str(cap))


class BurstFuturesStore(FuturesStore):
    def equity(self, initial):
        result = Decimal(str(initial))
        for row in self.db.execute("SELECT realized,commission,commission_asset FROM fills"):
            if row["commission_asset"] != "USDT" and Decimal(row["commission"]):
                raise ValueError("commission_asset_requires_review")
            result += Decimal(row["realized"]) - Decimal(row["commission"])
        return result + sum((Decimal(row[0]) for row in self.db.execute("SELECT amount FROM funding")), Decimal(0))


class BurstFuturesExecutor(FuturesExecutor):
    """Dedicated one-way HYPE account, stop ladder and full exit 48h after fill."""

    def offer(self, signal, now_ms):
        if signal["direction"] not in ("long", "short") or not isinstance(signal["sample_id"], str):
            raise ValueError("invalid_burst_intention")
        identity = signal["sample_id"]
        start = signal["eligible_timestamp_ms"]
        end = signal["expires_timestamp_ms"]
        if not start <= end <= start + 60_000:
            raise ValueError("invalid_entry_validity_window")
        with self.s.transaction():
            if self.s.db.execute("SELECT 1 FROM sessions WHERE session=?", (identity,)).fetchone():
                return
            if not self.v.armed:
                state, reason = "DISARMED", "execution_disabled"
            elif not self.c["accept_new_entries"] or self.s.meta("entries_blocked"):
                state, reason = "SKIPPED", "entries_paused"
            elif not start <= now_ms <= end:
                state, reason = "SKIPPED", "stale_intention"
            elif self.s.active():
                state, reason = "SKIPPED", "position_or_entry_pending"
            elif self.s.meta("last_entry_day") == str(now_ms // DAY):
                state, reason = "SKIPPED", "one_entry_per_utc_day"
            else:
                state, reason = "PENDING", "flow_confirmed"
            self.s.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                              (identity, identity, canonical_json(signal), None, state,
                               None if state == "PENDING" else reason))
            self.s.audit("entry_decision", dict(setup_id=identity, state=state, reason=reason, signal=signal))

    def wallet(self):
        asset = one(self.v.read("/fapi/v3/account").get("assets"), "asset", "USDT")
        return number(asset["walletBalance"], signed=True), number(asset["availableBalance"])

    def entry_guard(self, now_ms):
        cfg = self.v.read("/fapi/v1/accountConfig")
        if cfg.get("canTrade") is not True or cfg.get("dualSidePosition") is not False or cfg.get("multiAssetsMargin") is not False:
            raise ValueError("requires_one_way_single_asset_futures_account")
        amount, other = self.position()
        if amount or other or self.v.read("/fapi/v1/openOrders") or self.v.read("/fapi/v1/openAlgoOrders"):
            raise ValueError("dedicated_futures_account_not_flat")
        self.v.load_credentials()
        key_hash = hashlib.sha256(self.v._key.encode()).hexdigest()
        if self.s.meta("key_hash", key_hash) != key_hash:
            raise ValueError("api_identity_changed")
        wallet, available = self.wallet()
        if self.s.meta("initial_wallet") is None:
            if available < positive(self.c["initial_equity"]):
                raise ValueError("insufficient_USDT_for_approved_allocation")
            with self.s.transaction():
                self.s.set_meta("key_hash", key_hash)
                self.s.set_meta("initial_wallet", wallet)
                self.s.set_meta("funding_start_ms", now_ms)
        self.accounting(now_ms)
        settings = one(self.v.read("/fapi/v1/symbolConfig", symbol=self.symbol), "symbol", self.symbol)
        if settings.get("marginType") != "ISOLATED" or settings.get("isAutoAddMargin") is not False:
            raise ValueError("requires_isolated_margin_with_auto_add_off")
        if type(settings.get("leverage")) is not int or settings["leverage"] <= 0:
            raise ValueError("invalid_configured_leverage")
        spec = one(self.v.get("/fapi/v1/exchangeInfo").get("symbols"), "symbol", self.symbol)
        if (spec.get("status"), spec.get("contractType"), spec.get("marginAsset")) != ("TRADING", "PERPETUAL", "USDT"):
            raise ValueError("HYPEUSDT_contract_unavailable")
        if any(r.get("filterType") == "POSITION_RISK_CONTROL" and r.get("positionControlSide") != "NONE" for r in spec["filters"]):
            raise ValueError("exchange_position_restriction")
        return spec, available, settings

    def accounting(self, now_ms):
        if self.s.meta("initial_wallet") is None:
            return
        start = max(int(self.s.meta("funding_start_ms")), int(self.s.meta("funding_scan_ms", now_ms)) - DAY)
        while start <= now_ms:
            end = min(start + 7 * DAY - 1, now_ms)
            rows = self.v.read("/fapi/v1/income", symbol=self.symbol, incomeType="FUNDING_FEE",
                               startTime=start, endTime=end, limit=1000)
            if not isinstance(rows, list) or len(rows) >= 1000:
                raise ValueError("incomplete_funding_history")
            with self.s.transaction():
                for row in rows:
                    if (row.get("symbol"), row.get("asset"), row.get("incomeType")) != (self.symbol, "USDT", "FUNDING_FEE"):
                        raise ValueError("unexpected_funding_record")
                    identity = "funding:" + str(row["tranId"])
                    payload = canonical_json(row)
                    old = self.s.db.execute("SELECT payload FROM funding WHERE id=?", (identity,)).fetchone()
                    if old and old[0] != payload:
                        raise ValueError("conflicting_funding_record")
                    self.s.db.execute("INSERT OR IGNORE INTO funding VALUES (?,?,?)",
                                      (identity, str(number(row["income"], signed=True)), payload))
            start = end + 1
        wallet, _ = self.wallet()
        expected = number(self.s.meta("initial_wallet")) + self.s.equity(self.c["initial_equity"]) - positive(self.c["initial_equity"])
        if abs(wallet - expected) > Decimal(".00000001"):
            raise ValueError("wallet_fill_funding_mismatch")
        with self.s.transaction():
            self.s.set_meta("funding_scan_ms", now_ms)

    def _trail_stop(self, session, plan, price):
        side = 1 if plan["direction"] == "long" else -1
        desired = -1
        if price is not None and "actual_entry_price" in plan:
            entry = positive(plan["actual_entry_price"])
            distance = abs(entry - positive(plan["stop"]))
            reached = side * (positive(price) - entry) / distance
            milestone = 1
            while reached >= milestone:
                desired = 0 if milestone == 1 else milestone // 2
                milestone *= 2
        stop_rows = self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='STOP' ORDER BY rowid",
                                      (session["session"],)).fetchall()
        if any(row["state"] == "FINISHED" for row in stop_rows):
            return "trigger_finished"
        if any(row["state"] in {"TRIGGERING", "TRIGGERED"} for row in stop_rows):
            return "triggering"
        lock = int(plan.get("risk_lock_r", -1))
        confirmed = [int(row["client_id"].rsplit("-", 1)[1]) for row in stop_rows
                     if row["state"] == "NEW" and not row["client_id"].endswith("-initial")]
        desired = max(desired, lock, *confirmed) if confirmed else max(desired, lock)
        if not stop_rows:
            desired = -1
        trigger = positive(plan["stop"])
        if desired >= 0:
            entry = positive(plan["actual_entry_price"])
            distance = abs(entry - trigger)
            trigger = rounded(entry + side * desired * distance, positive(plan["tick_size"]), up=side < 0)
        suffix = "initial" if desired == -1 else str(desired)
        cid = order_id(session["id"], "sl-" + suffix)
        row = next((row for row in stop_rows if row["client_id"] == cid), None)
        if row is None and any(row["state"] in {"SENDING", "UNKNOWN"} for row in stop_rows):
            # Resolve an earlier submission before adding another native stop.
            return "protected" if any(row["state"] == "NEW" for row in stop_rows) else "unprotected"
        if row is None:
            params = dict(algoType="CONDITIONAL", symbol=self.symbol, side="SELL" if side > 0 else "BUY",
                          positionSide="BOTH", type="STOP_MARKET", triggerPrice=str(trigger), workingType="CONTRACT_PRICE",
                          closePosition="true", priceProtect="false", clientAlgoId=cid)
            row = self.drive(self._prepare(session["session"], "STOP", params))
        elif row["state"] == "PREPARED":
            row = self.drive(cid)
        if row["state"] == "FINISHED":
            return "trigger_finished"
        if row["state"] in {"TRIGGERING", "TRIGGERED", "FINISHED"}:
            return "triggering"
        if row["state"] != "NEW":
            if any(other["state"] == "NEW" and other["client_id"] != cid for other in stop_rows):
                return "protected"
            return "unprotected"
        if desired > lock:
            with self.s.transaction():
                plan["risk_lock_r"] = desired
                self.s.db.execute("UPDATE sessions SET plan=? WHERE session=?",
                                  (canonical_json(plan), session["session"]))
                self.s.audit("stop_raised", dict(setup_id=session["id"], stop=str(trigger), locked_r=desired,
                                                 native_client_id=cid))
        # Both close-position stops can coexist until the tighter one is confirmed.
        for other in stop_rows:
            if other["client_id"] != cid and other["state"] == "NEW":
                self.cancel(other)
        return "protected"

    def _skip(self, session, reason):
        with self.s.transaction():
            self.s.session_state(session["session"], "SKIPPED", reason)
            self.s.audit("entry_skipped", dict(setup_id=session["id"], reason=reason))

    def _record_closures(self):
        for session in self.s.db.execute("""
            SELECT * FROM sessions WHERE state='CLOSED' AND NOT EXISTS (
                SELECT 1 FROM audit WHERE kind='trade_closed' AND json_extract(payload, '$.setup_id')=sessions.id
            ) ORDER BY rowid
        """).fetchall():
            self._record_closed(session)

    def _record_closed(self, session):
        fills = self.s.db.execute("SELECT * FROM fills WHERE session_id=?", (session["session"],)).fetchall()
        plan = json.loads(session["plan"])
        gross = sum((Decimal(f["realized"]) for f in fills), Decimal(0))
        fees = sum((Decimal(f["commission"]) for f in fills), Decimal(0))
        funding = sum((Decimal(f["amount"]) for f in self.s.db.execute(
            "SELECT amount,payload FROM funding WHERE json_extract(payload, '$.time') BETWEEN ? AND ?",
            (min(json.loads(f["payload"])["time"] for f in fills),
             max(json.loads(f["payload"])["time"] for f in fills)))), Decimal(0))
        first_fill_day = min(json.loads(f["payload"])["time"] for f in fills) // DAY
        with self.s.transaction():
            if first_fill_day > int(self.s.meta("last_entry_day", "-1")):
                self.s.set_meta("last_entry_day", first_fill_day)
            self.s.audit("trade_closed", dict(setup_id=session["id"], reason=session["reason"],
                        pnl_asset="USDT", realized=str(gross), fees=str(fees), funding=str(funding),
                        net_pnl=str(gross-fees+funding), fill_ids=[f["id"] for f in fills],
                        plan=plan, final_equity=str(self.s.equity(self.c["initial_equity"]))))

    def _cycle(self, now_ms, *, new_entries_ok=True):
        session = self.s.active()
        if session is None:
            self._record_closures()
            return
        if not self.v.armed:
            raise ValueError("active_position_requires_armed_venue")
        p = json.loads(session["intention"])
        if session["state"] == "PENDING":
            if now_ms < p["eligible_timestamp_ms"]:
                return
            if now_ms > p["expires_timestamp_ms"] or not new_entries_ok or self.s.meta("entries_blocked"):
                return self._skip(session, "stale_or_entries_paused")
            if self.s.meta("last_entry_day") == str(now_ms // DAY):
                return self._skip(session, "one_entry_per_utc_day")
            try:
                spec, available, settings = self.entry_guard(now_ms)
                plan = plan_entry(p, spec, self.quote(now_ms), self.s.equity(self.c["initial_equity"]), available, settings)
            except ValueError as error:
                return self._skip(session, str(error))
            with self.s.transaction():
                self.s.db.execute("UPDATE sessions SET plan=?,state='ENTRY_PENDING' WHERE session=?",
                                  (canonical_json(plan), session["session"]))
                cid = self._market(session, "ENTRY", positive(plan["quantity"]))
                self.s.audit("entry_sized", dict(setup_id=session["id"], **plan))
            self.drive(cid)
            session = self.s.active()
        plan = json.loads(session["plan"])
        orders = self.s.db.execute("SELECT * FROM orders WHERE session_id=?", (session["session"],)).fetchall()
        for row in orders:
            if row["state"] in TERMINAL:
                continue
            if row["role"] == "ENTRY" and row["state"] == "PREPARED" and not new_entries_ok:
                self.cancel(row)
            elif row["state"] != "PREPARED" or row["role"] in {"ENTRY", "CLOSE"}:
                self.drive(row["client_id"])
        entry = self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='ENTRY'", (session["session"],)).fetchone()
        amount, other = self.position()
        sign = 1 if plan["direction"] == "long" else -1
        if other:
            self.block("unexpected_other_instrument_position")
        if amount and (amount * sign < 0 or abs(amount) > positive(plan["quantity"])):
            self.block("unexpected_position_direction_or_size")
            return
        if not amount:
            if entry["state"] not in TERMINAL and now_ms > p["expires_timestamp_ms"]:
                self.cancel(entry)
                entry = self.s.db.execute("SELECT * FROM orders WHERE client_id=?", (entry["client_id"],)).fetchone()
            if entry["state"] in TERMINAL:
                if not session["reason"]:
                    stop = self.s.db.execute("""
                        SELECT client_id FROM orders WHERE session_id=? AND role='STOP'
                        AND CAST(json_extract(response, '$.actualOrderId') AS INTEGER)>0 ORDER BY rowid DESC LIMIT 1
                    """, (session["session"],)).fetchone()
                    if stop is not None:
                        reason = "initial_stop" if stop["client_id"].endswith("-initial") else "trailing_stop"
                        with self.s.transaction():
                            self.s.session_state(session["session"], "EXIT_PENDING", reason)
                        session = self.s.active()
                self._cleanup(session, now_ms)
                self._record_closures()
            return
        if session["state"] == "EXIT_PENDING":
            return self._exit(session, amount, session["reason"])
        if "force_exit_ms" in plan and now_ms >= plan["force_exit_ms"]:
            return self._exit(session, amount, "time_exit_48h")
        protection = self._trail_stop(session, plan, None)
        if protection == "unprotected":
            return self._exit(session, amount, "protective_stop_not_confirmed")
        if protection == "trigger_finished":
            return self._exit(session, amount, "stop_left_unclosed_quantity")
        if protection == "triggering":
            return
        if entry["state"] not in TERMINAL:
            if now_ms > p["expires_timestamp_ms"]:
                self.cancel(entry)
            return
        self.sync_fills(entry)
        native = json.loads(entry["response"] or "{}")
        oid = str(native.get("orderId", ""))
        fills = [json.loads(row["payload"]) for row in self.s.db.execute("SELECT payload FROM fills WHERE order_id=?", (oid,))]
        qty = sum((positive(fill["qty"]) for fill in fills), Decimal(0))
        if qty != number(native.get("executedQty", "0")) or not fills:
            return
        if "actual_entry_price" not in plan:
            price = sum((positive(f["qty"])*positive(f["price"]) for f in fills), Decimal(0)) / qty
            first = min(f["time"] for f in fills)
            risk = qty * abs(price - positive(plan["stop"]))
            plan.update(actual_entry_price=str(price), actual_entry_quantity=str(qty), first_fill_ms=first,
                        force_exit_ms=first+HOLD_MS, actual_initial_price_risk=str(risk),
                        entry_slippage_bps=str(sign*(price/positive(plan["entry_reference"])-1)*10_000))
            with self.s.transaction():
                self.s.db.execute("UPDATE sessions SET plan=? WHERE session=?", (canonical_json(plan), session["session"]))
                self.s.session_state(session["session"], "OPEN")
                self.s.set_meta("last_entry_day", first // DAY)
                self.s.audit("position_open", dict(setup_id=session["id"], **plan))
            if sign*(price-positive(plan["stop"])) <= 0 or risk > positive(plan["risk_budget"]) or qty*price > positive(plan["notional_cap"]):
                return self._exit(session, amount, "actual_fill_exceeds_initial_stop_risk")
        if now_ms >= plan["force_exit_ms"]:
            return self._exit(session, amount, "time_exit_48h")
        mark = self.s.meta("live_excursion")
        if mark:
            trade = json.loads(mark)
            if trade["setup_id"] == session["id"] and trade["timestamp_ms"] >= plan["first_fill_ms"]:
                protection = self._trail_stop(session, plan, trade["price"])
                if protection == "unprotected":
                    self._exit(session, amount, "protective_stop_not_confirmed")
                elif protection == "trigger_finished":
                    self._exit(session, amount, "stop_left_unclosed_quantity")
