import json
from decimal import Decimal

import pytest

from models.trend_following_preny_profile_15m.pilot_stop_probe import run_probe,require_worker_off
from test_futures_execution import FakeVenue,NOW,Crash
from live_engine.adapters.binance_futures_execution import VenueError


@pytest.fixture
def probe_env(tmp_path,monkeypatch):
    monkeypatch.setattr('live_engine.futures_execution.time.time',lambda:NOW/1000)
    monkeypatch.setattr('time.sleep',lambda _:None)
    return FakeVenue(),tmp_path/'native-stop-probe.jsonl'


def test_one_close_only_stop_then_cancel_no_entry(probe_env):
    v,path=probe_env;r=run_probe(v,path)
    assert r['passed'] and r['cancellation_confirmed']
    assert r['trigger_price']=='900.00'
    assert len(v.posts)==1 and v.posts[0][0]=='STOP'
    p=v.posts[0][1]
    assert p['closePosition']=='true' and 'quantity' not in p and 'reduceOnly' not in p
    assert v.position==0 and v.wallet==Decimal('200')
    assert json.loads(path.read_text().splitlines()[0])['params']['clientAlgoId']==r['client_id']
    r2=run_probe(v,path)
    assert r2['recovery_only'] and len(v.posts)==1


def test_post_timeout_reconciles_and_cancels_same_id(probe_env):
    v,path=probe_env;v.fail['STOP']='timeout'
    r=run_probe(v,path)
    assert r['passed'] and len(v.posts)==1 and v.position==0


def test_rejected_probe_never_opens_position(probe_env):
    v,path=probe_env;v.fail['STOP']='rejected'
    r=run_probe(v,path)
    assert r['rejected'] and not r['passed'] and len(v.posts)==1 and v.position==0
    run_probe(v,path)
    assert len(v.posts)==1


def test_validation_error_still_cancels_order(probe_env):
    v,path=probe_env
    submit=v.submit
    def wrong_response(role,p):
        r=submit(role,p);r['closePosition']=False;return r
    v.submit=wrong_response
    r=run_probe(v,path)
    assert not r['passed'] and r['cancellation_confirmed'] and len(v.posts)==1
    assert all(x.get('algoStatus')!='NEW' for x in v.orders.values())


def test_recovery_from_sending_never_posts_again(probe_env):
    v,path=probe_env;v.fail['STOP']='after_crash'
    with pytest.raises(Crash):run_probe(v,path)
    r=run_probe(v,path)
    assert r['passed'] and r['recovery_only'] and len(v.posts)==1


def test_nonflat_account_stops_before_post(probe_env):
    v,path=probe_env;v.position=Decimal('.01')
    with pytest.raises(ValueError,match='flat'):run_probe(v,path)
    assert not v.posts


def test_foreign_journal_not_used_for_cancellation(probe_env):
    v,path=probe_env;path.write_text(json.dumps({'event':'other'})+'\n')
    with pytest.raises(ValueError,match='Foreign'):run_probe(v,path)
    assert not v.posts and not v.cancels


def test_worker_guard_requires_current_off_and_no_activity(probe_env,tmp_path):
    import sqlite3
    config=tmp_path/'policy.json';path=tmp_path/'worker.sqlite'
    config.write_text(json.dumps(dict(initial_equity='200',execution_enabled=False,accept_new_entries=False)))
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE metadata(key TEXT,value TEXT); CREATE TABLE orders(id INT); CREATE TABLE fills(id INT);')
        db.executemany('INSERT INTO metadata VALUES (?,?)',[('owner','binance_futures_pilot'),('worker_armed','0'),('worker_heartbeat_ms',str(NOW))])
    require_worker_off(config,path)
    with sqlite3.connect(path) as db:db.execute("UPDATE metadata SET value='1' WHERE key='worker_armed'")
    with pytest.raises(ValueError,match='disarmed'):require_worker_off(config,path)


def test_crash_before_http_never_reposts_even_when_query_not_found(probe_env):
    v,path=probe_env;v.fail['STOP']='before_crash'
    with pytest.raises(Crash):run_probe(v,path)
    r=run_probe(v,path)
    assert not r['passed'] and r['recovery_only'] and len(v.posts)==1 and v.position==0


def test_undocumented_4509_is_not_whitelisted_or_reposted(probe_env):
    v,path=probe_env
    def rejected(role,params):
        v.posts.append((role,params))
        raise VenueError(400,-4509,uncertain=True,reason='Example safe venue reason')
    v.submit=rejected
    r=run_probe(v,path)
    assert not r['passed'] and not r['rejected'] and r['post_error']['venue_code']==-4509
    assert r['post_error']['venue_reason']=='Example safe venue reason'
    assert r['after']['positions']==0 and r['after']['algo_orders']==0
    recovered=run_probe(v,path)
    assert recovered['post_error']['venue_reason']=='Example safe venue reason'
    assert len(v.posts)==1
