import ast
import asyncio
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest

from live_engine.account_owner import AccountOwner
from live_engine.account_store import AccountStore
from live_engine.account_worker import load_config, status
from live_engine.service_health import check as healthy
from live_engine.strategy_store import StrategyStore
from market_data.broker import flush_outbox
from trading_core.contracts import make_event, make_intention, content_hash
from trading_core.paper_execution import Config, Portfolio, on_tick

ROOT=Path(__file__).resolve().parents[1]


def config(venue='binance'):
    return dict(schema_version=1,mode='paper-shadow',environment='server-paper',venue=venue,
        account_group='test_'+venue,strategy_config_hash='cfg',allocations=[dict(id='eth',symbol='ETHUSDC',
        instrument_id='ETHUSDC' if venue=='binance' else '0',currency='USDC',contract='contract-v1',
        model_id='model',model_version='v1',allowed_risks=[.0025,.005],
        execution=dict(initial_equity=1000,risk_fraction=.005,fee_bps=4,max_leverage=5,
            max_entry_delay_ms=300000,tp1_r=1,tp1_fraction=.1))])


def intention(venue='binance', **kw):
    fields=dict(venue=venue,product='futures',instrument_id='ETHUSDC' if venue=='binance' else '0',
        environment='server-paper',account_id='eth',allocation_id='eth',model_id='model',
        model_version='v1',contract_version='contract-v1',config_hash='cfg',feature_version='f1',
        calibration_id='cal',decision_snapshot_ref='snapshot',session_id='2026-10-06',
        direction='long',feature_as_of_ms=1000,signal_timestamp_ms=1000,eligible_timestamp_ms=1000,
        expires_timestamp_ms=3000,force_exit_timestamp_ms=5000,risk_fraction=.005,
        initial_stop=90.,entry_reference=100.,exit_policy='initial_stop_or_time',source_sample_id='sample')
    return make_intention(**{**fields,**kw})


def trade(seq,ts,price):
    return dict(symbol='ETHUSDC',agg_trade_id=seq,timestamp_ms=ts,price=price,quantity=1.,buy=True)


def event(ticks=None, venue='binance', quality='complete', **kw):
    ts=ticks[-1]['timestamp_ms'] if ticks else 2000
    payload=(dict(trades=ticks,first_source_sequence=ticks[0]['agg_trade_id'],
        last_source_sequence=ticks[-1]['agg_trade_id']) if ticks else {'reason':'feed gap'})
    fields=dict(venue=venue,product='futures',instrument_id='ETHUSDC' if venue=='binance' else '0',
        source='test',event_type='trade' if ticks else 'coverage',event_timestamp_ms=ts,
        available_at_ms=ts,received_timestamp_ms=ts,stream_sequence=ticks[-1]['agg_trade_id'] if ticks else 99,
        source_sequence=ticks[-1]['agg_trade_id'] if ticks else None,source_epoch='epoch',
        feature_version='f1',quality=quality,payload=payload)
    return make_event(**{**fields,**kw})


def open_owner(path,venue='binance', cfg=None):
    cfg=cfg or config(venue)
    s=AccountStore(path,cfg,'test'); return s,AccountOwner(s,cfg)


def records(s,kind):
    return [json.loads(r[0]) for r in s.db.execute('SELECT payload FROM journal WHERE kind=? ORDER BY id',(kind,))]


def test_shared_mechanics_are_exact_ast_extraction():
    expected={'Config':'304fd2ebbfe01883a0d7e97a0f0b0ce3793ae3e1fa3c8c20811df0e09fd1d613',
        'Portfolio':'05e02cddab70e88b8ec8ba3e9ef96d70c832a43c846294a17a708154ca365997',
        'close_position':'559a41554da4a14c0936e1d0c56318793e1cecf98c59a6d60ef5cda14c0c627d',
        'take_partial':'181f36f703ee04e9796572050c3001240fe4223e25723972e8067e6f3dcc6df3',
        'on_tick':'b1045cd3879157d4979ddd3d3f7595ffb93056483945f8c844b9d46c06fb3d45',
        'validate_config':'06f5355c9ad365779a1c57a241f4200922b8b96209585e97863efa9b8243f220'}
    tree=ast.parse((ROOT/'trading_core/paper_execution.py').read_text())
    actual={n.name:hashlib.sha256(ast.dump(n,include_attributes=False).encode()).hexdigest()
        for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in expected}
    assert actual==expected


