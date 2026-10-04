"""Collector-owned compact history and transactional publication outbox."""
import json
from pathlib import Path
import sqlite3

from live_engine.state_store import StateStore
from trading_core.contracts import canonical_json, validate_event


class Backpressure(RuntimeError):
    pass


class DataStore(StateStore):
    # Reuse transaction/writer-lock/backup/close mechanics, not account schema.
    def __init__(self, path, venue, *, environment='server-paper', max_outbox_bytes=32*1024*1024):
        if venue not in ('binance', 'lighter') or environment not in ('local-dev','server-paper'):
            raise ValueError('Phase 2 supports public paper/shadow collection only')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer_lock = None
        self.max_outbox_bytes = max_outbox_bytes
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
            values = dict(self.db.execute('SELECT key,value FROM metadata'))
            if values.get('owner')!='market_data' or values.get('schema')!='2' or values.get('venue')!=venue or values.get('environment')!=environment:
                self.db.close()
                raise ValueError('Not this collector database; do not reuse a paper/account ledger')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
            subject TEXT NOT NULL,payload TEXT NOT NULL,bytes INTEGER NOT NULL);
        ''')
        try:
            with self.transaction():
                for key, value in dict(schema='2',owner='market_data',venue=venue,environment=environment).items():
                    self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',(key,value))
                    if self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()[0] != value:
                        raise ValueError(f'Collector database {key} mismatch')
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('stream_sequence','0')")
        except BaseException:
            self.close()
            raise

    def next_sequence(self):
        if not self.db.in_transaction:
            raise RuntimeError('Sequence must be assigned in the aggregation transaction')
        seq = int(self.get_meta('stream_sequence'))+1
        self.set_meta('stream_sequence',str(seq))
        return seq

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key,str(value)))

    def queue(self, subject, event):
        if not self.db.in_transaction:
            raise RuntimeError('Publication must be atomic with collector state')
        payload = canonical_json(validate_event(event))
        size = len(payload.encode())
        used = self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM outbox').fetchone()[0]
        if used+size > self.max_outbox_bytes:
            raise Backpressure('Outbox limit reached: checkpoint NOT advanced; repair/retry required')
        self.db.execute('INSERT INTO outbox(event_id,subject,payload,bytes) VALUES (?,?,?,?)',
                        (event['event_id'],subject,payload,size))

    def pending(self, limit=200):
        return self.db.execute('SELECT * FROM outbox ORDER BY sequence LIMIT ?', (limit,)).fetchall()

    def published(self, sequence):
        with self.transaction():
            self.db.execute('DELETE FROM outbox WHERE sequence=?',(sequence,))

    def status(self):
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        feeds = {symbol:json.loads(state) for symbol,state in self.db.execute('SELECT symbol,state FROM feeds')} if 'feeds' in tables else {}
        return dict(owner='market_data',venue=self.get_meta('venue'),real_orders=False,
            pending_events=self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],
            pending_bytes=self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM outbox').fetchone()[0],
            last_loop_ms=self.get_meta('last_loop_ms'),last_publish_ms=self.get_meta('last_publish_ms'),
            minutes=self.db.execute('SELECT COUNT(*) FROM minutes').fetchone()[0] if 'minutes' in tables else 0,
            feeds={symbol:dict(last_event_ms=state['cursor']['timestamp_ms'],
                last_source_sequence=state['cursor']['agg_trade_id'],coverage_start_ms=state['coverage_start_ms'],
                complete_from_ms=state['complete_from_ms']) for symbol,state in feeds.items()})
