import json
import sqlite3
import pytest
from infra.ops.upgrade_phase4_account_health import COMPATIBLE, upgrade
from live_engine.account_store import AccountStore
from live_engine.account_owner import AccountOwner
from test_account_phase4 import config, intention, event, trade


def test_health_upgrade_preserves_history_identity_and_reopens(tmp_path):
    path=tmp_path/'account.sqlite';cfg=config()
    store=AccountStore(path,cfg,sorted(COMPATIBLE)[0]);owner=AccountOwner(store,cfg)
    owner.intention(intention(),1000)
    owner.event(event([trade(1,1001,100.),trade(2,2000,90.),trade(3,2001,90.)]),2001)
    before={table:store.db.execute(f'SELECT * FROM {table}').fetchall()
            for table in ('accounts','journal','postings','requests','checkpoints','receipts')}
    before={k:[tuple(r) for r in rows] for k,rows in before.items()}
    store.db.execute('DROP INDEX receipts_received_ms')  # Existing server releases lack this index.
    store.close()
    target='a'*40;backup=tmp_path/'backup'
    assert upgrade(path,target)['status']=='ready'
    assert upgrade(path,target,backup)['status']=='upgraded'
    store=AccountStore(path,cfg,target)
    assert store.db.execute("SELECT 1 FROM sqlite_master WHERE name='receipts_received_ms'").fetchone()
    for table,rows in before.items():
        assert [tuple(r) for r in store.db.execute(f'SELECT * FROM {table}')]==rows
    store.close()
    with sqlite3.connect(next(backup.glob('*.sqlite'))) as db:
        assert dict(db.execute('SELECT key,value FROM metadata'))['code_version'] in COMPATIBLE
    assert upgrade(path,target,backup)['status']=='already_current'


def test_health_upgrade_refuses_open_position_and_unknown_release(tmp_path):
    path=tmp_path/'account.sqlite';cfg=config()
    store=AccountStore(path,cfg,sorted(COMPATIBLE)[0]);owner=AccountOwner(store,cfg)
    owner.intention(intention(),1000);owner.event(event([trade(1,1001,100.)]),1001)
    store.close()
    with pytest.raises(ValueError,match='flat'):upgrade(path,'a'*40,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()
    with sqlite3.connect(path) as db:db.execute("UPDATE metadata SET value='unknown' WHERE key='code_version'")
    with pytest.raises(ValueError,match='Unreviewed'):upgrade(path,'a'*40)
