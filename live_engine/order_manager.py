"""Durable paper execution, delegating all fill rules to the shared backtester.

This is NOT an exchange order manager. A real adapter needs reconciliation,
native protection, rounding, funding and acknowledgement handling first.
"""
from dataclasses import asdict, replace
import json
import math

from trading_core.paper_execution import Config, Portfolio, on_tick, validate_config
from .state_store import encode


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def save_state(portfolio, config):
    state = asdict(portfolio)
    # Audit events live in the append-only journal, not the hot state blob.
    for key in ("orders", "trades", "decisions", "equity_curve"):
        state[key] = []
    if portfolio.next_signal:
        state["signals"] = portfolio.signals[portfolio.next_signal:]
        state["next_signal"] = 0
    state["config"] = asdict(config)
    return encode(state)


class PaperOrderManager:
    def __init__(self, store):
        self.store = store

    def register(self, account, symbol, config, *, allowed_risks=None, contract="unversioned"):
        validate_config(config)
        risks = sorted(set(allowed_risks or [config.risk_fraction]))
        if any(not positive(r) or r >= 1 for r in risks) or config.risk_fraction not in risks:
            raise ValueError("Invalid risk tiers")
        definition = encode(dict(symbol=symbol, execution=asdict(config), risk_tiers=risks, contract=contract))
        with self.store.transaction():
            row = self.store.db.execute("SELECT config FROM accounts WHERE id=?", (account,)).fetchone()
            if row:
                if row[0] != definition:
                    raise ValueError("Account configuration changed; use a new account/database")
                return
            p = Portfolio(label=account, signals=[], equity=config.initial_equity)
            self.store.db.execute("INSERT INTO accounts VALUES (?,?,?,?,NULL)",
                                 (account, symbol, definition, save_state(p, config)))

    def _load(self, account):
        row = self.store.db.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone()
        if row is None:
            raise KeyError(account)
        state = json.loads(row["state"])
        config = Config(**state.pop("config"))
        return row, Portfolio(**state), config

    def submit(self, account, signal, *, snapshot, risk_fraction, selected=True):
        """Record the first candidate even when its selector rejects it.

        Caller is a causal model adapter, not an exchange. Snapshot computation
        and data-health checks must be integrated before live-feed paper runs.
        """
        if type(selected) is not bool:
            raise ValueError("selected must be boolean")
        required = ("sample_id", "session_day", "direction", "symbol", "stop", "entry_reference",
                    "feature_as_of_ms", "entry_eligible_timestamp_ms", "entry_deadline_timestamp_ms",
                    "force_exit_timestamp_ms")
        if any(key not in signal for key in required):
            raise ValueError("Incomplete signal")
        asof, eligible, deadline, exit_at = (signal[k] for k in required[-4:])
        if not all(integer(x) for x in (asof, eligible, deadline, exit_at)) or not asof <= eligible <= deadline < exit_at:
            raise ValueError("Invalid causal timestamps")
        if snapshot.get("feature_as_of_ms") != asof:
            raise ValueError("Snapshot timestamp must match signal")
        if signal["direction"] not in ("long", "short") or not all(positive(signal[k]) for k in ("stop", "entry_reference")):
            raise ValueError("Invalid direction/price")
        payload = encode(dict(signal=signal, snapshot=snapshot, risk_fraction=risk_fraction, selected=selected))
        with self.store.transaction():
            row, p, config = self._load(account)
            previous = self.store.db.execute("SELECT payload FROM intents WHERE account=? AND session=?",
                                            (account, signal["session_day"])).fetchone()
            if previous:
                if previous[0] == payload:
                    return "duplicate"
                raise ValueError("Session already has a different first candidate")
            if signal["symbol"] != row["symbol"] or risk_fraction not in json.loads(row["config"])["risk_tiers"]:
                raise ValueError("Symbol/risk does not match account")
            cursor = json.loads(row["cursor"]) if row["cursor"] else None
            if cursor and eligible <= cursor["timestamp_ms"]:
                raise ValueError("Late signal: eligible market event already consumed")
            if p.position or p.signals:
                raise ValueError("Account already has an active position or pending candidate")
            self.store.db.execute("INSERT INTO intents VALUES (?,?,?)", (account, signal["session_day"], payload))
            if selected:
                p.signals = [signal]
                config = replace(config, risk_fraction=risk_fraction)
            self.store.record(account, "candidate", json.loads(payload))
            self.store.db.execute("UPDATE accounts SET state=? WHERE id=?", (save_state(p, config), account))
            return "pending" if selected else "selector_rejected"

    def tick(self, account, tick):
        return self.ticks(account, [tick])[0]

    def ticks(self, account, ticks):
        """Normalized, ordered public trades; only the final cursor is retained.

        agg_trade_id must be a monotonic integer for this market. A venue that
        cannot provide this needs a validated normalization/backfill adapter.
        """
        with self.store.transaction():
            row, p, config = self._load(account)
            cursor = json.loads(row["cursor"]) if row["cursor"] else None
            results = []
            for tick in ticks:
                if not all(integer(tick.get(k)) for k in ("timestamp_ms", "agg_trade_id")) or not positive(tick.get("price")):
                    raise ValueError("Invalid normalized trade")
                tick = {k: tick[k] for k in ("timestamp_ms", "agg_trade_id", "price")}
                if cursor:
                    if tick["agg_trade_id"] == cursor["agg_trade_id"]:
                        if tick != cursor:
                            raise ValueError("Conflicting duplicate trade")
                        results.append("duplicate")
                        continue
                    if tick["agg_trade_id"] < cursor["agg_trade_id"]:
                        results.append("old_event_ignored")
                        continue
                    if tick["timestamp_ms"] < cursor["timestamp_ms"]:
                        raise ValueError("Trade timestamp regressed")
                on_tick(p, tick, config)
                cursor = tick
                results.append("processed")
            for kind, events in (("fill", p.orders), ("trade", p.trades), ("decision", p.decisions), ("equity", p.equity_curve)):
                for event in events:
                    self.store.record(account, kind, event)
            self.store.db.execute("UPDATE accounts SET state=?,cursor=? WHERE id=?",
                                 (save_state(p, config), encode(cursor) if cursor else None, account))
            return results

    def status(self):
        output = []
        for row in self.store.db.execute("SELECT * FROM accounts ORDER BY id"):
            state = json.loads(row["state"])
            output.append(dict(account=row["id"], symbol=row["symbol"], closed_equity=state["equity"],
                               position=state["position"], pending_candidates=len(state["signals"]),
                               stop_trigger=state["stop_trigger"], partial_trigger=state["partial_trigger"],
                               last_trade=json.loads(row["cursor"]) if row["cursor"] else None))
        return output