@pytest.mark.parametrize('venue',['binance','lighter'])
def test_restart_partial_time_exact_and_balanced_cash(tmp_path,venue):
    path=tmp_path/'account.sqlite'; s,o=open_owner(path,venue)
    p=intention(venue); assert o.intention(p,1000)=='pending'
    ticks=[trade(1,1001,100.),trade(2,1500,110.),trade(3,1501,111.),trade(4,5000,105.)]
    o.event(event(ticks[:2],venue),1500)
    assert o.manager.status()[0]['partial_trigger']
    s.close(); s,o=open_owner(path,venue)
    assert o.intention(p,1501)=='duplicate'
    o.event(event(ticks[2:3],venue),1501)
    assert o.manager.status()[0]['closed_equity']==1000
    assert s.cash('eth')!=Decimal(1000)
    o.clock(5000); s.close(); s,o=open_owner(path,venue)
    assert s.db.execute('SELECT status FROM deadlines').fetchone()[0]=='awaiting_next_native_print'
    o.event(event(ticks[3:],venue),5000)
    reference=Portfolio(label='eth',signals=[dict(sample_id='sample',session_day='2026-10-06',
        strategy='v1',route='model',symbol='ETHUSDC',direction='long',stop=90.,entry_reference=100.,
        feature_as_of_ms=1000,entry_eligible_timestamp_ms=1000,entry_deadline_timestamp_ms=3000,
        force_exit_timestamp_ms=5000)],equity=1000.)
    for tick in ticks: on_tick(reference,tick,Config(tp1_r=1,tp1_fraction=.1))
    assert records(s,'fill')==reference.orders
    assert records(s,'trade')==reference.trades
    assert float(s.cash('eth'))==pytest.approx(reference.equity,abs=1e-9)
    assert not s.db.execute('SELECT * FROM reservations').fetchall()
    assert s.db.execute('SELECT status FROM requests').fetchone()[0]=='closed'
    by_key={}
    for key,amount in s.db.execute('SELECT journal_key,amount FROM postings'):
        by_key[key]=by_key.get(key,Decimal(0))+Decimal(amount)
    assert all(v==0 for v in by_key.values())
    s.close()


def test_stop_trigger_restart_and_redelivery_idempotent(tmp_path):
    path=tmp_path/'a.sqlite';s,o=open_owner(path);o.intention(intention(),1000)
    e=event([trade(1,1001,100.),trade(2,2000,89.)]);s.anchor('exec',0)
    assert s.apply('exec',1,e['event_id'],e,2000,o.event)
    s.close();s,o=open_owner(path)
    assert not s.apply('exec',2,e['event_id'],e,2001,o.event)
    assert o.manager.status()[0]['stop_trigger']
    o.event(event([trade(3,2002,88.)]),2002)
    assert len(records(s,'fill'))==2
    assert records(s,'trade')[0]['exit_reason']=='initial_stop_next_print'
    assert float(s.cash('eth'))==pytest.approx(o.manager.status()[0]['closed_equity'])
    with pytest.raises(ValueError,match='Conflicting'):
        s.apply('exec',3,e['event_id'],{**e,'quality':'partial'},2002,o.event)
    s.close()


