"""Offline shared-feed, account separation and release-boundary checks."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from utils.engine.collectors.market_data.collector import Collector
from utils.engine.collectors.market_data.run import read_config
from utils.engine.collectors.market_data.shadow import ReceiptStore
from utils.engine.collectors.market_data.store import DataStore
from utils.engine.state.base import StateStore
from utils.engine.state.paper_account import AccountStore
from utils.engine.state.futures import FuturesStore
from utils.engine import release


def test_single_public_event_shared_by_two_independent_consumers(tmp_path):
    feed=DataStore(tmp_path/'feed.sqlite','binance',environment='shared-market')
    paper=ReceiptStore(tmp_path/'paper.sqlite');live=ReceiptStore(tmp_path/'live.sqlite')
    try:
        collector=Collector(feed,{'ETHUSDC':'ETHUSDC'})
        collector.consume('ETHUSDC',[dict(symbol='ETHUSDC',agg_trade_id=1,timestamp_ms=1,price=100.,quantity=2.,buy=True)],10)
        row=next(r for r in feed.pending() if json.loads(r['payload'])['event_type']=='trade')
        event=json.loads(row['payload'])
        assert 'environment' not in event and event['source_epoch']==feed.get_meta('source_epoch')
        paper.apply('GG_EXEC_V1',1,event,10)
        assert live.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==0
        live.apply('GG_EXEC_V1',1,event,20)
        a=paper.db.execute('SELECT event_id,payload FROM receipts').fetchone()
        b=live.db.execute('SELECT event_id,payload FROM receipts').fetchone()
        assert a==b
        paper.heartbeat('account_test',30)
        assert live.db.execute("SELECT value FROM metadata WHERE key='account_test'").fetchone() is None
        assert not feed.db.execute("SELECT name FROM sqlite_master WHERE name='accounts'").fetchone()
    finally:
        paper.db.close();live.db.close();feed.close()


def test_shared_source_config_is_explicit_and_does_not_relabel_old_db(tmp_path):
    config=tmp_path/'feed.json'
    config.write_text(json.dumps(dict(schema_version=1,environment='shared-market',product='futures',venue='binance',symbols=['ETHUSDC'])))
    assert read_config(config)['environment']=='shared-market'
    old=DataStore(tmp_path/'feed.sqlite','binance',environment='server-paper');old.close()
    with pytest.raises(ValueError,match='Not this collector'):
        DataStore(tmp_path/'feed.sqlite','binance',environment='shared-market')
    old=DataStore(tmp_path/'feed.sqlite','binance',environment='server-paper')
    assert old.get_meta('environment')=='server-paper';old.close()


def test_shared_storage_implementation_never_promotes_paper_account_state(tmp_path):
    assert issubclass(AccountStore,StateStore) and issubclass(FuturesStore,StateStore)
    paper=AccountStore(tmp_path/'paper.sqlite',dict(venue='binance',account_group='test'),'test')
    with paper.transaction():paper.set_meta('paper_only_balance','1234')
    paper.close()
    config=dict(symbol='ETHUSDC',allocation_id='test',initial_equity=200,execution_enabled=False)
    before=(tmp_path/'paper.sqlite').read_bytes()
    with pytest.raises(ValueError,match='never convert paper'):
        FuturesStore(tmp_path/'paper.sqlite',config)
    assert (tmp_path/'paper.sqlite').read_bytes()==before
    live=FuturesStore(tmp_path/'live.sqlite',config)
    try:
        assert live.meta('paper_only_balance') is None
        assert live.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0
        assert live.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==0
    finally:live.close()


def engine_fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(release,'ROOT',tmp_path)
    source=tmp_path/'engine.py';source.write_text('VALUE=1\n')
    manifest=dict(schema='shared_engine_release_v1',python=list(sys.version_info[:3]),packages={},
                  files={'engine.py':hashlib.sha256(source.read_bytes()).hexdigest()})
    path=tmp_path/'release.json';path.write_text(json.dumps(manifest))
    return path,hashlib.sha256(path.read_bytes()).hexdigest(),source


def test_model_pins_engine_bytes_and_manifest_not_just_a_version_label(tmp_path,monkeypatch):
    path,digest,source=engine_fixture(tmp_path,monkeypatch)
    assert release.verify_release(path,expected_sha256=digest)['files']
    source.write_text('VALUE=2\n')
    with pytest.raises(RuntimeError,match='source changed'):release.verify_release(path,expected_sha256=digest)
    updated=json.loads(path.read_text());updated['files']['engine.py']=hashlib.sha256(source.read_bytes()).hexdigest()
    path.write_text(json.dumps(updated))
    with pytest.raises(ValueError,match='manifest differs'):release.verify_release(path,expected_sha256=digest)


def test_unchanged_package_can_select_same_engine_from_paper_or_live(tmp_path,monkeypatch):
    path,digest,_=engine_fixture(tmp_path,monkeypatch)
    package=tmp_path/'paper_trading/models/example';package.mkdir(parents=True)
    (package/'strategy.py').write_text('def decision(value):\n    return value > 0\n')
    (package/'contract.json').write_text(json.dumps(dict(engine_release='release.json',engine_sha256=digest)))
    before={p.name:p.read_bytes() for p in package.iterdir()}
    destination=tmp_path/'live_trading/live_model/example';destination.parent.mkdir(parents=True)
    assert package.resolve().is_relative_to(tmp_path.resolve()) and destination.resolve().is_relative_to(tmp_path.resolve())
    package.rename(destination)
    assert {p.name:p.read_bytes() for p in destination.iterdir()}==before
    contract=json.loads((destination/'contract.json').read_text())
    release.verify_release(tmp_path/contract['engine_release'],expected_sha256=contract['engine_sha256'])


def test_release_rejects_source_paths_outside_project(tmp_path,monkeypatch):
    path,digest,_=engine_fixture(tmp_path,monkeypatch)
    manifest=json.loads(path.read_text());manifest['files']={'../outside.py':'0'*64}
    path.write_text(json.dumps(manifest));digest=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='escapes project'):release.verify_release(path,expected_sha256=digest)
