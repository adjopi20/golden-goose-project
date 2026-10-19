import json
from decimal import Decimal as D

import pytest

from live_engine.futures_store import FuturesStore
from models.trend_following_preny_profile_15m import pilot_live_smoke as smoke
from test_futures_execution import FakeVenue, NOW, policy, Crash


@pytest.fixture
def env(tmp_path,monkeypatch):
    clock=[NOW]
    monkeypatch.setattr('time.time',lambda:clock[0]/1000)
    config=policy();config.update(allocation_id='eth-execution-smoke',smoke_scenario='partial-time',smoke_direction='long')
    s=FuturesStore(tmp_path/'smoke.sqlite',config);s.acquire_writer()
    v=FakeVenue();e=smoke.SmokeExecutor(s,v,config)
    def sleep(seconds):clock[0]+=int(seconds*1000);v.clock=clock[0]
    yield s,v,e,clock,sleep
    s.close()


def test_resting_partial_then_actual_time_exit_and_no_second_entry(env):
    s,v,e,clock,sleep=env
    def fill_and_sleep(seconds):
        for cid,r in list(v.orders.items()):
            if r.get('type')=='LIMIT' and r['status']=='NEW':v.fill(cid,maker=True)
        sleep(seconds)
    result=smoke.run(e,clock=lambda:clock[0],sleep=fill_and_sleep)
    assert result['flat'] and result['ledger_reconciled'] and result['time_exit_observed']
    assert result['actual_maker_fill_count']==1 and result['actual_taker_fill_count']==2
    assert result['partial_fill_observed'] and not result['native_stop_fill_observed']
    assert [r for r,_ in v.posts]==['ENTRY','STOP','TP1','CLOSE']
    assert v.stop_at_close==[True]
    report=smoke.run(e,clock=lambda:clock[0],sleep=fill_and_sleep)
    assert report['flat'] and len(v.posts)==4
    plan=json.loads(s.db.execute('SELECT plan FROM sessions').fetchone()[0])
    assert D(plan['planned_price_risk'])<=D('.50') and D(plan['notional'])<=D('1000')
    assert D(result['net_PnL_USDC'])==v.wallet-D('200')
    assert s.db.execute("SELECT COUNT(*) FROM audit WHERE kind='smoke_authorized'").fetchone()[0]==1


def test_gtc_marketable_fallback_is_taker_only_if_actual_fill_says_so(env):
    s,v,e,clock,sleep=env
    submit=v.submit
    def marketable(role,p):
        result=submit(role,p)
        if role=='TP1' and D(p['price'])<1000:
            v.fill(p['newClientOrderId'],price='1000',maker=False)
            return dict(v.orders[p['newClientOrderId']])
        return result
    v.submit=marketable
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    assert result['time_exit_observed'] and result['partial_fill_observed']
    assert result['actual_maker_fill_count']==0 and result['actual_taker_fill_count']==3
    assert [r for r,_ in v.posts].count('TP1')==2
    assert len(v.cancels)>0 and result['flat']


def test_unfilled_maker_is_not_reported_as_maker_success(env):
    s,v,e,clock,sleep=env
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    assert result['flat'] and result['time_exit_observed']
    assert not result['partial_fill_observed'] and result['actual_maker_fill_count']==0


def test_stop_scenario_native_trigger_and_no_tp_post(env):
    s,v,e,clock,sleep=env;e.c['smoke_scenario']='stop'
    def stop_and_sleep(seconds):
        if v.position and any(r.get('algoStatus')=='NEW' for r in v.orders.values()):
            stop=next(r for r in v.orders.values() if r.get('algoStatus')=='NEW')
            v.stop_out(stop['triggerPrice'])
        sleep(seconds)
    result=smoke.run(e,clock=lambda:clock[0],sleep=stop_and_sleep)
    assert result['flat'] and result['native_stop_fill_observed'] and not result['time_exit_observed']
    assert [r for r,_ in v.posts]==['ENTRY','STOP']


def test_stop_not_hit_times_out_instead_of_claiming_success(env):
    s,v,e,clock,sleep=env;e.c['smoke_scenario']='stop'
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    assert result['flat'] and result['time_exit_observed'] and not result['native_stop_fill_observed']


def test_rejected_stop_emergency_close_and_no_tp(env):
    s,v,e,clock,sleep=env;v.fail['STOP']='rejected'
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    assert result['flat'] and result['ledger_reconciled']
    assert [r for r,_ in v.posts]==['ENTRY','STOP','CLOSE']


def test_restart_after_entry_http_keeps_id_and_original_deadline(env):
    s,v,e,clock,sleep=env;v.fail['ENTRY']='after_crash'
    with pytest.raises(Crash):smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    original=json.loads(s.active()['intention'])['force_exit_timestamp_ms']
    clock[0]+=120000;v.clock=clock[0]
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    assert result['flat'] and [r for r,_ in v.posts].count('ENTRY')==1
    assert json.loads(s.db.execute('SELECT intention FROM sessions').fetchone()[0])['force_exit_timestamp_ms']==original


