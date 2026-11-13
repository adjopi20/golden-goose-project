"""Run one Telegram outbox across the active engine journals."""

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import signal
import sqlite3
import threading
import time

from utils.engine.monitoring.alerts import AlertStore, credentials, inspect_target, issue, load_config


def heartbeat_issues(heartbeat_ms, target, now):
    age = now - heartbeat_ms
    if age < -5000:
        return [issue("CLOCK_FUTURE", "Timestamp heartbeat di masa depan; periksa waktu server.")]
    if age > target["max_age_seconds"] * 1000:
        return [issue("HEARTBEAT_STALE", "Worker/sumber berhenti memperbarui heartbeat; periksa container dan koneksi.")]
    return []


def inspect_burst_legacy(db, metadata, target, now):
    expected = {key: value for key, value in target["expected_metadata"].items() if key != "worker_symbol"}
    cursor = json.loads(metadata["public_cursor"])
    if any(metadata.get(key) != value for key, value in expected.items()) or (
        cursor.get("symbol") != target["expected_metadata"]["worker_symbol"]
    ):
        return [issue("IDENTITY_MISMATCH", "Identitas jurnal Burst berbeda dari target monitor.")], False
    issues = heartbeat_issues(int(cursor["timestamp_ms"]), target, now)
    if metadata.get("entries_blocked"):
        issues.append(issue("ENTRIES_BLOCKED", "Entry diblokir; cek jurnal Burst sebelum melanjutkan."))
    sessions = db.execute("""SELECT session,plan FROM sessions
                            WHERE state NOT IN ('CLOSED','SKIPPED','DISARMED')""").fetchall()
    if len(sessions) > 1:
        issues.append(issue("MULTIPLE_ACTIVE", "Lebih dari satu lifecycle aktif pada satu allocation."))
    for session, plan in sessions:
        orders = db.execute("""SELECT role,state,response,cancel_pending,error FROM orders
                               WHERE session_id=?""", (session,)).fetchall()
        if any(state in {"SENDING", "UNKNOWN"} or cancel or error for _, state, _, cancel, error in orders):
            issues.append(issue("ORDER_UNRESOLVED", "Status order belum pasti; cek Binance dan jurnal Burst.", 30))
        entered = any(role == "ENTRY" and response and float(json.loads(response).get("executedQty", "0")) > 0
                      for role, _, response, _, _ in orders)
        protected = any(role == "STOP" and state in {"NEW", "TRIGGERING", "TRIGGERED", "FINISHED"}
                        for role, state, _, _, _ in orders)
        if entered and not protected:
            issues.append(issue("STOP_UNCONFIRMED", "Entry terisi tetapi stop belum terkonfirmasi; cek Binance.", 10))
        deadline = json.loads(plan).get("force_exit_ms") if plan else None
        if deadline is not None and now > int(deadline) + 15000:
            issues.append(issue("DEADLINE_UNRESOLVED", "Jadwal exit terlewati; periksa posisi dan order Binance."))
    return issues, True


def inspect_mvrv(db, target, now):
    metadata = dict(db.execute("SELECT key,value FROM meta"))
    contract = json.loads(metadata.get("monitor_contract", "null"))
    if not isinstance(contract, dict) or contract.get("contract") != target["monitor_contract"]:
        return [issue("IDENTITY_MISMATCH", "Jurnal BTC MVRV tidak memiliki kontrak monitor yang valid.")], False
    timestamp = datetime.fromisoformat(metadata["heartbeat"])
    if timestamp.tzinfo is None:
        raise ValueError("Naive MVRV heartbeat")
    issues = heartbeat_issues(int(timestamp.timestamp() * 1000), target, now)
    age_days = (datetime.fromtimestamp(now / 1000, timezone.utc).date()
                - date.fromisoformat(metadata["last_data_date"])).days
    if not 0 <= age_days <= 2 or metadata.get("error"):
        issues.append(issue("MVRV_DATA_ERROR", "Data/status BTC MVRV perlu diperiksa.", 60, "WARNING"))
    if metadata.get("notification_error"):
        issues.append(issue("MVRV_DELIVERY_ERROR", "Pengiriman sinyal BTC MVRV tertunda; periksa outbox.", 60))
    return issues, True


def inspect_native_executor(target, now):
    issues, valid = inspect_target(target, now)
    if valid:
        uri = Path(target["path"]).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            latest = db.execute("""
                SELECT o.state FROM orders o JOIN sessions s ON s.session=o.session_id
                WHERE s.state='OPEN' AND o.role='STOP'
                AND (o.client_id LIKE '%-guard-%' OR o.client_id LIKE '%-sl-v2-%')
                AND o.state!='CANCELED' ORDER BY o.rowid DESC LIMIT 1
            """).fetchone()
        if latest and latest[0] == "REJECTED":
            issues.append(issue("STOP_REPLACEMENT_REJECTED",
                                "Binance menolak perpindahan stop. Cek level stop aktif dan jurnal.", 30, "WARNING"))
    return issues, valid


