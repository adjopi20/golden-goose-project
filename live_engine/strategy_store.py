"""Strategy-owned history and decisions; deliberately no account/order tables."""
import json
from pathlib import Path
import sqlite3
from live_engine.state_store import StateStore
from trading_core.contracts import canonical_json, content_hash, validate_event


class StrategyStore(StateStore):
    def __init__(self, path, *, venue, config_hash, calibration_hash, code_version):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer_lock = None
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        identity = dict(schema='3', owner='preny_strategy_shadow', venue=venue,
            environment='server-paper', config_hash=config_hash,
            calibration_hash=calibration_hash, code_version=code_version)
        try:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                values = dict(self.db.execute('SELECT key,value FROM metadata'))
                if any(values.get(k) != v for k, v in identity.items()):
                    raise ValueError('Strategy identity changed; use a new shadow database/release')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS minutes(symbol TEXT,timestamp_ms INTEGER,payload TEXT,source TEXT,
                PRIMARY KEY(symbol,timestamp_ms));
              CREATE TABLE IF NOT EXISTS profiles(symbol TEXT,session TEXT,payload TEXT,PRIMARY KEY(symbol,session));
              CREATE TABLE IF NOT EXISTS profile_prices(symbol TEXT,session TEXT,price REAL,quantity REAL,
                PRIMARY KEY(symbol,session,price));
              CREATE TABLE IF NOT EXISTS evaluations(symbol TEXT,asof INTEGER,payload TEXT,PRIMARY KEY(symbol,asof));
              CREATE TABLE IF NOT EXISTS receipts(event_id TEXT PRIMARY KEY,sequence INTEGER,
                payload_hash TEXT,received_ms INTEGER);
              CREATE TABLE IF NOT EXISTS proposals(intention_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            ''')
            with self.transaction():
                for k, v in identity.items(): self.set_meta(k, v)
        except BaseException:
            self.close()
            raise

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, str(value)))

    def bootstrap(self, collector_path, *, symbols, broker_sequence, now_ms):
        """One consistent read-only snapshot, followed by broker replay from BEFORE it.

        The venue's source-sequence anchor skips events already in the snapshot;
        the global broker anchor cannot be obtained after the copy (race/loss).
        """
        uri = Path(collector_path).resolve().as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True, timeout=10) as source:
            source.execute('BEGIN')
            meta = dict(source.execute('SELECT key,value FROM metadata'))
            if (meta.get('owner'), meta.get('schema'), meta.get('venue'), meta.get('environment')) != (
                    'market_data', '2', self.get_meta('venue'), 'server-paper'):
                raise ValueError('Bootstrap must be this venue collector, not a paper ledger')
            if not meta.get('source_epoch'):
                raise ValueError('Upgrade the shadow collector before starting Phase 3')
            if self.get_meta('bootstrap_source_sequence') is not None:
                if self.get_meta('source_epoch') != meta['source_epoch']:
                    raise ValueError('Collector database replaced; explicit recovery review required')
                return False
            with self.transaction():
                for symbol in symbols:
                    for row in source.execute('SELECT * FROM minutes WHERE symbol=? ORDER BY timestamp_ms', (symbol,)):
                        self.db.execute('INSERT INTO minutes VALUES (?,?,?,?)', row)
                    for row in source.execute('SELECT * FROM profiles WHERE symbol=?', (symbol,)):
                        self.db.execute('INSERT INTO profiles VALUES (?,?,?)', row)
                    feed = source.execute('SELECT state FROM feeds WHERE symbol=?', (symbol,)).fetchone()
                    if feed: self.set_meta('bootstrap_feed:'+symbol, feed[0])
                self.set_meta('bootstrap_source_sequence', meta['stream_sequence'])
                self.set_meta('source_epoch', meta['source_epoch'])
                self.set_meta('broker_sequence', broker_sequence)
                self.set_meta('bootstrap_ms', now_ms)
        return True

    def apply(self, sequence, event, now_ms, handler):
        event = validate_event(event)
        if event['venue'] != self.get_meta('venue') or event['product'] != 'futures':
            raise ValueError('Wrong strategy venue/product')
        epoch = self.get_meta('source_epoch')
        if epoch and ((event.get('source_epoch') is not None and event['source_epoch'] != epoch)
                or (event['stream_sequence'] > int(self.get_meta('bootstrap_source_sequence',0))
                    and event.get('source_epoch') != epoch)):
            raise ValueError('Collector epoch changed/missing; do not silently skip a reset feed')
        digest = content_hash(event)
        with self.transaction():
            prior = self.db.execute('SELECT payload_hash FROM receipts WHERE event_id=?', (event['event_id'],)).fetchone()
            if prior:
                if prior[0] != digest: raise ValueError('Conflicting duplicate event')
                self.set_meta('broker_sequence', max(sequence, int(self.get_meta('broker_sequence', 0))))
                self.set_meta('last_receipt_ms', now_ms)
                return False
            if sequence <= int(self.get_meta('broker_sequence', 0)):
                raise ValueError('Unrecognized event behind committed cursor')
            if event['stream_sequence'] > int(self.get_meta('bootstrap_source_sequence', 0)):
                handler(event)
            self.db.execute('INSERT INTO receipts VALUES (?,?,?,?)', (event['event_id'], sequence, digest, now_ms))
            self.set_meta('broker_sequence', sequence)
            self.set_meta('last_receipt_ms', now_ms)
        return True

    def check_retention(self, first_sequence):
        if first_sequence > int(self.get_meta('broker_sequence', 0))+1:
            raise ValueError('Broker retention gap; stop shadow and create reviewed fresh bootstrap')

    def prune(self, now_ms):
        with self.transaction():
            self.db.execute('DELETE FROM minutes WHERE timestamp_ms<?', (now_ms-45*86400_000,))
            # Decisions/proposals are audit records, not discarded with input retention.
            self.db.execute('DELETE FROM receipts WHERE received_ms<?', (now_ms-72*3600_000,))

    def status(self):
        return dict(venue=self.get_meta('venue'), mode='strategy_shadow', real_orders=False,
            broker_sequence=int(self.get_meta('broker_sequence', 0)),
            last_receipt_ms=self.get_meta('last_receipt_ms'), last_poll_ms=self.get_meta('last_poll_ms'),
            bootstrap_source_sequence=self.get_meta('bootstrap_source_sequence'),
            decisions=self.db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0],
            proposals=self.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0],
            latest={s:json.loads(p) for s,p in self.db.execute('SELECT symbol,payload FROM evaluations e '
                'WHERE asof=(SELECT MAX(asof) FROM evaluations WHERE symbol=e.symbol)')})
