"""Paper account-owner SQLite. No collector/model state or exchange credentials."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from live_engine.state_store import StateStore
from trading_core.contracts import canonical_json, content_hash


class AccountStore(StateStore):
    def __init__(self, path, config, code_version):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer_lock = None
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        identity = dict(schema='4', owner='paper_account_service', environment='server-paper',
            venue=config['venue'], account_group=config['account_group'],
            config_hash=content_hash(config), code_version=code_version,
            simulation_version='next-native-print-v1')
        try:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                existing = dict(self.db.execute('SELECT key,value FROM metadata'))
                if any(existing.get(k) != v for k,v in identity.items()):
                    raise ValueError('Account identity changed; never reuse a legacy/other release ledger')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,symbol TEXT NOT NULL,
                config TEXT NOT NULL,state TEXT NOT NULL,cursor TEXT);
              CREATE TABLE IF NOT EXISTS intents(account TEXT NOT NULL REFERENCES accounts(id),
                session TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,session));
              CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,account TEXT NOT NULL,
                kind TEXT NOT NULL,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS receipts(channel TEXT,event_id TEXT,sequence INTEGER,
                payload_hash TEXT,received_ms INTEGER,PRIMARY KEY(channel,event_id));
              CREATE TABLE IF NOT EXISTS checkpoints(channel TEXT PRIMARY KEY,sequence INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS requests(intention_id TEXT PRIMARY KEY,allocation TEXT,
                payload TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,received_ms INTEGER);
              CREATE TABLE IF NOT EXISTS reservations(allocation TEXT PRIMARY KEY,intention_id TEXT UNIQUE,
                instrument_id TEXT NOT NULL,risk_amount REAL,margin_amount REAL);
              CREATE TABLE IF NOT EXISTS deadlines(allocation TEXT,intention_id TEXT,deadline_ms INTEGER,
                detected_ms INTEGER,status TEXT,PRIMARY KEY(allocation,intention_id));
              CREATE TABLE IF NOT EXISTS postings(journal_key TEXT,allocation TEXT,currency TEXT,
                book TEXT,amount TEXT,PRIMARY KEY(journal_key,book));
              CREATE TABLE IF NOT EXISTS book_positions(allocation TEXT PRIMARY KEY,sample_id TEXT,
                direction TEXT,entry_price REAL);
            ''')
            with self.transaction():
                for k,v in identity.items(): self.set_meta(k,v)
        except BaseException:
            self.close()
            raise

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key,str(value)))

    def anchor(self, channel, sequence):
        self.db.execute('INSERT OR IGNORE INTO checkpoints VALUES (?,?)',(channel,sequence))

    def checkpoint(self, channel):
        row = self.db.execute('SELECT sequence FROM checkpoints WHERE channel=?',(channel,)).fetchone()
        return row[0] if row else None

    def apply(self, channel, sequence, identity, payload, now_ms, handler):
        digest = content_hash(payload)
        with self.transaction():
            old = self.db.execute('SELECT payload_hash FROM receipts WHERE channel=? AND event_id=?',
                                  (channel,identity)).fetchone()
            if old:
                if old[0] != digest: raise ValueError('Conflicting account event redelivery')
                self.db.execute('UPDATE checkpoints SET sequence=MAX(sequence,?) WHERE channel=?',(sequence,channel))
                return False
            cursor = self.checkpoint(channel)
            if cursor is None or sequence <= cursor: raise ValueError('Unrecognized event behind account cursor')
            handler(payload, now_ms)
            self.db.execute('INSERT INTO receipts VALUES (?,?,?,?,?)',(channel,identity,sequence,digest,now_ms))
            self.db.execute('UPDATE checkpoints SET sequence=? WHERE channel=?',(sequence,channel))
        return True

    def check_retention(self, channel, first):
        cursor = self.checkpoint(channel)
        if cursor is not None and first > cursor+1:
            raise ValueError('Account execution retention gap; reviewed recovery required')

    def post(self, key, allocation, currency, debit, credit, amount):
        amount = Decimal(str(amount))
        if not amount.is_finite(): raise ValueError('Nonfinite posting')
        self.db.executemany('INSERT INTO postings VALUES (?,?,?,?,?)',
            [(key,allocation,currency,debit,str(amount)), (key,allocation,currency,credit,str(-amount))])

    def cash(self, allocation):
        return sum((Decimal(r[0]) for r in self.db.execute(
            "SELECT amount FROM postings WHERE allocation=? AND book='cash'",(allocation,))),Decimal(0))

    def record(self, account, kind, payload):
        super().record(account,kind,payload)
        if kind != 'fill': return
        journal_id = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        currency = self.get_meta('currency:'+account)
        if not currency: raise ValueError('Unregistered allocation currency')
        fee = payload['fee']
        self.post(f'fee:{journal_id}',account,currency,'fees','cash',fee)
        if payload['action'] == 'ENTRY':
            row = self.db.execute('SELECT payload FROM intents WHERE account=? ORDER BY rowid DESC LIMIT 1',
                                  (account,)).fetchone()
            direction = json.loads(row[0])['signal']['direction']
            self.db.execute('INSERT INTO book_positions VALUES (?,?,?,?)',
                (account,payload['sample_id'],direction,payload['price']))
        else:
            p = self.db.execute('SELECT * FROM book_positions WHERE allocation=?',(account,)).fetchone()
            if not p or p['sample_id'] != payload['sample_id']: raise ValueError('Fill accounting position mismatch')
            sign = 1 if p['direction']=='long' else -1
            gross = sign*(payload['price']-p['entry_price'])*payload['quantity']
            self.post(f'pnl:{journal_id}',account,currency,'cash','realized_pnl',gross)
            if payload['action']=='EXIT': self.db.execute('DELETE FROM book_positions WHERE allocation=?',(account,))

    def prune(self, now_ms):
        self.db.execute('DELETE FROM receipts WHERE received_ms<?',(now_ms-72*3600_000,))
