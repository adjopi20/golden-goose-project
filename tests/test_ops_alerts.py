import json
from pathlib import Path
import sqlite3

import pytest
import requests

from live_engine import ops_alerts as a
from live_engine.futures_execution import FuturesExecutor
from live_engine.futures_store import FuturesStore
from test_futures_execution import policy,intent,NOW,FakeVenue

TOKEN='123:'+('S'*24)
CHAT=42


class HTTP:
    def __init__(self,status=200,payload=None,error=None):
        self.status=status;self.payload=payload if payload is not None else {'ok':True,'result':{'message_id':3,'chat':{'id':CHAT}}}
        self.error=error;self.calls=[]
    def post(self,url,**kw):
        self.calls.append((url,kw))
        if self.error:raise self.error
        outer=self
        class Response:
            status_code=outer.status
            def json(self):return outer.payload
        return Response()


@pytest.fixture
def ledger(tmp_path,monkeypatch):
    monkeypatch.setattr('time.time',lambda:NOW/1000)
    s=FuturesStore(tmp_path/'executor.sqlite',policy());v=FakeVenue();e=FuturesExecutor(s,v,policy())
    with s.transaction():
        s.set_meta('worker_heartbeat_ms',NOW);s.set_meta('worker_armed','1');s.set_meta('worker_error','')
    target=dict(id='eth',label='ETH pilot',kind='futures_executor',path=str(s.path),expected_metadata={'owner':'binance_futures_pilot','schema':'1'},heartbeat_key='worker_heartbeat_ms',max_age_seconds=60)
    yield s,v,e,target
    s.close()


@pytest.fixture
def alerts(tmp_path):
    s=a.AlertStore(tmp_path/'alerts.sqlite','test-monitor');s.acquire_writer()
    yield s
    s.close()


def test_healthy_ledger_readonly_no_mutation(ledger):
    s,v,e,target=ledger
    before=dict(s.db.execute('SELECT key,value FROM metadata'))
    assert a.inspect_target(target,NOW)==([],True)
    assert dict(s.db.execute('SELECT key,value FROM metadata'))==before
    assert not v.posts


def test_missing_target_not_created(tmp_path):
    t=dict(path=str(tmp_path/'absent.sqlite'))
    issues,valid=a.inspect_target(t,NOW)
    assert not valid and issues[0]['code']=='DB_UNREADABLE' and not Path(t['path']).exists()


def test_identity_mismatch_and_stale_clock(ledger):
    s,v,e,t=ledger
    wrong=dict(t,expected_metadata={'owner':'other'})
    issues,valid=a.inspect_target(wrong,NOW);assert not valid and issues[0]['code']=='IDENTITY_MISMATCH'
    assert 'HEARTBEAT_STALE' in {x['code'] for x in a.inspect_target(t,NOW+61000)[0]}
    assert 'CLOCK_FUTURE' in {x['code'] for x in a.inspect_target(t,NOW-6000)[0]}


def test_generic_source_heartbeat_has_no_futures_schema_requirement(tmp_path):
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata(key TEXT,value TEXT)')
        db.executemany('INSERT INTO metadata VALUES (?,?)',[('owner','source'),('poll',str(NOW))])
    t=dict(path=str(path),kind='heartbeat',expected_metadata={'owner':'source'},heartbeat_key='poll',max_age_seconds=180)
    assert a.inspect_target(t,NOW)==([],True)


def test_persisted_errors_are_classified_without_secret_text(ledger):
    s,v,e,t=ledger
    s.set_meta('worker_armed','0');s.set_meta('worker_error','secret-api-key-and-signed-url');s.set_meta('entries_blocked','raw-private-details')
    issues,valid=a.inspect_target(t,NOW)
    assert valid and {x['code'] for x in issues}=={'EXECUTOR_DISARMED','WORKER_ERROR','ENTRIES_BLOCKED'}
    assert 'secret-api' not in json.dumps(issues) and 'raw-private' not in json.dumps(issues)