def inspect_engine(target, now):
    adapter = target.get("adapter")
    try:
        if not adapter:
            if target["kind"] == "futures_executor":
                return inspect_native_executor(target, now)
            return inspect_target(target, now)
        uri = Path(target["path"]).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if adapter == "mvrv":
                return inspect_mvrv(db, target, now)
            if adapter != "burst_legacy":
                raise ValueError("Unknown engine adapter")
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            if target["heartbeat_key"] not in metadata:
                return inspect_burst_legacy(db, metadata, target, now)
        return inspect_native_executor(target, now)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return [issue("DB_UNREADABLE", "Jurnal/status tidak dapat dibaca; periksa mount, disk dan proses.", 30)], False


PERFORMANCE = "𝗠𝗢𝗗𝗘𝗟 𝗣𝗘𝗥𝗙𝗢𝗥𝗠𝗔𝗡𝗖𝗘"


def performance_alerts(target, profile):
    with closing(sqlite3.connect(Path(target["path"]).resolve().as_uri() + "?mode=ro", uri=True,
                                  timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        meta = dict(db.execute("SELECT key,value FROM metadata"))
        if meta.get("strategy_contract") != target["performance_contract"] or any(
            meta.get(k) != v for k, v in target["expected_metadata"].items()
        ):
            raise ValueError("performance_journal_contract_mismatch")
        bands = profile["bands"]
        horizon = len(bands["net_loss"])
        records = db.execute("""SELECT payload FROM audit WHERE kind='trade_closed'
                                ORDER BY id DESC LIMIT ?""", (horizon,)).fetchall()
    count = len(records)
    if count < profile["minimum_closed_trades"]:
        return []
    wealth = peak = Decimal(1)
    max_dd = Decimal(0)
    streak = max_streak = 0
    for row in reversed(records):
        trade = json.loads(row[0])
        equity = Decimal(trade["plan"]["equity_before_entry"])
        pnl = Decimal(trade["net_pnl"])
        if not equity.is_finite() or equity <= 0 or not pnl.is_finite():
            raise ValueError("invalid_closed_trade_performance")
        wealth *= max(Decimal(0), 1 + pnl / equity)
        peak = max(peak, wealth)
        max_dd = max(max_dd, 1 - wealth / peak)
        streak = streak + 1 if pnl < 0 else 0
        max_streak = max(max_streak, streak)

    results = []
    for key, label, value, as_percent in (
        ("net_loss", "Kerugian net kumulatif", max(Decimal(0), 1 - wealth), True),
        ("drawdown", "Drawdown maksimum equity tertutup", max_dd, True),
        ("loss_streak", "Rangkaian rugi terpanjang", max_streak, False),
    ):
        mean, p95 = (Decimal(str(x)) for x in bands[key][count - 1])
        if not mean.is_finite() or not p95.is_finite() or min(mean, p95) < 0:
            raise ValueError("invalid_monte_carlo_baseline")
        if value <= mean:
            continue
        fmt = ".2%" if as_percent else ".2f"
        context = " · melewati P95" if value > p95 else ""
        detail = (f"{label} {value:{fmt}} · rata-rata MC {mean:{fmt}} · P95 {p95:{fmt}}{context}\n"
                  f"{count} trade tertutup · {profile['simulations']} shuffle · evaluasi strategi")
        results.append(issue(key.upper(), detail, severity=PERFORMANCE))
    return results


def poll_target(store, target, now, config, profiles=None):
    if target.get("start_when_present"):
        key = "observed:" + target["id"]
        if not store.db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
            uri = Path(target["path"]).resolve().as_uri() + "?mode=ro"
            try:
                with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
                    if not db.execute("SELECT 1 FROM metadata WHERE key=?", (target["heartbeat_key"],)).fetchone():
                        return
            except sqlite3.Error:
                return
            store.set_meta(key, "1")
    issues, valid = inspect_engine(target, now)
    store.observe(target, issues, valid, now, config["reminder_seconds"] * 1000, config["monitor_id"])
    contract = target.get("performance_contract")
    if not contract or not profiles or contract not in profiles or not valid:
        return
    try:
        performance = performance_alerts(target, profiles[contract])
    except (OSError, sqlite3.Error, ValueError, ArithmeticError, KeyError, TypeError, IndexError):
        performance = [issue("PERFORMANCE_DATA_UNAVAILABLE", "Data atau baseline performa belum dapat dibaca.", 60, "WARNING")]
        valid = False
    store.observe(dict(target, id=target["id"] + "-performance"), performance,
                  valid, now, 86_400_000, config["monitor_id"])


def main():
    import requests

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", default="/data/alerts.sqlite")
    parser.add_argument("--performance", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    profiles = {}
    if args.performance:
        doc = json.loads(args.performance.read_text(encoding="utf-8"))
        required = {target["performance_contract"] for target in config["targets"] if "performance_contract" in target}
        if doc["schema"] != "closed_trade_shuffle_v1" or not required <= doc["profiles"].keys():
            raise ValueError("missing_or_invalid_performance_profiles")
        profiles = doc["profiles"]
    token, chat = credentials()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with requests.Session() as http, closing(AlertStore(args.database, config["monitor_id"])) as store:
        http.trust_env = False
        store.acquire_writer()
        while not stop.is_set():
            now = int(time.time() * 1000)
            for target in config["targets"]:
                poll_target(store, target, now, config, profiles)
            store.deliver_one(http, token, chat, now)
            store.set_meta("heartbeat_ms", int(time.time() * 1000))
            stop.wait(config["poll_seconds"])


if __name__ == "__main__":
    main()
