"""One deterministic cash/BTC engine for both frozen threshold branches."""
from decimal import Decimal, localcontext
import hashlib
import json
import math

import pandas as pd

from .data import next_close, utc
from .storage import get_meta, message, set_meta


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Amount must be finite and nonnegative")
    return result


def configuration():
    from pathlib import Path
    config = json.loads(Path(__file__).with_name("config.json").read_text())
    if config != {
        "contract": "btc-mvrv-dual-v1", "history_origin": "2010-07-18", "fee_bps": 10,
        "models": [{"id": "fixed_0_7", "weight": "0.5", "entry": 0, "exit": 7},
                   {"id": "percentile_4y_p5_p95", "weight": "0.5", "window": 1460, "low": .05, "high": .95}]}:
        raise ValueError("Frozen config changed; create and test a new contract version")
    return config


def initialize(c, config, capital, mode, when):
    if mode not in {"paper", "alerts"} or amount(capital) <= 0:
        raise ValueError("Choose paper/alerts and positive initial capital")
    fingerprint = json.dumps(dict(config=config, capital=str(amount(capital).normalize()), mode=mode), sort_keys=True)
    existing = get_meta(c, "contract")
    if existing is not None:
        if existing != fingerprint:
            raise ValueError("Account configuration changed; existing state will not be reset")
        return
    if sum(amount(m["weight"]) for m in config["models"]) != 1:
        raise ValueError("Initial weights must sum to one")
    for model in config["models"]:
        c.execute("INSERT INTO accounts VALUES(?,?,?)", (model["id"], str(amount(capital) * amount(model["weight"])), "0"))
    set_meta(c, "contract", fingerprint)
    set_meta(c, "initialized_at", when)
    set_meta(c, "initial_capital", capital)
    set_meta(c, "mode", mode)


def fill(c, action_id, fill_id, units, price, fee, recorded_at, price_time=None, complete=True):
    """Actual/manual or planned paper fill. Never debit another account."""
    old = c.execute("SELECT * FROM fills WHERE id=?", (fill_id,)).fetchone()
    if old:
        if (old["action_id"], amount(old["units"]), amount(old["price"]), amount(old["fee"])) != (action_id, amount(units), amount(price), amount(fee)):
            raise ValueError("Fill ID already used for a different transaction")
        return False
    action = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if action is None or action["status"] != "pending":
        raise ValueError("No pending action for this fill")
    recorded = utc(recorded_at)
    priced = utc(price_time or recorded_at)
    if recorded < utc(action["created_at"]) or priced < utc(action["created_at"]) or priced > recorded:
        raise ValueError("Fill/price cannot precede proposal or occur in the future")
    q, p, f = amount(units), amount(price), amount(fee)
    if q <= 0 or p <= 0:
        raise ValueError("Fill units and price must be positive")
    account = c.execute("SELECT * FROM accounts WHERE id=?", (action["account"],)).fetchone()
    cash, btc = amount(account["cash"]), amount(account["btc"])
    with localcontext() as context:
        context.prec = 40
        notional = q * p
        if action["side"] == "buy":
            cash -= notional + f
            btc += q
        else:
            if f > notional:
                raise ValueError("Sell fee exceeds proceeds")
            cash += notional - f
            btc -= q
        if min(cash, btc) < 0:
            raise ValueError("Insufficient account balance; transaction rejected")
    c.execute("UPDATE accounts SET cash=?,btc=? WHERE id=?", (str(cash), str(btc), action["account"]))
    c.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)", (fill_id, action_id, recorded.isoformat(), priced.isoformat(), str(q), str(p), str(f), action["side"]))
    if complete:
        c.execute("UPDATE actions SET status='filled' WHERE id=?", (action_id,))
    message(c, "fill:" + fill_id, f"{action['account']} {action['side'].upper()} FILL [{fill_id}]: {q} BTC @ ${p}; fee ${f}; recorded={recorded.isoformat()}; mode={get_meta(c,'mode')}")
    return True


def apply_day(c, row, config, evaluated_at, mode, *, historical=False, btc_dust="1e-12"):
    """Caller owns SQL transaction. Live caller must validate freshness first."""
    when = utc(evaluated_at).isoformat()
    day = str(row["date"])
    close_at = (pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=1)).isoformat()
    if utc(when) < utc(close_at):
        raise ValueError("Daily observation is not closed yet")
    if not math.isfinite(float(row["z"])) or not math.isfinite(float(row["price"])) or float(row["price"]) <= 0:
        raise ValueError("Invalid daily signal input")
    last = get_meta(c, "last_decision_date")
    if last is not None and day <= last:
        return False
    for model in config["models"]:
        account_id = model["id"]
        pending = c.execute("SELECT * FROM actions WHERE account=? AND status='pending'", (account_id,)).fetchone()
        if pending and mode == "paper":
            due = utc(pending["due_close"])
            if utc(close_at) > due:
                c.execute("UPDATE actions SET status='expired' WHERE id=?", (pending["id"],))
                message(c, "expired:" + pending["id"], f"{account_id}: missed planned paper close; NO backdated fill [{pending['id']}]")
            elif utc(close_at) == due:
                account = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                with localcontext() as context:
                    context.prec = 40
                    p, rate = amount(row["price"]), amount(config["fee_bps"]) / 10000
                    if pending["side"] == "buy":
                        # Round down so decimal division never overspends cash.
                        from decimal import ROUND_DOWN
                        q = (amount(account["cash"]) / (1 + rate) / p).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                    else:
                        q = amount(account["btc"])
                    f = q * p * rate
                fill(c, pending["id"], "paper:" + pending["id"], q, p, f, when, close_at)
        account = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        cash, btc = amount(account["cash"]), amount(account["btc"])
        lo, hi = float(row[account_id + "_entry"]), float(row[account_id + "_exit"])
        action = "WAIT"
        waiting = c.execute("SELECT 1 FROM actions WHERE account=? AND status='pending'", (account_id,)).fetchone()
        if waiting:
            action = "PENDING"
        elif not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
            action = "DATA_NOT_READY"
        elif btc <= amount(btc_dust) and cash > Decimal("0.0000001") and row["z"] < lo:
            action = "BUY"
        elif btc > amount(btc_dust) and row["z"] >= hi:
            action = "SELL"
        if action in {"BUY", "SELL"}:
            key = hashlib.sha256(f"{config['contract']}:{account_id}:{day}:{action}".encode()).hexdigest()[:24]
            due = (pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=2)).isoformat() if historical else next_close(day, when)
            c.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?)", (key, account_id, day, when, due, action.lower(), "pending"))
            currency = get_meta(c, "quote_asset", "USD")
            venue = {"binance_spot_testnet": "TESTNET ", "binance_spot_production": "PRODUCTION "}.get(get_meta(c, "execution"), "")
            budget = f"cash budget including fees {cash} {currency}" if action == "BUY" else f"sell attributed BTC {btc}"
            message(c, "action:" + key, f"{venue}{account_id} {action} [{key}] mode={mode}; data={day}; Z={row['z']:.6f}; entry<{lo:.6f}; exit>={hi:.6f}; {budget}. Proposal is NOT a real fill.")
        c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)", (account_id, day, when, float(row["z"]), lo if math.isfinite(lo) else None, hi if math.isfinite(hi) else None, action))
        value = float(cash + btc * amount(row["price"]))
        c.execute("INSERT INTO marks VALUES(?,?,?,?,?,?)", (account_id, day, value, float(cash), float(btc), when))
    set_meta(c, "last_decision_date", day)
    set_meta(c, "last_mark_price", row["price"])
    return True
