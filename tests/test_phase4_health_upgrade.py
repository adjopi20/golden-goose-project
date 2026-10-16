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


@pytest.mark.parametrize('stage',['pending','open','partial','closed_uncertain'])
def test_health_upgrade_preserves_active_and_uncertain_state_and_resumes(tmp_path,stage):
    path=tmp_path/'account.sqlite';cfg=config();source=sorted(COMPATIBLE)[0]
    store=AccountStore(path,cfg,source);owner=AccountOwner(store,cfg)
    ticks=[trade(1,1001,100.),trade(2,1500,111.),trade(3,1501,111.),
           trade(4,2000,90.),trade(5,2001,89.)]
    done={'pending':0,'open':1,'partial':3,'closed_uncertain':5}[stage]
    owner.intention(intention(),1000)
    if done:owner.event(event(ticks[:done]),ticks[done-1]['timestamp_ms'])
    if stage=='closed_uncertain':
        with store.transaction():
            store.db.execute("UPDATE requests SET status='closed_uncertain'")
            store.set_meta('blocked:ETHUSDC',3000)
    control=tmp_path/'control.sqlite';store.backup(control)
    store.close()
    target='a'*40;upgrade(path,target,tmp_path/'backup')
    upgraded=AccountStore(path,cfg,target);control_store=AccountStore(control,cfg,source)
    for table in ('accounts','requests','reservations','postings','journal','checkpoints'):
        assert [tuple(r) for r in upgraded.db.execute(f'SELECT * FROM {table}')]==[tuple(r) for r in control_store.db.execute(f'SELECT * FROM {table}')]
    assert upgraded.get_meta('blocked:ETHUSDC')==control_store.get_meta('blocked:ETHUSDC')
    if done<len(ticks):
        AccountOwner(upgraded,cfg).event(event(ticks[done:]),2001)
        AccountOwner(control_store,cfg).event(event(ticks[done:]),2001)
        assert upgraded.db.execute('SELECT state FROM accounts').fetchone()[0]==control_store.db.execute('SELECT state FROM accounts').fetchone()[0]
        assert [tuple(r) for r in upgraded.db.execute('SELECT * FROM journal')]==[tuple(r) for r in control_store.db.execute('SELECT * FROM journal')]
    upgraded.close();control_store.close()


def test_health_upgrade_refuses_inconsistent_state_and_unknown_release(tmp_path):
    path=tmp_path/'account.sqlite';cfg=config()
    store=AccountStore(path,cfg,sorted(COMPATIBLE)[0]);owner=AccountOwner(store,cfg)
    owner.intention(intention(),1000);owner.event(event([trade(1,1001,100.)]),1001)
    store.db.execute('DELETE FROM reservations');store.close()
    with pytest.raises(ValueError,match='disagree'):upgrade(path,'a'*40,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()
    with sqlite3.connect(path) as db:db.execute("UPDATE metadata SET value='unknown' WHERE key='code_version'")
    with pytest.raises(ValueError,match='Unreviewed'):upgrade(path,'a'*40)