def test_unconfirmed_stop_unknown_order_and_missed_deadline(ledger):
    s,v,e,t=ledger;e.offer(intent(),NOW);e.cycle(NOW)
    s.db.execute("UPDATE orders SET state='UNKNOWN',error='private details' WHERE role='STOP'")
    issues,_=a.inspect_target(t,NOW)
    assert {'STOP_UNCONFIRMED','ORDER_UNRESOLVED'} <= {x['code'] for x in issues}
    plan=json.loads(s.active()['plan']);plan['force_exit_ms']=NOW-15001
    s.db.execute('UPDATE sessions SET plan=?',(json.dumps(plan),))
    assert 'DEADLINE_UNRESOLVED' in {x['code'] for x in a.inspect_target(t,NOW)[0]}


def test_terminal_finished_stop_is_not_misreported_unprotected(ledger):
    s,v,e,t=ledger;e.offer(intent(),NOW);e.cycle(NOW)
    s.db.execute("UPDATE orders SET state='FINISHED' WHERE role='STOP'")
    assert 'STOP_UNCONFIRMED' not in {x['code'] for x in a.inspect_target(t,NOW)[0]}


def observe(s,t,issues,now,valid=True):s.observe(t,issues,valid,now,1800000,'test-monitor')


def test_transient_error_is_debounced_then_deduped_and_reminded(alerts,ledger):
    t=ledger[-1];i=[a.issue('ORDER_UNRESOLVED','Check order',30)]
    observe(alerts,t,i,NOW);observe(alerts,t,i,NOW+29000)
    assert alerts.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    observe(alerts,t,i,NOW+30000);observe(alerts,t,i,NOW+40000)
    assert alerts.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==1
    observe(alerts,t,i,NOW+1830000)
    assert alerts.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==2


def test_handled_transient_is_quiet(alerts,ledger):
    t=ledger[-1];observe(alerts,t,[a.issue('WORKER_ERROR','temporary',60)],NOW)
    observe(alerts,t,[],NOW+30000)
    assert alerts.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_failed_read_never_clears_prior_unresolved_condition(alerts,ledger):
    t=ledger[-1];observe(alerts,t,[a.issue('ENTRIES_BLOCKED','blocked')],NOW)
    observe(alerts,t,[a.issue('DB_UNREADABLE','unreadable',30)],NOW+10000,False)
    assert alerts.db.execute("SELECT active FROM conditions WHERE code='ENTRIES_BLOCKED'").fetchone()[0]==1
    assert not alerts.db.execute("SELECT 1 FROM outbox WHERE kind='RECOVERY'").fetchone()


def test_recovery_sent_once_and_supersedes_undelivered_alarm(alerts,ledger):
    t=ledger[-1];observe(alerts,t,[a.issue('ENTRIES_BLOCKED','blocked')],NOW)
    observe(alerts,t,[],NOW+1000);observe(alerts,t,[],NOW+2000)
    http=HTTP();assert alerts.deliver_one(http,TOKEN,CHAT,NOW+2000)
    assert len(http.calls)==1 and 'RECOVERY' in http.calls[0][1]['json']['text']
    assert alerts.db.execute("SELECT status FROM outbox WHERE kind='OPEN'").fetchone()[0]=='SUPERSEDED'
    assert not alerts.deliver_one(http,TOKEN,CHAT,NOW+3000)


def test_outbox_failure_retained_rate_limit_global_and_success_recovers(alerts,ledger):
    t=ledger[-1];observe(alerts,t,[a.issue('A','first'),a.issue('B','second')],NOW)
    bad=HTTP(status=429,payload={'ok':False,'description':TOKEN,'parameters':{'retry_after':120}})
    assert not alerts.deliver_one(bad,TOKEN,CHAT,NOW)
    assert not alerts.deliver_one(bad,TOKEN,CHAT,NOW+119000) and len(bad.calls)==1
    assert alerts.db.execute("SELECT COUNT(*) FROM outbox WHERE status='PENDING'").fetchone()[0]==2
    good=HTTP();assert alerts.deliver_one(good,TOKEN,CHAT,NOW+120000)
    assert dict(alerts.db.execute('SELECT key,value FROM metadata'))['delivery_error']==''


