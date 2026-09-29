"""Small transactional state store; one database per venue, no tick archive."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StateStore:
    def __init__(self, path, venue):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.writer_lock = None
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts(
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, config TEXT NOT NULL,
                state TEXT NOT NULL, cursor TEXT);
            CREATE TABLE IF NOT EXISTS intents(
                account TEXT NOT NULL REFERENCES accounts(id), session TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(account, session));
            CREATE TABLE IF NOT EXISTS journal(
                id INTEGER PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(id),
                kind TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        try:
            with self.transaction():
                for key, value in (("schema", "1"), ("venue", venue), ("mode", "paper")):
                    self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))
                    actual = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]
                    if actual != value:
                        raise ValueError(f"Database {key} mismatch")
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        self.db.execute("SAVEPOINT nested_write" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if nested:
                self.db.execute("ROLLBACK TO nested_write")
                self.db.execute("RELEASE nested_write")
            elif self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("RELEASE nested_write" if nested else "COMMIT")

    def record(self, account, kind, payload):
        self.db.execute("INSERT INTO journal(account,kind,payload) VALUES (?,?,?)",
                        (account, kind, encode(payload)))

    def acquire_writer(self):
        """One collector/worker per database; OS releases the lock after a crash."""
        handle = open(str(self.path)+".writer.lock", "a+b")
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        self.writer_lock = handle

    def backup(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise ValueError("Backup must be a new, different file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)

    def close(self):
        self.db.close()
        if self.writer_lock:
            self.writer_lock.close()
            self.writer_lock = None
