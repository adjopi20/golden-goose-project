"""Durable USD-M shadow projection and read-only comparison with deployed evidence."""
from contextlib import ExitStack, closing, contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from time import time_ns


ACCOUNTS = {
    "burst": ("burst_hypeusdt_16h_f_p1_initiative_add", "burst_injusdt_24h_f_p15_initiative",
              "burst_uniusdc_12h_f_p10_initiative"),
    "quiet-core": ("quiet_breakout_stable", "quiet_breakout_ethusdc_stable",
                   "quiet_breakout_uniusdc_stable_position_addition"),
    "quiet-tail": ("quiet_breakout_tail_dependent_position_addition",),
}
CONTROLS = {"gap", "connected", "snapshot", "heartbeat"}
GRACE_MS = 15_000
RETENTION_MS = 2 * 86_400_000


def now_ms():
    return time_ns() // 1_000_000


def canonical(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def token(value, pattern=r"[A-Z0-9_]{1,32}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("private_event_field_invalid")
    return value


def native_id(value):
    return token(str(value), r"[1-9][0-9]{0,23}")


def number(value):
    if not re.fullmatch(r"-?[0-9]{1,30}(\.[0-9]{1,30})?", str(value)):
        raise ValueError("private_numeric_field_invalid")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("private_numeric_field_invalid") from None
    return amount


def stamp(value):
    if type(value) is not int or not 0 < value < 10**15:
        raise ValueError("private_timestamp_invalid")
    return value


def project_order(row):
    return {"symbol": token(row["symbol"]), "order_id": native_id(row["orderId"]),
            "client_id": token(row["clientOrderId"], r"[A-Za-z0-9._:/-]{1,36}"),
            "status": token(row["status"]), "filled": str(number(row["executedQty"])),
            "side": token(row["side"], r"BUY|SELL")}


def project_algo(row):
    out = {"symbol": token(row["symbol"]), "algo_id": native_id(row["algoId"]),
           "algo_client_id": token(row["clientAlgoId"], r"[A-Za-z0-9._:/-]{1,36}"),
           "status": token(row["algoStatus"])}
    child = row.get("actualOrderId")
    if child not in (None, "", "0", 0):
        out["child_order_id"] = native_id(child)
    return out


def project_position(row):
    return {"symbol": token(row["symbol"]), "position_side": token(row["positionSide"], r"BOTH|LONG|SHORT"),
            "quantity": str(number(row["positionAmt"]))}


def envelope(account, epoch, kind, venue_ms, received_ms, fields, transaction_ms=None):
    if account not in ACCOUNTS:
        raise ValueError("unknown_private_account")
    return {"schema": 1, "account": account, "epoch": token(epoch, r"[0-9a-f]{32}"),
            "kind": kind, "venue_ms": stamp(venue_ms) if venue_ms is not None else None,
            "transaction_ms": stamp(transaction_ms) if transaction_ms is not None else None,
            "received_ms": stamp(received_ms), "fields": fields}


def validate_fields(kind, fields):
    if kind == "snapshot":
        start, end = stamp(fields["started_ms"]), stamp(fields["through_ms"])
        if not start <= end:
            raise ValueError("private_snapshot_clock")
        selected = {"started_ms": start, "through_ms": end}
        for name, event in (("orders", "order"), ("algos", "algo"), ("positions", "position")):
            if not isinstance(fields[name], list):
                raise ValueError("private_snapshot_rows")
            selected[name] = [validate_fields(event, row) for row in fields[name]]
            keys = [key_for(event, row) for row in selected[name]]
            if len(keys) != len(set(keys)):
                raise ValueError("private_snapshot_duplicate")
    elif kind in CONTROLS:
        selected = {}
    elif kind == "order":
        selected = project_order(dict(symbol=fields["symbol"], orderId=fields["order_id"],
                                      clientOrderId=fields["client_id"], status=fields["status"],
                                      executedQty=fields["filled"], side=fields["side"]))
    elif kind == "algo":
        selected = project_algo(dict(symbol=fields["symbol"], algoId=fields["algo_id"],
                                     clientAlgoId=fields["algo_client_id"], algoStatus=fields["status"],
                                     actualOrderId=fields.get("child_order_id")))
    elif kind == "position":
        selected = project_position(dict(symbol=fields["symbol"], positionSide=fields["position_side"],
                                         positionAmt=fields["quantity"]))
    elif kind == "fill":
        selected = {"symbol": token(fields["symbol"]), "order_id": native_id(fields["order_id"]),
                    "trade_id": native_id(fields["trade_id"]), "trade_ms": stamp(fields["trade_ms"]), "side": token(fields["side"], r"BUY|SELL"),
                    "client_id": token(fields["client_id"], r"[A-Za-z0-9._:/-]{1,36}"),
                    "quantity": str(number(fields["quantity"])), "price": str(number(fields["price"]))}
        if number(selected["quantity"]) <= 0 or number(selected["price"]) <= 0:
            raise ValueError("private_fill_amount_invalid")
    elif kind == "balance":
        selected = {"asset": token(fields["asset"]), "wallet_balance": str(number(fields["wallet_balance"]))}
    elif kind == "trigger_reject":
        selected = {"symbol": token(fields["symbol"]), "order_id": native_id(fields["order_id"])}
    else:
        raise ValueError("private_kind_invalid")
    if selected != fields:
        raise ValueError("private_fields_not_allowlisted")
    return selected


def key_for(kind, fields):
    if kind == "balance":
        return fields["asset"], fields["asset"]
    attr = {"order": "order_id", "fill": "trade_id", "algo": "algo_id", "position": "position_side",
            "trigger_reject": "order_id"}[kind]
    return fields["symbol"], fields[attr]


@contextmanager
def readonly(path):
    # Never create an evidence database or take a write transaction.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
        db.execute("PRAGMA query_only=ON")
        yield db


def binding(observed, account):
    with readonly(observed / "portfolio-accounts.sqlite") as db:
        row = db.execute("SELECT uid_hash FROM account_uids WHERE name=?", (account,)).fetchone()
    if row is None:
        raise ValueError("private_account_binding_missing")
    return token(row[0], r"[0-9a-f]{64}")


def audit_path(observed, account):
    return observed / ("binance-broker-audit" + ("" if account == "burst" else "-" + account) + ".sqlite")


class Evidence:
    def __init__(self, stack, directory, account, uid_hash):
        self.workers = []
        keys = set()
        symbols = set()
        for name in ACCOUNTS[account]:
            db = stack.enter_context(readonly(directory / (name + ".sqlite")))
            meta = dict(db.execute("SELECT key,value FROM metadata WHERE key IN "
                                   "('owner','portfolio_account_uid','key_hash','worker_symbol')"))
            if meta.get("owner") != "binance_futures_pilot" or meta.get("portfolio_account_uid") != uid_hash:
                raise ValueError("private_journal_identity_mismatch")
            keys.add(token(meta["key_hash"], r"[0-9a-f]{64}"))
            symbol = token(meta["worker_symbol"])
            symbols.add(symbol)
            self.workers.append((symbol, db))
        self.audit = stack.enter_context(readonly(audit_path(directory, account)))
        self.audit.execute("BEGIN")
        meta = dict(self.audit.execute("SELECT key,value FROM metadata"))
        if (meta.get("owner") != "binance_broker_audit" or keys != {meta.get("key_hash")}
                or symbols != set(meta.get("symbols", "").split(","))):
            raise ValueError("private_audit_identity_mismatch")
        self.through = int(meta["coverage_through_ms"])
        self.start = int(meta["coverage_from_ms"])
        self.symbols = symbols

    def native(self, kind, symbol, identity):
        for payload, in self.audit.execute(
                """SELECT payload FROM events WHERE kind=? AND symbol=? AND native_id=?
                   ORDER BY observed_ms DESC, payload""", (kind, symbol, identity)):
            yield json.loads(payload)

    def attribution(self, symbol, fields):
        worker = next((db for s, db in self.workers if s == symbol), None)
        if worker is None:
            return None, None
        local = worker.execute("SELECT order_id,quantity,price FROM fills WHERE id=?",
                               (symbol + ":" + fields["trade_id"],)).fetchone()
        # A conditional child can have its own client ID. Use the journal's
        # native actualOrderId to attribute it to a journaled algo order.
        orders = worker.execute("""SELECT client_id,role,response FROM orders WHERE client_id=?
            OR CAST(json_extract(response, '$.orderId') AS TEXT)=?
            OR CAST(json_extract(response, '$.actualOrderId') AS TEXT)=?""",
                                (fields["client_id"], fields["order_id"], fields["order_id"])).fetchall()
        return local, orders

    def snapshot(self, kind, project):
        row = self.audit.execute("SELECT payload FROM events WHERE kind=? "
                                 "ORDER BY observed_ms DESC LIMIT 1", (kind,)).fetchone()
        return None if row is None else [
            r for r in map(project, json.loads(row[0])) if r["symbol"] in self.symbols]


class ShadowStore:
    def __init__(self, path, account, uid_hash, *, clock=now_ms):
        self.account = account
        self.uid_hash = uid_hash
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=5, isolation_level="IMMEDIATE")
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(seq INTEGER PRIMARY KEY, digest TEXT, created INTEGER);
                CREATE TABLE IF NOT EXISTS events(
                    kind TEXT, symbol TEXT, entity TEXT, revision INTEGER, digest TEXT, fields TEXT,
                    epoch TEXT, received INTEGER, PRIMARY KEY(kind,symbol,entity,revision));
                CREATE TABLE IF NOT EXISTS facts(
                    kind TEXT, symbol TEXT, entity TEXT, revision INTEGER, fields TEXT,
                    PRIMARY KEY(kind,symbol,entity));
                CREATE TABLE IF NOT EXISTS findings(
                    identity TEXT PRIMARY KEY, first_evidence INTEGER, last_evidence INTEGER,
                    status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS private_event_age ON events(received);
            """)
            with self.db:
                if self.get("account") not in (None, account) or self.get("uid_hash") not in (None, uid_hash):
                    raise ValueError("shadow_database_identity_mismatch")
                self.put("account", account)
                self.put("uid_hash", uid_hash)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, str(value)))

    def bind_stream(self, generation):
        with self.db:
            if self.get("generation") not in (None, generation):
                self.put("coverage", "broker_generation_changed")
                conflict = True
            else:
                conflict = False
                self.put("generation", generation)
        if conflict:
            raise ValueError("shadow_stream_generation_changed")

    def apply(self, payload, sequence, pending):
        if not 0 < sequence or not 0 <= pending or len(payload) > 384 * 1024:
            raise ValueError("shadow_delivery_invalid")
        raw = json.loads(payload)
        kind, fields = raw["kind"], raw["fields"]
        event = envelope(raw["account"], raw["epoch"], kind, raw["venue_ms"], raw["received_ms"],
                         validate_fields(kind, fields), raw["transaction_ms"])
        if (raw != event or event["account"] != self.account
                or (kind == "fill" and fields["trade_ms"] != event["transaction_ms"])
                or (kind in CONTROLS) != (event["venue_ms"] is None)
                or (kind in CONTROLS) != (event["transaction_ms"] is None)):
            raise ValueError("shadow_envelope_invalid")
        digest = hashlib.sha256(canonical(event)).hexdigest()
        with self.db:
            row = self.db.execute("SELECT digest FROM receipts WHERE seq=?", (sequence,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("shadow_delivery_conflict")
                return  # Redelivery after commit, before acknowledgement.
            prev = int(self.get("sequence", 0))
            if sequence <= prev:
                raise ValueError("shadow_unknown_old_delivery")
            now = self.clock()
            if sequence != prev + 1:
                self.put("coverage", "broker_gap")
                self.put("gap_count", int(self.get("gap_count", 0)) + 1)
            self._apply_event(event)
            self.db.execute("INSERT INTO receipts VALUES (?,?,?)", (sequence, digest, now))
            self.put("sequence", sequence)
            self.put("last_processed_ms", now)
            self.put("last_backlog", pending)
            self.put("last_delivery_lag_ms", max(0, now - event["received_ms"]))
            if event["venue_ms"] is not None:
                self.put("last_venue_lag_ms", max(0, event["received_ms"] - event["venue_ms"]))

    def quarantine(self, payload, sequence, pending):
        if not 0 < sequence or not 0 <= pending:
            raise ValueError("shadow_delivery_invalid")
        # Advance only with a durable receipt. Keep the digest instead of raw data.
        digest = hashlib.sha256(b"invalid:" + payload).hexdigest()
        with self.db:
            row = self.db.execute("SELECT digest FROM receipts WHERE seq=?", (sequence,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("shadow_delivery_conflict")
                return
            prev = int(self.get("sequence", 0))
            if sequence <= prev:
                raise ValueError("shadow_unknown_old_delivery")
            if sequence != prev + 1:
                self.put("gap_count", int(self.get("gap_count", 0)) + 1)
            now = self.clock()
            self.db.execute("INSERT INTO receipts VALUES (?,?,?)", (sequence, digest, now))
            self.put("coverage", "invalid_event")
            self.put("invalid_events", int(self.get("invalid_events", 0)) + 1)
            self.put("sequence", sequence)
            self.put("last_processed_ms", now)
            self.put("last_backlog", pending)

    def _apply_event(self, event):
        kind, fields, epoch = event["kind"], event["fields"], event["epoch"]
        received = event["received_ms"]
        if kind == "gap":
            self.put("epoch", epoch)
            self.put("connection", "down")
            self.put("coverage", "venue_gap")
            self.put("baseline_through", 0)
            self.put("snapshot_overlap", 0)
            self.put("heartbeat_ms", 0)
            return
        if epoch != self.get("epoch"):
            self.put("coverage", "unexpected_epoch")
            return
        if kind == "connected":
            self.put("connection", "up")
            return
        if kind == "heartbeat":
            self.put("heartbeat_ms", received)
            return
        if kind == "snapshot":
            if self.get("connection") != "up":
                self.put("coverage", "snapshot_without_connection")
                return
            end = fields["through_ms"]
            self.put("baseline_start", fields["started_ms"])
            self.put("baseline_through", end)
            self.put("snapshot_overlap", 0)
            self.db.execute("DELETE FROM facts")
            for name, category in (("orders", "order"), ("algos", "algo"), ("positions", "position")):
                for item in fields[name]:
                    self._fact(category, *key_for(category, item), end, item)
            self.put("coverage", "snapshot_baseline")
            return

        symbol, identity = key_for(kind, fields)
        revision = event["transaction_ms"]
        dedupe_revision = 0 if kind == "fill" else revision
        encoded = canonical(fields)
        digest = hashlib.sha256(encoded).hexdigest()
        existing = self.db.execute(
            "SELECT digest FROM events WHERE kind=? AND symbol=? AND entity=? AND revision=?",
            (kind, symbol, identity, dedupe_revision)).fetchone()
        if existing and existing[0] != digest:
            self.put("coverage", "conflicting_event")
            self.put("conflicts", int(self.get("conflicts", 0)) + 1)
            return
        if not existing:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                            (kind, symbol, identity, dedupe_revision, digest, encoded.decode(), epoch, received))
        if kind in ("fill", "trigger_reject"):
            return  # Neither an algo status nor a rejection creates a synthetic fill.

        end = int(self.get("baseline_through", 0))
        if not end:
            return  # Evidence is retained, but no baseline exists to update.
        if revision <= end:
            if revision >= int(self.get("baseline_start", end)):
                self.put("snapshot_overlap", 1)
            return
        self._fact(kind, symbol, identity, revision, fields)

    def _fact(self, kind, symbol, identity, revision, fields):
        self.db.execute("""INSERT INTO facts VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET
                          revision=excluded.revision, fields=excluded.fields
                          WHERE excluded.revision>facts.revision""",
                        (kind, symbol, identity, revision, canonical(fields).decode()))

    def _verdict(self, key, finding, checkpoint, counts):
        row = self.db.execute("SELECT first_evidence,last_evidence,status FROM findings WHERE identity=?",
                              (key,)).fetchone()
        first = row[0] if row and row[2] in ("timing", "persistent") and finding == "difference" else checkpoint
        status = finding
        if finding == "difference":
            status = "persistent" if checkpoint - first >= 120_000 else "timing"
        self.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?)", (key, first, checkpoint, status))
        counts[status] = counts.get(status, 0) + 1

    def health(self, *, backlog=None):
        now = self.clock()
        heartbeat = int(self.get("heartbeat_ms", 0))
        queue = int(self.get("last_backlog", 0)) if backlog is None else backlog
        coverage = self.get("coverage", "no_baseline")
        return {"connection_alive": self.get("connection") == "up" and 0 <= now - heartbeat < 60_000,
                "consumer_backlog": queue, "coverage": coverage,
                "snapshot_overlap": bool(int(self.get("snapshot_overlap", 0))),
                "broker_gaps": int(self.get("gap_count", 0)),
                "conflicts": int(self.get("conflicts", 0)),
                "invalid_events": int(self.get("invalid_events", 0)),
                "last_delivery_lag_ms": int(self.get("last_delivery_lag_ms", 0)),
                "last_venue_lag_ms": self.get("last_venue_lag_ms")}

    def compare(self, directory, *, backlog=None):
        health = self.health(backlog=backlog)
        now = self.clock()
        counts = {}
        if now - int(self.get("pruned_ms", 0)) > 300_000:
            with self.db:
                self.db.execute("DELETE FROM receipts WHERE created<?", (now - RETENTION_MS,))
                self.db.execute("DELETE FROM events WHERE received<?", (now - RETENTION_MS,))
                self.db.execute("DELETE FROM findings WHERE last_evidence<?", (now - RETENTION_MS,))
                self.put("pruned_ms", now)
        with ExitStack() as stack:
            try:
                evidence = Evidence(stack, directory, self.account, self.uid_hash)
                if not 0 <= now - evidence.through < 300_000:
                    return {**health, "comparison": "evidence_stale", "state_verification": "incomplete"}
                with self.db:
                    verification = self._compare_facts(evidence, health, counts)
                    self._compare_fills(evidence, health, counts, now)
            except (ValueError, sqlite3.Error, KeyError, TypeError):
                return {**health, "comparison": "evidence_unavailable_or_unverified", "state_verification": "incomplete"}
        return {**health, "comparison": "evaluated", "state_verification": verification, **counts}

    def _compare_facts(self, evidence, health, counts):
        baseline = int(self.get("baseline_through", 0))
        trustworthy = (health["connection_alive"] and not health["consumer_backlog"]
                       and health["coverage"] == "snapshot_baseline" and not health["snapshot_overlap"]
                       and evidence.through >= baseline + GRACE_MS)
        if not trustworthy:
            return "incomplete"
        through = evidence.through
        rows = evidence.snapshot("positions", project_position)
        positions = None if rows is None else {
            key_for("position", p): number(p["quantity"]) for p in rows}
        state_counts = {}
        for kind, symbol, identity, revision, payload in self.db.execute(
                "SELECT kind,symbol,entity,revision,fields FROM facts WHERE kind IN ('position','order','algo')"):
            if symbol not in evidence.symbols:
                continue
            fields = json.loads(payload)
            candidates = list(evidence.native(kind, symbol, identity)) if kind != "position" else []
            if kind == "position":
                if positions is None:
                    continue
                expected = positions.pop((symbol, identity), Decimal(0))
                if through < revision + GRACE_MS:
                    verdict = "timing"
                else:
                    verdict = "match" if number(fields["quantity"]) == expected else "difference"
            elif through < revision + GRACE_MS:
                verdict = "timing"
            else:
                newest = max(candidates, key=lambda x: x.get("updateTime", 0), default=None)
                if newest is None or newest.get("updateTime") != revision:
                    # Historical status may legitimately differ. Only compare corresponding revisions.
                    verdict = "order_revision_unverified"
                else:
                    ref = project_order(newest) if kind == "order" else project_algo(newest)
                    if kind == "order":
                        ref["filled"] = number(ref["filled"])
                        fields["filled"] = number(fields["filled"])
                    verdict = "match" if ref == fields else "difference"
            self._verdict(f"{kind}:{symbol}:{identity}", verdict, through,
                          state_counts if kind == "position" else counts)
        for (symbol, identity), qty in (positions or {}).items():
            if qty:
                self._verdict(f"position:{symbol}:{identity}", "difference", through, state_counts)
        order_snapshots = (("order", "open_orders", project_order, {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}),
                           ("algo", "open_algos", project_algo, {"NEW", "TRIGGERING", "TRIGGERED"}))
        for kind, name, project, statuses in order_snapshots:
            venue = evidence.snapshot(name, project)
            if venue is None:
                state_counts["snapshot_missing"] = state_counts.get("snapshot_missing", 0) + 1
                continue
            local_rows = [(rev, json.loads(payload)) for sym, rev, payload in self.db.execute(
                "SELECT symbol,revision,fields FROM facts WHERE kind=?", (kind,)) if sym in evidence.symbols]
            if any(rev + GRACE_MS > through for rev, _ in local_rows):
                verdict = "timing"
            else:
                def comparable(items):
                    return {key_for(kind, r): {**r, **({"filled": number(r["filled"])} if kind == "order" else {})}
                            for r in items}
                verdict = "match" if comparable(r for _, r in local_rows if r["status"] in statuses) == (
                    comparable(venue)) else "difference"
            self._verdict(f"snapshot:{kind}", verdict, through, state_counts)
        for label, total in state_counts.items():
            counts["state_" + label] = total
        if rows is None or state_counts.get("snapshot_missing"):
            return "incomplete"
        if state_counts.get("persistent"):
            return "mismatch"
        if state_counts.get("timing"):
            return "timing"
        return "matched"

    def _compare_fills(self, evidence, health, counts, now):
        through = evidence.through
        for symbol, identity, payload in self.db.execute(
                "SELECT symbol,entity,fields FROM events WHERE kind='fill' AND received>=?",
                (now - RETENTION_MS,)):
            fields = json.loads(payload)
            native = next(evidence.native("trade", symbol, identity), None)
            local, owners = evidence.attribution(symbol, fields)
            if fields["trade_ms"] < evidence.start:
                verdict = "coverage_unverified"
            elif through < fields["trade_ms"] + GRACE_MS:
                verdict = "timing"
            elif native is None:
                verdict = "difference"
            elif (str(native["orderId"]), native["side"], number(native["qty"]), number(native["price"]),
                  native["time"]) != (fields["order_id"], fields["side"], number(fields["quantity"]),
                                      number(fields["price"]), fields["trade_ms"]):
                verdict = "difference"
            elif local is None:
                verdict = "difference" if owners else "external_or_unattributed"
            elif (str(local[0]), number(local[1]), number(local[2])) != (
                    fields["order_id"], number(fields["quantity"]), number(fields["price"])):
                verdict = "difference"
            else:
                verdict = "match" if len(owners) == 1 else "attribution_unverified"
            self._verdict(f"fill:{symbol}:{identity}", verdict, through, counts)
        # REST-only trades demonstrate observed coverage gaps. They do not recreate
        # events or establish that earlier connection epochs had complete coverage.
        after = int(self.get("baseline_through", 0))
        if after and through > after + GRACE_MS:
            for symbol, identity, payload in evidence.audit.execute(
                    "SELECT DISTINCT symbol,native_id,payload FROM events WHERE kind='trade' AND observed_ms>=?",
                    (after,)):
                fill = json.loads(payload)
                if not after < fill["time"] < through - GRACE_MS:
                    continue
                seen = self.db.execute("SELECT 1 FROM events WHERE kind='fill' AND symbol=? AND entity=?",
                                       (symbol, identity)).fetchone()
                if not seen:
                    verdict = "difference" if health["coverage"] == "snapshot_baseline" and (
                        health["connection_alive"] and not health["consumer_backlog"]) else "coverage_unverified"
                    self._verdict(f"missing_stream_fill:{symbol}:{identity}", verdict, through, counts)