def test_pending_outbox_survives_restart_and_has_single_writer(alerts,ledger):
    t=ledger[-1];observe(alerts,t,[a.issue('ENTRIES_BLOCKED','blocked')],NOW)
    path=alerts.path
    second=a.AlertStore(path,'test-monitor')
    with pytest.raises((OSError,IOError)):second.acquire_writer()
    second.close()
    alerts.writer_lock.close();alerts.writer_lock=None
    other=a.AlertStore(path,'test-monitor');other.acquire_writer()
    try:
        assert other.deliver_one(HTTP(),TOKEN,CHAT,NOW+1)
        assert not other.deliver_one(HTTP(),TOKEN,CHAT,NOW+2)
    finally:other.close()


def test_cannot_use_trading_db_for_alert_state(ledger):
    s,v,e,t=ledger
    with pytest.raises(ValueError,match='identity'):a.AlertStore(s.path,'test-monitor')
    assert dict(s.db.execute('SELECT key,value FROM metadata'))['owner']=='binance_futures_pilot'


@pytest.mark.parametrize('status,payload',[(401,{'ok':False,'description':TOKEN}),(200,{'ok':False}),
    (200,{'ok':True,'result':{'message_id':4,'chat':{'id':99}}}),(302,{}),(200,[])])
def test_telegram_rejection_and_wrong_recipient_redacted(status,payload):
    with pytest.raises(a.DeliveryError) as e:a.send_telegram(HTTP(status,payload),TOKEN,CHAT,'Test')
    assert TOKEN not in str(e.value) and 'https:' not in str(e.value)


def test_telegram_network_exception_redacted_and_no_automatic_retry():
    http=HTTP(error=requests.Timeout('https://api.telegram.org/bot'+TOKEN))
    with pytest.raises(a.DeliveryError) as e:a.send_telegram(http,TOKEN,CHAT,'Test')
    assert TOKEN not in str(e.value) and len(http.calls)==1


def test_telegram_request_is_plain_and_disallows_redirects():
    http=HTTP();assert a.send_telegram(http,TOKEN,CHAT,'TEST no orders')==3
    _,kw=http.calls[0]
    assert kw['allow_redirects'] is False and 'parse_mode' not in kw['json'] and kw['json']['chat_id']==CHAT


def test_test_action_sends_only_test_without_ledger_access(monkeypatch,capsys):
    monkeypatch.setenv('TELEGRAM_ENABLED','true');monkeypatch.setenv('TELEGRAM_BOT_TOKEN',TOKEN);monkeypatch.setenv('TELEGRAM_CHAT_ID',str(CHAT))
    http=HTTP();http.close=lambda:None
    monkeypatch.setattr(requests,'Session',lambda:http)
    monkeypatch.setattr(a,'AlertStore',lambda *args:pytest.fail('No state DB for TEST'))
    monkeypatch.setattr(a,'load_config',lambda *args:pytest.fail('No target config read for TEST'))
    monkeypatch.setattr('sys.argv',['alerts','--config','unused','--action','test'])
    assert a.main()==0
    assert json.loads(capsys.readouterr().out)['telegram_api_accepted'] and 'TEST' in http.calls[0][1]['json']['text']


def test_alert_health_freshness_and_delivery_error(alerts):
    alerts.set_meta('heartbeat_ms',NOW)
    assert a.health(alerts.path,NOW)
    assert not a.health(alerts.path,NOW+90000)
    alerts.set_meta('delivery_error','telegram_transport_or_json')
    assert not a.health(alerts.path,NOW)