def test_atomic_receipt_reservation_rollback_and_duplicate_conflict(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite');s.anchor('intent',0);p=intention()
    def fail(p,now): o.intention(p,now);raise RuntimeError('crash before commit')
    with pytest.raises(RuntimeError): s.apply('intent',1,p['intention_id'],p,1000,fail)
    assert not s.db.execute('SELECT * FROM requests').fetchall()
    assert not s.db.execute('SELECT * FROM reservations').fetchall()
    assert s.checkpoint('intent')==0
    assert s.apply('intent',1,p['intention_id'],p,1000,o.intention)
    assert not s.apply('intent',2,p['intention_id'],p,1001,o.intention)
    assert o.intention(p,1001)=='duplicate'
    with pytest.raises(ValueError,match='Conflicting'): o.intention({**p,'risk_fraction':.0025},1001)
    assert len(s.db.execute('SELECT * FROM reservations').fetchall())==1
    s.close()


def test_fill_and_posting_rollback(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite');o.intention(intention(),1000);s.anchor('exec',0)
    e=event([trade(1,1001,100.)])
    def fail(e,now):o.event(e,now);raise RuntimeError('power lost')
    with pytest.raises(RuntimeError):s.apply('exec',1,e['event_id'],e,1001,fail)
    assert not records(s,'fill') and s.cash('eth')==1000 and not o.manager.status()[0]['position']
    assert s.checkpoint('exec')==0
    s.apply('exec',1,e['event_id'],e,1001,o.event)
    assert len(records(s,'fill'))==1 and s.cash('eth')<1000
    s.close()


@pytest.mark.parametrize('change',[dict(venue='lighter'),dict(risk_fraction=.01),dict(contract_version='wrong'),
    dict(config_hash='wrong'),dict(model_version='v2'),dict(instrument_id='BNBUSDC')])
def test_contract_binding_fails_before_reservation(tmp_path,change):
    s,o=open_owner(tmp_path/'a.sqlite')
    with pytest.raises(ValueError):o.intention(intention(**change),1000)
    assert not s.db.execute('SELECT * FROM requests').fetchall()
    assert not s.db.execute('SELECT * FROM reservations').fetchall();s.close()


def test_no_retroactive_fill_expiry_and_no_deadline_extension(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite')
    o.event(event([trade(1,1200,100.)]),1200)
    assert o.intention(intention(),1300)=='pending'
    assert json.loads(s.db.execute('SELECT payload FROM intents').fetchone()[0])['signal']['entry_eligible_timestamp_ms']==1300
    o.event(event([trade(2,1299,100.)]),1301)
    assert not records(s,'fill')
    o.clock(3001)
    o.event(event([trade(3,3100,100.)]),3100)
    assert not records(s,'fill') and not s.db.execute('SELECT * FROM reservations').fetchall()
    s.close()
    s,o=open_owner(tmp_path/'expired.sqlite')
    assert o.intention(intention(),3001)=='rejected';assert not records(s,'fill');s.close()


def test_isolation_and_single_instrument_owner(tmp_path):
    cfg=config(); other=deepcopy(cfg['allocations'][0]);other['id']='eth2';cfg['allocations'].append(other)
    s,o=open_owner(tmp_path/'a.sqlite',cfg=cfg);o.intention(intention(),1000)
    p=intention(account_id='eth2',allocation_id='eth2')
    assert o.intention(p,1000)=='rejected'
    assert s.cash('eth2')==1000
    o.event(event([trade(1,1001,100.)]),1001)
    assert o.manager.status()[0]['position'] and not o.manager.status()[1]['position'];s.close()


def test_sizing_price_risk_not_notional_geometric_and_cap(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite');o.intention(intention(),1000)
    o.event(event([trade(1,1001,100.)]),1001)
    p=o.manager.status()[0]['position'];assert p['quantity']==.5 and p['actual_initial_risk']==5
    o.event(event([trade(2,5000,105.)]),5000);eq=o.manager.status()[0]['closed_equity']
    p2=intention(session_id='2026-10-07',feature_as_of_ms=6000,signal_timestamp_ms=6000,
        eligible_timestamp_ms=6000,expires_timestamp_ms=7000,force_exit_timestamp_ms=9000,source_sample_id='sample2')
    o.intention(p2,6000);o.event(event([trade(3,6001,100.)]),6001)
    assert o.manager.status()[0]['position']['actual_initial_risk']==pytest.approx(eq*.005);s.close()
    s,o=open_owner(tmp_path/'cap.sqlite');o.intention(intention(initial_stop=99.99),1000)
    o.event(event([trade(1,1001,100.)]),1001)
    pos=o.manager.status()[0]['position'];assert pos['leverage_at_entry']==5 and pos['actual_initial_risk']<5;s.close()


def test_time_exit_duty_without_print_and_no_invented_price(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite');o.intention(intention(),1000)
    o.event(event([trade(1,1001,100.)]),1001);o.clock(5000);o.clock(5500)
    assert len(records(s,'fill'))==1 and len(s.db.execute('SELECT * FROM deadlines').fetchall())==1
    o.event(event([trade(2,6000,103.)]),6000)
    assert records(s,'fill')[-1]['timestamp_ms']==6000
    assert s.db.execute('SELECT status FROM deadlines').fetchone()[0]=='filled';s.close()


def test_gap_cancels_pending_but_open_protection_continues(tmp_path):
    s,o=open_owner(tmp_path/'pending.sqlite');o.intention(intention(),1000)
    o.event(event(quality='gap'),2000)
    o.event(event([trade(1,2001,100.)]),2001)
    assert not records(s,'fill') and not s.db.execute('SELECT * FROM reservations').fetchall();s.close()
    s,o=open_owner(tmp_path/'open.sqlite');o.intention(intention(),1000)
    o.event(event([trade(1,1001,100.)]),1001)
    o.event(event([trade(3,2001,89.),trade(4,2002,88.)]),2002)
    assert records(s,'trade')[0]['exit_reason']=='initial_stop_next_print'
    assert s.db.execute('SELECT status FROM requests').fetchone()[0]=='closed_uncertain'
    assert records(s,'execution_uncertain');s.close()


def test_epoch_retention_identity_fail_closed(tmp_path):
    path=tmp_path/'a.sqlite';s,o=open_owner(path)
    o.event(event([trade(1,1001,100.)]),1001)
    with pytest.raises(ValueError,match='epoch changed'):o.event(event([trade(2,1002,100.)],source_epoch='other'),1002)
    s.anchor('exec',1)
    with pytest.raises(ValueError,match='retention gap'):s.check_retention('exec',3)
    s.close()
    with pytest.raises(ValueError,match='identity changed'):AccountStore(path,config(),'new-release')


@pytest.mark.parametrize('mutate',[lambda e:e['payload'].update(last_source_sequence=42),
    lambda e:e['payload']['trades'][0].update(symbol='BTCUSDC'),
    lambda e:e['payload']['trades'][0].update(price=float('nan'))])
def test_malformed_batch_no_mutation(tmp_path,mutate):
    s,o=open_owner(tmp_path/'a.sqlite');p=intention();o.intention(p,1000)
    e=event([trade(1,1001,100.)]);mutate(e)
    with pytest.raises(ValueError):o.event(e,1001)
    assert not records(s,'fill') and s.cash('eth')==1000;s.close()


def test_outbox_publish_retry_and_ack(tmp_path):
    s=StrategyStore(tmp_path/'s.sqlite',venue='binance',config_hash='cfg',calibration_hash='cal',code_version='test')
    with s.transaction():s.queue_intention(intention())
    class Broker:
        async def publish(self,*args,**kw):raise RuntimeError('disconnect after commit')
    with pytest.raises(RuntimeError):asyncio.run(flush_outbox(s,Broker()))
    assert len(s.pending())==1
    seen=[]
    class GoodBroker:
        async def publish(self,subject,payload,**kw):seen.append((subject,json.loads(payload),kw))
    assert asyncio.run(flush_outbox(s,GoodBroker()))==1
    assert not s.pending() and seen[0][2]['headers']['Nats-Msg-Id']==intention()['intention_id'];s.close()


def test_account_runtime_does_not_import_research_or_strategy():
    code='import live_engine.account_worker,sys; forbidden=("pandas","numpy","pyarrow","backtest_engine","models","live_engine.adapters"); assert not any(k==p or k.startswith(p+".") for p in forbidden for k in sys.modules)'
    subprocess.run([sys.executable,'-c',code],cwd=ROOT,check=True)


def test_deployment_registry_matches_frozen_configs():
    for venue in ('binance','lighter'):
        cfg=load_config(ROOT/f'models/trend_following_preny_profile_15m/deploy/phase4/{venue}.account.json')
        frozen=json.loads((ROOT/f'models/trend_following_preny_profile_15m/deploy/{venue}.paper.yaml').read_text())
        assert cfg['strategy_config_hash']==content_hash(frozen)
        for a,b in zip(cfg['allocations'],frozen['accounts']):
            assert all(a[k]==b[k] for k in ('id','symbol','contract','allowed_risks','execution'))
    compose=(ROOT/'models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml').read_text()
    assert 'phase3_' not in compose and '--publish-intentions' in compose


def test_health_stale_future_readonly(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite');o.clock(1000)
    assert healthy(s.path,['last_timer_ms'],now_ms=1001)
    assert not healthy(s.path,['last_timer_ms'],now_ms=1000+180001)
    assert not healthy(s.path,['missing'],now_ms=1000)
    assert not healthy(s.path,['last_timer_ms'],now_ms=999)
    assert not healthy(s.path,[],now_ms=1000);s.close()


def test_readonly_account_audit_and_deadline_delay_record(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite');o.intention(intention(),1000)
    o.event(event([trade(1,1001,100.)]),1001);o.clock(5000)
    assert status(s.path)['accounting_checks']['status']=='pass'
    o.event(event([trade(2,6000,103.)]),6000)
    report=status(s.path)
    assert report['accounting_checks']['status']=='pass' and report['journal_counts']['trade']==1
    assert records(s,'exit_deadline_execution')[0]['delay_ms']==1000
    with s.transaction():s.db.execute("UPDATE postings SET amount='99' WHERE journal_key='capital:eth' AND book='cash'")
    assert status(s.path)['accounting_checks']['status']=='fail';s.close()


def test_late_cross_stream_gap_notice_marks_overlap_close_uncertain(tmp_path):
    s,o=open_owner(tmp_path/'a.sqlite','lighter');o.intention(intention('lighter'),1000)
    o.event(event([trade(1,1001,100.),trade(9,2000,89.),trade(12,2001,88.)],'lighter'),2001)
    assert s.db.execute('SELECT status FROM requests').fetchone()[0]=='closed'
    gap=event(venue='lighter',quality='gap',payload=dict(previous_cursor=dict(timestamp_ms=1500)))
    o.event(gap,2100)
    assert status(s.path)['uncertain_closes']==1 and status(s.path)['input_blocks']
    assert status(s.path)['accounting_checks']['status']=='pass';s.close()