def test_no_entry_when_account_nonflat_or_budget_cannot_fit(env):
    s,v,e,clock,sleep=env;v.position=D('.1')
    with pytest.raises(ValueError,match='not flat'):smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    assert not v.posts


def test_operator_interrupt_closes_with_protection_retained(env):
    s,v,e,clock,sleep=env
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep,abort=lambda:bool(v.posts))
    assert result['flat'] and v.stop_at_close==[True]
    assert [r for r,_ in v.posts].count('ENTRY')==1


def test_no_request_without_exact_ack(monkeypatch):
    monkeypatch.setattr('sys.argv',['smoke','--acknowledge-real-smoke','wrong','--scenario','stop','--direction','long'])
    monkeypatch.setattr(smoke,'require_worker_off',lambda *a:pytest.fail('must stop before IO'))
    with pytest.raises(SystemExit):smoke.main()


def test_funding_and_actual_fees_are_reconciled_without_subtracting_spread_twice(env):
    s,v,e,clock,sleep=env
    def fund_and_sleep(seconds):
        if not v.funding and v.position:
            v.funding=[dict(symbol='ETHUSDC',asset='USDC',incomeType='FUNDING_FEE',income='-.01',tranId=55,time=clock[0])]
            v.wallet-=D('.01')
        sleep(seconds)
    result=smoke.run(e,clock=lambda:clock[0],sleep=fund_and_sleep)
    assert result['actual_funding_USDC']=='-0.01'
    assert D(result['net_PnL_USDC'])==v.wallet-D('200')
    assert D(result['actual_fee_by_asset']['USDC'])>0


def test_private_diagnostics_do_not_log_signing_material(env,tmp_path):
    from test_futures_preflight import credentials,FakeHTTP
    s,v,e,clock,sleep=env
    key,api,pem=credentials(tmp_path)
    http=FakeHTTP(payload={'orderId':1})
    http.request=lambda method,url,**kw:http.get(url,**kw)
    client=smoke.TracedOrders(api_key_file=api,private_key_file=pem,session=http,
        execution_enabled=True,acknowledgement='I_UNDERSTAND_REAL_MONEY')
    client.store=s;client.offset=0
    client.last_quote=dict(symbol='ETHUSDC',bidPrice='999.99',askPrice='1000.01',time=clock[0])
    client.submit('ENTRY',dict(symbol='ETHUSDC',side='BUY',positionSide='BOTH',type='MARKET',quantity='.01',
        newClientOrderId='ggp-smoke-test',newOrderRespType='RESULT'))
    audit=''.join(r[0] for r in s.db.execute('SELECT payload FROM audit'))
    assert 'smoke' in audit and 'duration_ms' in audit
    assert client._key not in audit and http.calls[0][1]['data']['signature'] not in audit


def test_partial_fill_during_cancel_only_replaces_remaining_quantity(env):
    s,v,e,clock,sleep=env
    original=v.cancel
    raced=[False]
    def race(cid,**kw):
        row=v.orders.get(cid,{})
        if row.get('type')=='LIMIT' and row.get('status')=='NEW' and not raced[0]:
            v.fill(cid,quantity='.02',maker=True,terminal=False);raced[0]=True
        return original(cid,**kw)
    v.cancel=race
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    replacement=next(p for role,p in v.posts if p.get('newClientOrderId','').endswith('tp1taker'))
    assert D(replacement['quantity'])==D('.03')
    assert result['flat'] and result['ledger_reconciled']


def test_quote_cost_metrics_are_estimates_not_additional_fees(env):
    s,v,e,clock,sleep=env
    smoke.seed_once(e,clock[0]);e.cycle(clock[0])
    entry=s.db.execute("SELECT client_id FROM orders WHERE role='ENTRY'").fetchone()[0]
    s.audit('smoke_order_context',dict(client_id=entry,quote=dict(bidPrice='999.9',askPrice='1000.1',time=NOW)))
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep)
    metrics=next(f['quote_metrics'] for f in result['fills'] if f['role']=='ENTRY')
    assert D(metrics['spread_price'])==D('.2') and D(metrics['spread_bps'])==D('2')
    assert D(metrics['signed_shortfall_vs_mid_USDC'])==0
    assert D(metrics['estimated_half_spread_USDC'])==D('.05')
    assert D(result['net_PnL_USDC'])==v.wallet-D('200')


def test_interrupted_before_entry_produces_no_mutation(env):
    s,v,e,clock,sleep=env
    result=smoke.run(e,clock=lambda:clock[0],sleep=sleep,abort=lambda:True)
    assert result['flat'] and not v.posts


def test_smoke_does_not_weaken_production_intention_validation(env):
    from test_futures_execution import intent
    s,v,e,clock,sleep=env
    with pytest.raises(ValueError,match='identity'):
        e.offer(intent(),clock[0])
    assert not v.posts