def test_deploy_is_readonly_observer_no_order_keys_or_socket():
    root=Path(__file__).resolve().parents[1]
    config=a.load_config(root/'models/trend_following_preny_profile_15m/deploy/pilot.alerts.json')
    assert len(config['targets'])==2
    compose=(root/'models/trend_following_preny_profile_15m/deploy/pilot.alerts.yaml').read_text()
    assert '/observed/executor:ro' in compose and '/observed/strategy:ro' in compose
    assert 'BINANCE_API_KEY' not in compose and 'docker.sock' not in compose and 'key.pem' not in compose
    assert 'timeout: 15s' in compose and 'cpus: 0.1' in compose
    context=(root/'live_engine/Dockerfile.alerts.dockerignore').read_text()
    assert 'adapters' not in context and 'bitcoin_onchain' not in context


def test_discover_private_chats_without_consuming_or_printing_messages():
    http=HTTP();payloads=iter([{'is_bot':True},{'url':''},[
        {'message':{'text':'PRIVATE MESSAGE','chat':{'id':42,'type':'private'}}},
        {'message':{'chat':{'id':99,'type':'group'}}}]])
    post=http.post
    def next_response(url,**kw):
        http.payload={'ok':True,'result':next(payloads)}
        return post(url,**kw)
    http.post=next_response
    result=a.discover(http,TOKEN)
    assert result['private_chat_ids']==[42] and result['updates_acknowledged'] is False
    assert 'PRIVATE MESSAGE' not in json.dumps(result)
    assert all('offset' not in kw['json'] for _,kw in http.calls)


def test_discover_does_not_remove_existing_webhook():
    http=HTTP();responses=iter([{'is_bot':True},{'url':'https://private.example'}]);post=http.post
    def next_response(url,**kw):
        http.payload={'ok':True,'result':next(responses)};return post(url,**kw)
    http.post=next_response
    with pytest.raises(ValueError,match='NOT removed'):a.discover(http,TOKEN)
    assert len(http.calls)==2


def test_missing_credentials_do_not_read_secret_files(monkeypatch):
    monkeypatch.setenv('TELEGRAM_ENABLED','true');monkeypatch.delenv('TELEGRAM_BOT_TOKEN',raising=False)
    with pytest.raises(ValueError,match='token'):a.credentials()


def test_recurrence_does_not_deliver_old_recovery_or_old_alarm(alerts,ledger):
    t=ledger[-1];bad=[a.issue('ENTRIES_BLOCKED','blocked')]
    observe(alerts,t,bad,NOW);observe(alerts,t,[],NOW+1000)
    observe(alerts,t,bad,NOW+2000)
    http=HTTP();assert alerts.deliver_one(http,TOKEN,CHAT,NOW+3000)
    text=http.calls[0][1]['json']['text']
    assert 'CRITICAL' in text and a.stamp(NOW+2000) in text
    assert alerts.db.execute("SELECT COUNT(*) FROM outbox WHERE status='SUPERSEDED'").fetchone()[0]==2
    assert not alerts.deliver_one(http,TOKEN,CHAT,NOW+4000)


@pytest.mark.parametrize('action',['health','status'])
def test_health_status_do_not_import_http_stack(alerts,action):
    import subprocess
    import sys
    import time
    alerts.set_meta('heartbeat_ms',int(time.time()*1000))
    script='''
import importlib.abc,runpy,sys
class NoHTTP(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in ('requests','urllib3'):
            raise AssertionError('Health/status must not import HTTP stack')
sys.meta_path.insert(0,NoHTTP())
database,action=sys.argv[1:]
sys.argv=['ops_alerts','--config','unused','--database',database,'--action',action]
runpy.run_module('live_engine.ops_alerts',run_name='__main__')
'''
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,'-B','-c',script,str(alerts.path),action],cwd=root,
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_status_before_first_start_is_explicit_and_does_not_create_db(tmp_path,monkeypatch,capsys):
    path=tmp_path/'not-created-yet.sqlite'
    monkeypatch.setattr('sys.argv',['alerts','--config','unused','--database',str(path),'--action','status'])
    assert a.main()==0
    assert json.loads(capsys.readouterr().out)['status']=='starting'
    assert not path.exists() and not a.health(path,NOW)
