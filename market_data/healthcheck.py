"""Collector loop health, distinct from instrument trade age/readiness."""
import argparse
import sqlite3
import time
from pathlib import Path


def check(path,max_age=180):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='last_loop_ms'").fetchone()
        if not row or int(time.time()*1000)-int(row[0]) > max_age*1000:
            return False
        return True


if __name__=='__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--database',required=True)
    p.add_argument('--max-age',type=int,default=180)
    args = p.parse_args()
    raise SystemExit(0 if check(args.database,args.max_age) else 1)
