"""Dedicated real-execution journal. Never opens/converts a paper account ledger."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from live_engine.state_store import StateStore
from trading_core.contracts import canonical_json, content_hash


class FuturesStore(StateStore):
    def __init__(self, path, config):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer_lock = None
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        frozen = {k: v for k, v in config.items() if k not in ('execution_enabled', 'accept_new_entries')}
        identity = dict(owner='binance_futures_pilot', schema='1', environment='production', config_hash=content_hash(frozen))
        try:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                old = dict(self.db.execute('SELECT key,value FROM metadata'))
                if any(old.get(k) != v for k, v in identity.items()):
                    raise ValueError('Wrong/changed live ledger identity; never convert paper databases')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(session TEXT PRIMARY KEY,id TEXT UNIQUE NOT NULL,
                  intention TEXT NOT NULL,plan TEXT,state TEXT NOT NULL,reason TEXT);
                CREATE TABLE IF NOT EXISTS orders(client_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
                  role TEXT NOT NULL,params TEXT NOT NULL,state TEXT NOT NULL,response TEXT,error TEXT,
                  cancel_pending INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,session_id TEXT NOT NULL,
                  quantity TEXT NOT NULL,price TEXT NOT NULL,realized TEXT NOT NULL,commission TEXT NOT NULL,
                  commission_asset TEXT NOT NULL,maker INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS funding(id TEXT PRIMARY KEY,amount TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL);
            ''')
            with self.transaction():
                for k, v in identity.items(): self.set_meta(k, v)
        except BaseException:
            self.close()
            raise

    def meta(self, key, default=None):
        r = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def audit(self, kind, payload):
        stamped = dict(payload, local_recorded_at_ms=int(time.time() * 1000))
        self.db.execute('INSERT INTO audit(kind,payload) VALUES (?,?)', (kind, canonical_json(stamped)))

    def equity(self, initial):
        result = Decimal(str(initial))
        for r in self.db.execute('SELECT realized,commission,commission_asset FROM fills'):
            if r['commission_asset'] != 'USDC' and Decimal(r['commission']) != 0:
                raise ValueError('Non-USDC fee requires reviewed accounting; do not guess a conversion')
            result += Decimal(r['realized']) - Decimal(r['commission'])
        result += sum((Decimal(r[0]) for r in self.db.execute('SELECT amount FROM funding')), Decimal(0))
        return result

    def active(self):
        rows = self.db.execute("SELECT * FROM sessions WHERE state NOT IN ('CLOSED','SKIPPED','DISARMED') ORDER BY rowid").fetchall()
        if len(rows) > 1:
            raise ValueError('Multiple active sessions in single-model pilot')
        return rows[0] if rows else None

    def session_state(self, session, state, reason=None):
        self.db.execute('UPDATE sessions SET state=?,reason=? WHERE session=?', (state, reason, session))

    def status(self):
        return dict(mode='binance_futures_executor', simulation=False,
                    initialized=self.meta('initial_wallet') is not None,
                    source_cursor=self.meta('source_cursor'), entries_blocked=self.meta('entries_blocked'),
                    sessions=[dict(r) for r in self.db.execute('SELECT session,state,reason FROM sessions ORDER BY rowid DESC LIMIT 10')],
                    orders=[dict(r) for r in self.db.execute('SELECT client_id,role,state,cancel_pending,error FROM orders ORDER BY rowid DESC LIMIT 12')],
                    fill_count=self.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],
                    last_cycle_ms=self.meta('last_cycle_ms'))
