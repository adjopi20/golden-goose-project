"""Transactional state and outbox; independent of intraday trading databases."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw(date TEXT PRIMARY KEY, price REAL NOT NULL, cap REAL NOT NULL, ratio REAL NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vintages(id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, cash TEXT NOT NULL, btc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(account TEXT, date TEXT, evaluated_at TEXT, z REAL, entry REAL, exit REAL, action TEXT, PRIMARY KEY(account,date));
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, account TEXT, signal_date TEXT, created_at TEXT, due_close TEXT, side TEXT, status TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_account ON actions(account) WHERE status='pending';
CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY, action_id TEXT, recorded_at TEXT, price_time TEXT, units TEXT, price TEXT, fee TEXT, side TEXT);
CREATE TABLE IF NOT EXISTS marks(account TEXT, date TEXT, equity REAL, cash REAL, btc REAL, recorded_at TEXT, PRIMARY KEY(account,date));
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, message TEXT NOT NULL, delivered_at TEXT);
"""


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(SCHEMA)
    return connection


def get_meta(c, key, default=None):
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(c, key, value):
    c.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def message(c, key, text):
    c.execute("INSERT OR IGNORE INTO outbox(id,message) VALUES(?,?)", (key, text))


def archive(c, frame, when):
    c.execute("INSERT INTO vintages(fetched_at,payload) VALUES(?,?)", (when, frame.to_json(orient="records")))


def backup(c, destination):
    if Path(destination).exists():
        raise FileExistsError("Use a new backup filename")
    with sqlite3.connect(destination) as target:
        c.backup(target)
