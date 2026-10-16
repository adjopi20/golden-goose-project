"""Reviewed schema-only paper-account upgrade. Run with the account stopped."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import time

COMPATIBLE = {
    'e2917f054494682b4e5ba703631089263d03156e',
    'c8fc52081950095fa478a4f076b26d2107853d22',
}


def inspect(db, target):
    meta=dict(db.execute('SELECT key,value FROM metadata'))
    if (meta.get('schema'),meta.get('owner'),meta.get('environment')) != ('4','paper_account_service','server-paper'):
        raise ValueError('Not a Phase 4 paper-account ledger')
    if meta.get('code_version') not in COMPATIBLE | {target}:
        raise ValueError('Unreviewed source release')
    active=sum(bool(s.get('position') or s.get('signals')) for (raw,) in db.execute('SELECT state FROM accounts')
               for s in [json.loads(raw)])
    pending=db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('pending','open','closed_uncertain')").fetchone()[0]
    reservations=db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]
    if active or pending or reservations:
        raise ValueError(f'Wait until accounts are flat and reconciled: active={active}, pending/uncertain={pending}, reservations={reservations}')
    return meta


def upgrade(path, target, backup_dir=None):
    if not re.fullmatch('[0-9a-f]{40}',target):
        raise ValueError('Target must be the exact tested commit')
    path=Path(path).resolve()
    db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5)
    try:
        meta=inspect(db,target)
        if backup_dir is None or meta['code_version']==target:
            return dict(database=str(path),status='already_current' if meta['code_version']==target else 'ready')
        backup_dir=Path(backup_dir);backup_dir.mkdir(parents=True,exist_ok=True)
        backup=backup_dir/(meta['account_group']+'.sqlite')
        if backup.exists(): raise ValueError('Backup must not overwrite an existing file')
        with sqlite3.connect(backup) as copy:
            db.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Backup integrity check failed')
        with db:
            db.execute('CREATE INDEX IF NOT EXISTS receipts_received_ms ON receipts(received_ms)')
            stamp=int(time.time()*1000)
            db.execute('INSERT INTO metadata VALUES (?,?)',
                ('health_upgrade:'+str(stamp),json.dumps(dict(previous=meta['code_version'],current=target))))
            db.execute("UPDATE metadata SET value=? WHERE key='code_version'",(target,))
        return dict(database=str(path),status='upgraded',backup=str(backup))
    finally:
        db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,nargs='+',required=True)
    parser.add_argument('--target',required=True)
    parser.add_argument('--backup-dir',type=Path)
    args=parser.parse_args()
    # Validate both before changing either. Account services must stay stopped.
    for path in args.database:upgrade(path,args.target)
    for path in args.database:print(json.dumps(upgrade(path,args.target,args.backup_dir)))
