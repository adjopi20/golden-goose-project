"""Lightweight read-only heartbeat health for service-owned SQLite."""
import argparse
from pathlib import Path
import sqlite3
import time


def check(path, keys, max_age_ms=180_000, now_ms=None):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        now=int(time.time()*1000) if now_ms is None else now_ms
        for key in keys:
            row=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            if not row or not 0 <= now-int(row[0]) <= max_age_ms: return False
        return bool(keys)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--database',required=True)
    p.add_argument('--keys',nargs='+',required=True)
    args=p.parse_args()
    try: healthy=check(args.database,args.keys)
    except (sqlite3.Error,ValueError): healthy=False
    raise SystemExit(0 if healthy else 1)
