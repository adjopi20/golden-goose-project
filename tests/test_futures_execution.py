from copy import deepcopy
import base64
from datetime import date, timedelta
from decimal import Decimal as D
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

from live_engine.adapters.binance_futures_execution import FuturesOrders, VenueError, safe_venue_reason
from live_engine.futures_execution import FuturesExecutor, millis, order_id, plan_order
from live_engine.futures_store import FuturesStore
from models.trend_following_preny_profile_15m.pilot_worker import import_proposals, live_proposal, load_policy
from trading_core.contracts import canonical_json, content_hash, make_intention
from test_futures_preflight import FakeHTTP, credentials, specification

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT/'models/trend_following_preny_profile_15m/deploy/pilot.ethusdc.execution.json'
DAY = date(2026, 10, 19)
NOW = millis(DAY, 9, 30)


def policy(enabled=True):
    p = load_policy(POLICY)
    p.update(execution_enabled=enabled, accept_new_entries=enabled)
    return p


def intent(**changes):
    p = dict(venue='binance', product='futures', instrument_id='ETHUSDC', environment='server-live',
             account_id='tf-preny-eth-pilot-v1', allocation_id='tf-preny-eth-pilot-v1',
             model_id='trend_following_preny_profile_15m', model_version='trend_following_preny_profile_15m_v1',
             contract_version='TF-PRENY-ETH-001', config_hash=policy()['source_config_hash'],
             feature_version='phase1-frozen-v1', calibration_id='cal', decision_snapshot_ref='snapshot',
             session_id=str(DAY), direction='long', feature_as_of_ms=NOW, signal_timestamp_ms=NOW,
             eligible_timestamp_ms=NOW, expires_timestamp_ms=NOW+300000,
             force_exit_timestamp_ms=millis(DAY+timedelta(days=1), 8, 59), risk_fraction=.005,
             initial_stop=990., entry_reference=1000., exit_policy='initial_stop_or_time')
    p.update(changes)
    return make_intention(**p)


class Crash(BaseException):
    pass


class FakeVenue:
    def __init__(self, armed=True):
        self.armed, self._key, self.offset = armed, 'test-api-key', 0
        self.wallet = D('200')
        self.position = D(0)
        self.entry_price = D(0)
        self.orders, self.fills, self.funding = {}, [], []
        self.posts, self.cancels = [], []
        self.fail = {}
        self.next_id = 100
        self.clock = NOW
        self.spec = specification()
        self.initial_fill = None
        self.close_fraction = D(1)
        self.on_cancel = None
        self.hide_fills = False
        self.available = None
        self.stop_at_close = []
        self.query_failures = set()

    def load_credentials(self):
        pass

    def get(self, path, **kw):
        if path == '/fapi/v1/exchangeInfo': return {'symbols': [self.spec]}
        if path == '/fapi/v1/ticker/bookTicker': return dict(symbol='ETHUSDC', bidPrice='1000', askPrice='1000', time=self.clock)
        raise AssertionError(path)

    def read(self, path, **kw):
        if path == '/fapi/v1/accountConfig': return dict(canTrade=True, dualSidePosition=False, multiAssetsMargin=False)
        if path == '/fapi/v3/positionRisk':
            return [] if not self.position else [dict(symbol='ETHUSDC', positionSide='BOTH', positionAmt=str(self.position))]
        if path == '/fapi/v3/account': return dict(assets=[dict(asset='USDC', walletBalance=str(self.wallet), availableBalance=str(self.available if self.available is not None else self.wallet))])
        if path == '/fapi/v1/openOrders': return [deepcopy(r) for r in self.orders.values() if 'status' in r and r['status'] in ('NEW', 'PARTIALLY_FILLED')]
        if path == '/fapi/v1/openAlgoOrders': return [deepcopy(r) for r in self.orders.values() if r.get('algoStatus') in ('NEW', 'TRIGGERING', 'TRIGGERED')]
        if path == '/fapi/v1/symbolConfig': return [dict(symbol='ETHUSDC', leverage=5, marginType='CROSSED', maxNotionalValue='100000')]
        if path == '/fapi/v1/userTrades': return [] if self.hide_fills else deepcopy([r for r in self.fills if str(r['orderId']) == str(kw['orderId'])])
        if path == '/fapi/v1/income': return deepcopy([r for r in self.funding if kw['startTime'] <= r['time'] <= kw['endTime']])
        if path == '/fapi/v1/order': return deepcopy(next(r for r in self.orders.values() if str(r.get('orderId')) == str(kw['orderId'])))
        raise AssertionError(path)

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def fill(self, cid, quantity=None, price=None, maker=False, terminal=True):
        r = self.orders[cid]
        qty = D(quantity) if quantity is not None else D(r['origQty'])-D(r['executedQty'])
        price = D(price or (r['price'] if r['type'] == 'LIMIT' else '1000'))
        pnl = D(0)
        delta = qty if r['side'] == 'BUY' else -qty
        if self.position and self.position * delta < 0:
            qty = min(qty, abs(self.position)); delta = qty if delta > 0 else -qty
            pnl = (1 if self.position > 0 else -1) * (price-self.entry_price)*qty
        else:
            self.entry_price = price
        self.position += delta
        before = D(r['executedQty'])
        r['avgPrice'] = str((D(r['avgPrice'])*before+price*qty)/(before+qty))
        r['executedQty'] = str(before+qty)
        r['status'] = ('FILLED' if before+qty == D(r['origQty']) else 'EXPIRED') if terminal else 'PARTIALLY_FILLED'
        fee = D(0) if maker else price*qty*D('.0004')
        self.wallet += pnl-fee
        self.fills.append(dict(symbol='ETHUSDC', id=len(self.fills)+1, orderId=r['orderId'], side=r['side'],
                               qty=str(qty), price=str(price), realizedPnl=str(pnl), commission=str(fee),
                               commissionAsset='USDC', maker=maker, time=self.clock))

    def submit(self, role, p):
        assert self.armed
        self.posts.append((role, deepcopy(p)))
        failure = self.fail.pop(role, None)
        if failure == 'before_crash': raise Crash()
        if failure == 'rejected': raise VenueError(400, -2021, uncertain=False)
        cid = p.get('clientAlgoId', p.get('newClientOrderId'))
        assert cid not in self.orders, 'duplicate submission'
        r = dict(symbol=p['symbol'], side=p['side'], positionSide='BOTH')
        if role == 'STOP':
            r.update(clientAlgoId=cid, algoId=self._new_id(), orderType='STOP_MARKET', algoStatus='NEW',
                     workingType='CONTRACT_PRICE', closePosition=True, priceProtect=False, triggerPrice=p['triggerPrice'], actualOrderId='')
        else:
            r.update(clientOrderId=cid, orderId=self._new_id(), type=p['type'], origQty=p['quantity'], executedQty='0',
                     avgPrice='0', status='NEW', reduceOnly=p.get('reduceOnly') == 'true', price=p.get('price', '0'), timeInForce=p.get('timeInForce', 'GTC'))
        self.orders[cid] = r
        if role == 'ENTRY':
            self.fill(cid, self.initial_fill or p['quantity'], terminal=self.initial_fill is None)
        if role == 'CLOSE':
            self.stop_at_close.append(any(r.get('algoStatus') == 'NEW' for r in self.orders.values()))
            self.fill(cid, str(D(p['quantity'])*self.close_fraction))
        if failure == 'after_crash': raise Crash()
        if failure == 'timeout': raise VenueError(503, uncertain=True)
        if failure == 'malformed': return {'unexpected':'response'}
        return deepcopy(self.orders[cid])

    def query(self, cid, **kw):
        if cid in self.query_failures: raise VenueError(503)
        return deepcopy(self.orders.get(cid))

    def cancel(self, cid, **kw):
        self.cancels.append(cid)
        if self.on_cancel:
            callback, self.on_cancel = self.on_cancel, None
            callback(cid)
        if cid not in self.orders: raise VenueError(400, -2011)
        r = self.orders[cid]
        key = 'algoStatus' if 'algoStatus' in r else 'status'
        if r[key] not in ('FILLED', 'FINISHED', 'EXPIRED'): r[key] = 'CANCELED'
        return deepcopy(r)

    def stop_out(self, price='990'):
        stop = next(r for r in self.orders.values() if r.get('algoStatus') == 'NEW')
        cid = 'native-stop-child'
        child = dict(symbol='ETHUSDC', side=stop['side'], positionSide='BOTH', clientOrderId=cid,
                     orderId=self._new_id(), type='MARKET', origQty=str(abs(self.position)), executedQty='0',
                     avgPrice='0', status='NEW', reduceOnly=True)
        self.orders[cid] = child
        self.fill(cid, price=price)
        stop.update(algoStatus='FINISHED', actualOrderId=child['orderId'])


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr('live_engine.futures_execution.time.time', lambda: clock[0]/1000)
    cfg = policy()
    s = FuturesStore(tmp_path/'live.sqlite', cfg); s.acquire_writer()
    v = FakeVenue()
    e = FuturesExecutor(s, v, cfg)
    yield s, v, e, clock
    s.close()


def enter(env):
    s,v,e,clock = env
    assert e.offer(intent(), NOW) == 'PENDING'
    e.cycle(NOW)
    return s,v,e,clock


def test_real_lifecycle_partial_tp_stop_and_equity(env):
    s,v,e,clock = enter(env)
    assert [r for r,_ in v.posts] == ['ENTRY', 'STOP', 'TP1']
    assert v.position == D('.10')
    assert [x['state'] for x in s.db.execute('SELECT state FROM sessions')] == ['OPEN']
    tp = next(cid for cid,r in v.orders.items() if r.get('type') == 'LIMIT')
    v.fill(tp, maker=True)
    e.cycle(NOW+1000)
    assert v.position == D('.09')
    v.stop_out()
    e.cycle(NOW+2000)
    assert s.active() is None and v.position == 0
    assert s.equity('200') == v.wallet
    assert s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == 3
    e.cycle(NOW+3000)
    assert len(v.posts) == 3 and e.offer(intent(), NOW+4000) == 'duplicate'


def test_restart_never_reposts_entry(env):
    s,v,e,clock = env
    e.offer(intent(), NOW)
    v.fail['ENTRY'] = 'after_crash'
    with pytest.raises(Crash): e.cycle(NOW)
    assert s.db.execute("SELECT state FROM orders WHERE role='ENTRY'").fetchone()[0] == 'SENDING'
    cfg,path = e.c,s.path
    s.close()
    s2 = FuturesStore(path,cfg); s2.acquire_writer()
    try:
        e2 = FuturesExecutor(s2,v,cfg); e2.cycle(NOW+1000)
        assert [r for r,_ in v.posts].count('ENTRY') == 1
        assert s2.active()['state'] == 'OPEN'
        assert len([r for r in v.orders.values() if r.get('algoStatus') == 'NEW']) == 1
    finally:s2.close()
    # fixture owns the original handle; close is deliberately idempotent here.
    s.db = __import__('sqlite3').connect(':memory:')


def test_crash_before_http_remains_unknown_no_blind_retry(env):
    s,v,e,clock = env
    e.offer(intent(), NOW); v.fail['ENTRY']='before_crash'
    with pytest.raises(Crash): e.cycle(NOW)
    e.cycle(NOW+1000); e.cycle(NOW+2000)
    assert len(v.posts) == 1 and v.position == 0
    assert s.active()['state'] == 'ENTRY_PENDING'


def test_timeout_after_acceptance_queries_same_id(env):
    s,v,e,clock = env
    e.offer(intent(), NOW); v.fail['ENTRY']='timeout'
    e.cycle(NOW)
    assert [r for r,_ in v.posts].count('ENTRY') == 1
    assert s.active()['state'] == 'OPEN'


def test_time_exit_keeps_stop_until_flat_and_cancels_tp(env):
    s,v,e,clock = enter(env)
    clock[0] = intent()['force_exit_timestamp_ms']
    e.cycle(clock[0]); e.cycle(clock[0]+1)
    assert v.position == 0 and s.active() is None
    assert [r for r,_ in v.posts] == ['ENTRY','STOP','TP1','CLOSE']
    assert v.posts[-1][1]['reduceOnly'] == 'true'
    assert v.stop_at_close == [True]
    assert all(r.get('algoStatus') != 'NEW' for r in v.orders.values())


def test_tp_cancel_race_uses_remaining_venue_quantity(env):
    s,v,e,clock = enter(env)
    tp = next(cid for cid,r in v.orders.items() if r.get('type') == 'LIMIT')
    v.on_cancel = lambda cid: v.fill(tp, maker=True)
    clock[0] = intent()['force_exit_timestamp_ms']
    e.cycle(clock[0]); e.cycle(clock[0]+1)
    assert D(v.posts[-1][1]['quantity']) == D('.09')
    assert v.position == 0 and s.active() is None


def test_partial_entry_protected_before_final_fill(env):
    s,v,e,clock = env
    v.initial_fill = '.03'
    e.offer(intent(), NOW); e.cycle(NOW)
    assert [r for r,_ in v.posts] == ['ENTRY','STOP']
    entry = next(r for r in v.orders.values() if r.get('type') == 'MARKET')
    entry['status'] = 'EXPIRED'
    e.cycle(NOW+1000)
    assert D(v.posts[-1][1]['quantity']) == D('.003')


def test_funding_and_duplicate_records_not_double_counted(env):
    s,v,e,clock = enter(env)
    v.funding=[dict(symbol='ETHUSDC',asset='USDC',incomeType='FUNDING_FEE',income='-.01',tranId=1,time=NOW+500)]
    v.wallet -= D('.01')
    e.accounting(NOW+1000); e.accounting(NOW+2000)
    assert s.db.execute('SELECT COUNT(*) FROM funding').fetchone()[0] == 1
    assert s.equity('200') == v.wallet


def test_wallet_deposit_does_not_change_allocated_equity(env):
    s,v,e,clock = enter(env)
    old = s.equity('200'); v.wallet += 10
    with pytest.raises(ValueError,match='Wallet'):
        e.accounting(NOW+1000)
    assert s.equity('200') == old


def test_missing_fill_history_does_not_delay_stop_and_prevents_false_close(env):
    s,v,e,clock = env
    v.hide_fills = True
    e.offer(intent(),NOW); e.cycle(NOW)
    assert [r for r,_ in v.posts] == ['ENTRY','STOP','TP1']
    v.stop_out()
    with pytest.raises(ValueError,match='fills not complete'):
        e.cycle(NOW+1000)
    assert s.active() is not None
    v.hide_fills=False
    e.cycle(NOW+2000)
    assert s.active() is None


def test_disarmed_cannot_trade_or_fund_ledger(env):
    s,v,e,clock = env
    v.armed=False
    assert e.offer(intent(),NOW) == 'DISARMED'
    e.cycle(NOW)
    assert not v.posts and s.meta('initial_wallet') is None


def test_paper_intention_wrong_time_and_other_asset_rejected(env):
    s,v,e,clock = env
    for changed in (dict(environment='server-paper'), dict(instrument_id='BNBUSDC'),
                    dict(force_exit_timestamp_ms=millis(DAY+timedelta(days=1),9)), dict(risk_fraction=.01)):
        with pytest.raises(ValueError):e.offer(intent(**changed),NOW)
    assert not s.active() and not v.posts


def test_expiry_before_network_send_and_small_geometry(env):
    s,v,e,clock = env
    e.offer(intent(),NOW)
    clock[0] = NOW+300001
    e.cycle(clock[0])
    assert not v.posts
    with pytest.raises(ValueError,match='budget'):
        plan_order(intent(initial_stop=100.),v.spec,v.get('/fapi/v1/ticker/bookTicker'),equity='200',available='200',leverage=5,max_notional='100000')


def test_source_health_loss_skips_pending_not_open_exit(env):
    s,v,e,clock = env
    e.offer(intent(),NOW); e.cycle(NOW,new_entries_ok=False)
    assert not v.posts and s.active() is None


def test_key_rotation_fails_before_any_new_mutation(env):
    s,v,e,clock = enter(env)
    count=len(v.posts);v._key='another-key'
    with pytest.raises(ValueError,match='API key changed'):e.cycle(NOW+1000)
    assert len(v.posts)==count


def test_store_lock_identity_and_consistent_backup(env,tmp_path):
    s,v,e,clock = env
    other = FuturesStore(s.path,e.c)
    try:
        with pytest.raises(OSError):other.acquire_writer()
    finally:other.close()
    with pytest.raises(ValueError,match='identity'):
        FuturesStore(s.path,dict(e.c,initial_equity='1000'))
    s.backup(tmp_path/'backup.sqlite')
    with pytest.raises(ValueError):s.backup(tmp_path/'backup.sqlite')


def test_adapter_disarmed_and_exact_templates(tmp_path):
    _,api,pem=credentials(tmp_path)
    http=FakeHTTP()
    client=FuturesOrders(api_key_file=api,private_key_file=pem,session=http)
    client.offset=0
    params=dict(symbol='ETHUSDC',side='BUY',positionSide='BOTH',type='MARKET',quantity='.01',
                newClientOrderId='ggp-test-entry',newOrderRespType='RESULT')
    with pytest.raises(ValueError,match='OFF'):client.submit('ENTRY',params)
    with pytest.raises(ValueError):client.submit('ENTRY',dict(params,reduceOnly='false'))
    assert not http.calls


def test_stop_rejection_emergency_flattens_without_new_entry(env):
    s,v,e,clock=env
    v.fail['STOP']='rejected'
    e.offer(intent(),NOW);e.cycle(NOW);e.cycle(NOW+1000)
    assert [r for r,_ in v.posts]==['ENTRY','STOP','CLOSE']
    assert v.position==0 and s.active() is None


def test_unknown_stop_response_flattens_and_cleans_up_original_stop(env):
    s,v,e,clock=env
    v.fail['STOP']='timeout'
    e.offer(intent(),NOW);e.cycle(NOW);e.cycle(NOW+1000)
    assert [r for r,_ in v.posts].count('STOP')==1
    assert v.position==0 and s.active() is None
    assert all(r.get('algoStatus')!='NEW' for r in v.orders.values())


def source_db(path, now=NOW):
    import sqlite3
    db=sqlite3.connect(path)
    db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE proposals(intention_id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE evaluations(symbol TEXT,asof INTEGER,payload TEXT);')
    cfg=policy()
    meta=dict(owner='preny_strategy_shadow',schema='3',environment='server-paper',venue='binance',
              config_hash=cfg['source_config_hash'],code_version=cfg['source_code_version'],calibration_hash='cal',source_epoch='epoch',last_poll_ms=str(now))
    db.executemany('INSERT INTO metadata VALUES (?,?)',meta.items());db.commit()
    return db,meta


def source_proposal(day=DAY, direction='long'):
    t=millis(day,9,30)
    snapshot=dict(feature_as_of_ms=t,profile=dict(poc=990),risk_fraction=.005,
                  trend_15m=dict(status='ready',end_ms=t,price_vs_ema200_pct=.6))
    p=intent(environment='server-paper',account_id='tf-preny-eth-v1',allocation_id='tf-preny-eth-v1',
             session_id=str(day),feature_as_of_ms=t,signal_timestamp_ms=t,eligible_timestamp_ms=t,
             expires_timestamp_ms=t+300000,force_exit_timestamp_ms=millis(day+timedelta(days=1),8,59),
             decision_snapshot_ref=content_hash(snapshot),direction=direction)
    return p,dict(state='ENTRY_READY',selected=True,snapshot=snapshot)


def test_bridge_bootstrap_never_backfills_then_imports_new_eth_only(env,tmp_path):
    s,v,e,clock=env;v.armed=False
    path=tmp_path/'strategy.sqlite';db,meta=source_db(path)
    old,ev=source_proposal()
    db.execute('INSERT INTO proposals VALUES (?,?)',(old['intention_id'],canonical_json(old)))
    db.commit()
    assert import_proposals(path,e,NOW)==0
    day=DAY+timedelta(days=1);t=millis(day,9,30);new,ev=source_proposal(day)
    db.execute("UPDATE metadata SET value=? WHERE key='last_poll_ms'",(str(t),))
    db.execute('INSERT INTO proposals VALUES (?,?)',(new['intention_id'],canonical_json(new)))
    db.execute('INSERT INTO evaluations VALUES (?,?,?)',('ETHUSDC',t,canonical_json(ev)));db.commit()
    assert import_proposals(path,e,t)==1
    assert import_proposals(path,e,t)==0
    assert s.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==1
    assert s.db.execute('SELECT state FROM sessions').fetchone()[0]=='DISARMED'
    assert not v.posts
    db.close()


def test_bridge_rejects_changed_source_and_missing_trend(env,tmp_path):
    s,v,e,clock=env
    path=tmp_path/'strategy.sqlite';db,meta=source_db(path)
    p,ev=source_proposal()
    live=live_proposal(p,ev,policy(),meta)
    assert live['environment']=='server-live' and live['allocation_id']=='tf-preny-eth-pilot-v1'
    ev['snapshot']['trend_15m']['status']='missing'
    p['decision_snapshot_ref']=content_hash(ev['snapshot'])
    with pytest.raises(ValueError,match='EMA200'):live_proposal(p,ev,policy(),meta)
    db.execute("UPDATE metadata SET value='replacement' WHERE key='code_version'");db.commit()
    with pytest.raises(ValueError,match='identity'):import_proposals(path,e,NOW)
    assert not v.posts
    db.close()


@pytest.mark.parametrize('role',['ENTRY','CLOSE'])
def test_prepared_order_recovers_before_http(env,monkeypatch,role):
    s,v,e,clock=env
    if role=='CLOSE':
        enter(env);clock[0]=intent()['force_exit_timestamp_ms']
    else:e.offer(intent(),NOW)
    drive=e.drive
    def interrupted(cid):
        row=s.db.execute('SELECT role,state FROM orders WHERE client_id=?',(cid,)).fetchone()
        if row['role']==role and row['state']=='PREPARED':raise Crash()
        return drive(cid)
    monkeypatch.setattr(e,'drive',interrupted)
    with pytest.raises(Crash):e.cycle(clock[0])
    assert s.db.execute('SELECT state FROM orders WHERE role=?',(role,)).fetchone()[0]=='PREPARED'
    monkeypatch.setattr(e,'drive',drive)
    e.cycle(clock[0]);e.cycle(clock[0]+1)
    assert [r for r,_ in v.posts].count(role)==1
    if role=='CLOSE':assert v.position==0
    else:assert s.active()['state']=='OPEN'


def test_malformed_entry_response_still_protects_using_id_query(env):
    s,v,e,clock=env
    v.fail['ENTRY']='malformed'
    e.offer(intent(),NOW);e.cycle(NOW)
    assert [r for r,_ in v.posts]==['ENTRY','STOP','TP1']


def test_tp_status_endpoint_failure_does_not_block_time_exit(env):
    s,v,e,clock=enter(env)
    tp=next(cid for cid,r in v.orders.items() if r.get('type')=='LIMIT')
    v.query_failures.add(tp)
    clock[0]=intent()['force_exit_timestamp_ms']
    e.cycle(clock[0])
    assert v.position==0
    e.cycle(clock[0]+1)
    assert s.active() is not None  # Do not re-enter while cancellation remains unverified.
    v.query_failures.clear();e.cycle(clock[0]+2)
    assert s.active() is None


def test_partial_close_reconciles_residual_without_reentry(env):
    s,v,e,clock=enter(env)
    v.close_fraction=D('.5');clock[0]=intent()['force_exit_timestamp_ms']
    e.cycle(clock[0]);assert v.position==D('.05')
    v.close_fraction=D(1)
    e.cycle(clock[0]+1);e.cycle(clock[0]+2)
    assert v.position==0 and s.active() is None
    assert [r for r,_ in v.posts].count('ENTRY')==1
    assert [r for r,_ in v.posts].count('CLOSE')==2


def test_too_small_terminal_partial_is_flattened_not_silently_full_runner(env):
    s,v,e,clock=env;v.initial_fill='.005'
    e.offer(intent(),NOW);e.cycle(NOW)
    next(r for r in v.orders.values() if r.get('type')=='MARKET')['status']='EXPIRED'
    e.cycle(NOW+1);e.cycle(NOW+2)
    assert v.position==0 and 'TP1' not in [r for r,_ in v.posts]
    assert s.active() is None


def test_open_time_exit_not_dependent_on_source_health(env):
    s,v,e,clock=enter(env)
    clock[0]=intent()['force_exit_timestamp_ms']
    e.cycle(clock[0],new_entries_ok=False);e.cycle(clock[0]+1,new_entries_ok=False)
    assert v.position==0 and s.active() is None


def test_adapter_signed_post_is_gated_and_never_retried(tmp_path):
    key,api,pem=credentials(tmp_path)
    payload=dict(symbol='ETHUSDC',side='BUY',positionSide='BOTH',type='MARKET',quantity='.01',
                 newClientOrderId='ggp-test-entry',newOrderRespType='RESULT')
    http=FakeHTTP(payload={'orderId':1})
    calls=[]
    def request(method,url,**kw):
        calls.append(method)
        return http.get(url,**kw)
    http.request=request
    client=FuturesOrders(api_key='A'*64,private_key_file=pem,session=http,
                         execution_enabled=True,acknowledgement='I_UNDERSTAND_REAL_MONEY')
    client.offset=0
    client.submit('ENTRY',payload)
    assert calls==['POST'] and len(http.calls)==1
    _,kw=http.calls[0];data=kw['data'].copy();signature=base64.b64decode(data.pop('signature'))
    key.public_key().verify(signature,urlencode(data).encode('ascii'))
    assert kw['allow_redirects'] is False and kw['params'] is None
    http.status=503
    with pytest.raises(VenueError) as exc:client.submit('ENTRY',payload)
    assert exc.value.uncertain and len(calls)==2
    for bad in ('0','-1','NaN'):
        with pytest.raises(ValueError):client.submit('ENTRY',dict(payload,quantity=bad))
    assert len(calls)==2
    with pytest.raises(ValueError):client.cancel('manual-order',symbol='ETHUSDC')


@pytest.mark.parametrize('message', [None, {'msg':'bad'}, 'x'*1025,
    'https://fapi.binance.com/fapi/v1/order?signature=private',
    'X-MBX-APIKEY: private', '-----BEGIN PRIVATE KEY-----', 'x'*64])
def test_venue_reason_omits_sensitive_or_malformed_content(message):
    reason=safe_venue_reason(message)
    assert reason in (None,'[sensitive venue message redacted]')


def test_adapter_keeps_safe_reason_without_changing_uncertainty_or_retry(tmp_path):
    key,api,pem=credentials(tmp_path)
    http=FakeHTTP(status=400,payload={'code':-4509,'msg':'GTE can only be used with open positions'})
    http.request=lambda method,url,**kw:http.get(url,**kw)
    client=FuturesOrders(api_key_file=api,private_key_file=pem,session=http,
                        execution_enabled=True,acknowledgement='I_UNDERSTAND_REAL_MONEY')
    client.offset=0
    with pytest.raises(VenueError) as exc:client._request('POST','/fapi/v1/algoOrder',{})
    assert exc.value.reason=='GTE can only be used with open positions'
    assert exc.value.code==-4509 and exc.value.uncertain and len(http.calls)==1
    assert exc.value.reason in str(exc.value)
    http.payload['msg']='Rejected '+client._key
    with pytest.raises(VenueError) as exc:client.read('/fapi/v1/openOrders')
    assert exc.value.reason=='Rejected [redacted]' and client._key not in str(exc.value)
    signature=http.calls[-1][1]['params']['signature']
    http.payload['msg']='Rejected '+signature
    with pytest.raises(VenueError) as exc:client.read('/fapi/v1/openOrders')
    assert signature not in str(exc.value) and 'redacted' in str(exc.value)


def test_safe_venue_reason_normalizes_controls_and_limits_length():
    assert safe_venue_reason('Order\nwould\x1b immediately trigger.')=='Order would immediately trigger.'
    assert len(safe_venue_reason('word '*100))==300
    assert safe_venue_reason('Rejected token',('token',))=='Rejected [redacted]'


def test_rejection_reason_is_preserved_in_execution_audit(env):
    s,v,e,clock=env
    def reject(role,params):
        raise VenueError(400,-2021,reason='Order would immediately trigger.')
    v.submit=reject
    e.offer(intent(),NOW);e.cycle(NOW)
    event=json.loads(s.db.execute("SELECT payload FROM audit WHERE kind='order_submit_error'").fetchone()[0])
    assert event['venue_reason']=='Order would immediately trigger.'
    assert event['uncertain'] is False
    assert 'immediately trigger' in s.db.execute('SELECT error FROM orders').fetchone()[0]


def test_default_release_has_no_credentials_and_two_off_switches():
    cfg=load_policy(POLICY)
    assert cfg['execution_enabled'] is False and cfg['accept_new_entries'] is False
    compose=(ROOT/'models/trend_following_preny_profile_15m/deploy/pilot.executor.yaml').read_text()
    assert 'BINANCE_API_KEY' not in compose and 'BINANCE_PRIVATE_KEY_HOST_PATH' not in compose
    assert '--acknowledge-real-money' not in compose
    assert compose.count('/data/execution-200.sqlite')==2  # Worker and healthcheck must inspect the same new journal.


def test_cli_validate_and_online_backup_do_not_create_orders(env,tmp_path):
    s,v,e,clock=env
    base=[sys.executable,'-B','-m','models.trend_following_preny_profile_15m.pilot_worker','--config',str(POLICY)]
    r=subprocess.run(base+['--action','validate'],cwd=ROOT,text=True,capture_output=True,check=True,timeout=15)
    assert json.loads(r.stdout)['orders_submitted']==0
    dest=tmp_path/'online.sqlite'
    subprocess.run(base+['--database',str(s.path),'--action','backup','--backup-to',str(dest)],cwd=ROOT,check=True,capture_output=True,timeout=15)
    assert dest.exists() and not v.posts


def test_short_lifecycle_uses_buy_exits_and_lower_tp(env):
    s,v,e,clock=env
    e.offer(intent(direction='short',initial_stop=1010.),NOW);e.cycle(NOW)
    assert v.posts[0][1]['side']=='SELL'
    assert v.posts[1][1]['side']=='BUY' and v.posts[1][1]['triggerPrice']=='1010.0'
    assert D(v.posts[2][1]['price'])==990 and v.posts[2][1]['side']=='BUY'
    tp=next(cid for cid,r in v.orders.items() if r.get('type')=='LIMIT')
    v.fill(tp,maker=True);v.stop_out('1010');e.cycle(NOW+1000)
    assert v.position==0 and s.active() is None
    assert s.equity('200')==v.wallet


def test_dynamic_half_risk_and_rounding_never_exceed_budget(env):
    s,v,e,clock=env
    e.offer(intent(risk_fraction=.0025,initial_stop=989.995),NOW);e.cycle(NOW)
    plan=json.loads(s.active()['plan'])
    assert D(plan['stop'])==D('989.99')
    assert D(plan['actual_initial_price_risk'])<=D('.5')
    assert D(plan['tp1_quantity'])==D(plan['quantity'])/10


def test_taker_tp_commission_uses_fill_not_zero_maker_assumption(env):
    s,v,e,clock=enter(env)
    tp=next(cid for cid,r in v.orders.items() if r.get('type')=='LIMIT')
    v.fill(tp,maker=False);v.stop_out();e.cycle(NOW+1000)
    assert s.equity('200')==v.wallet
    assert all(D(r[0])>0 for r in s.db.execute('SELECT commission FROM fills'))


def test_rejected_entry_never_fabricates_a_trade(env):
    s,v,e,clock=env
    v.fail['ENTRY']='rejected'
    e.offer(intent(),NOW);e.cycle(NOW)
    assert v.position==0 and s.active() is None
    assert s.db.execute('SELECT state FROM sessions').fetchone()[0]=='SKIPPED'
    assert s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==0


def test_cancel_prepared_never_submits_http(env):
    s,v,e,clock=env
    e.offer(intent(),NOW)
    session=s.active()
    cid=e._market(session,'ENTRY',D('.01'))
    row=s.db.execute('SELECT * FROM orders WHERE client_id=?',(cid,)).fetchone()
    assert e.cancel(row)
    assert not v.posts and not v.cancels
    assert s.db.execute('SELECT state FROM orders WHERE client_id=?',(cid,)).fetchone()[0]=='CANCELED'


def test_missing_native_stop_is_unknown_and_emergency_flattens(env):
    s,v,e,clock=enter(env)
    stop=next(cid for cid,r in v.orders.items() if 'algoStatus' in r)
    del v.orders[stop]
    e.cycle(NOW+1000)
    assert v.position==0
    assert [r for r,_ in v.posts].count('STOP')==1
    assert s.active() is not None  # Unknown protection is not silently finalized.


def test_unknown_unfilled_entry_is_cancelled_after_deadline_not_reposted(env):
    s,v,e,clock=env
    e.offer(intent(),NOW);v.fail['ENTRY']='before_crash'
    with pytest.raises(Crash):e.cycle(NOW)
    e.cycle(NOW+300001)
    assert v.cancels and len(v.posts)==1 and v.position==0


def test_200_allocation_budget_is_one_or_half_usdc_and_never_forces_minimum(env):
    s,v,e,clock=env
    assert e.c['initial_equity']=='200'
    for tier,budget in ((.005,'1'),(.0025,'.5')):
        p=plan_order(intent(risk_fraction=tier),v.spec,v.get('/fapi/v1/ticker/bookTicker'),
                     equity='200',available='202',leverage=5,max_notional='100000')
        assert D(p['risk_budget'])==D(budget)
        assert D(p['planned_price_risk'])<=D(budget)
    v.available=D('199.99')
    e.offer(intent(),NOW)
    with pytest.raises(ValueError,match='Insufficient USDC'):e.cycle(NOW)
    assert not v.posts


def test_existing_145_journal_cannot_be_silently_reallocated(tmp_path):
    old=FuturesStore(tmp_path/'previous.sqlite',dict(policy(),initial_equity='145'))
    old.close()
    with pytest.raises(ValueError,match='identity'):
        FuturesStore(tmp_path/'previous.sqlite',policy())


def test_diagnostics_capture_timing_quote_and_fills_without_poll_duplication(env,monkeypatch):
    ticks=iter(range(0,1000000000,1000000))
    monkeypatch.setattr('live_engine.futures_execution.time.perf_counter_ns',lambda:next(ticks))
    s,v,e,clock=enter(env)
    events=[(r['kind'],json.loads(r['payload'])) for r in s.db.execute('SELECT * FROM audit ORDER BY id')]
    received=next(p for k,p in events if k=='intention_received')
    assert received['signal_timestamp_ms']==NOW and received['local_recorded_at_ms']==NOW
    entry=next(p for k,p in events if k=='order_observed' and p['role']=='ENTRY')
    assert entry['observation_source']=='submit' and entry['request_duration_ms']==1.0
    assert entry['server_offset_ms']==0
    assert any(k=='order_send_started' and p['role']=='STOP' for k,p in events)
    fill=next(p for k,p in events if k=='fill_first_observed')
    assert fill['native_fill_time_ms']==NOW
    plan=json.loads(s.active()['plan'])
    assert plan['entry_quote']['ask']=='1000' and plan['entry_quote']['native_time_ms']==NOW
    before=len(events)
    e.cycle(NOW+1000)
    assert s.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]==before
    assert v._key not in ''.join(r[0] for r in s.db.execute('SELECT payload FROM audit'))


def test_submit_error_has_duration_not_raw_request_or_credentials(env):
    s,v,e,clock=env;v.fail['ENTRY']='rejected'
    e.offer(intent(),NOW);e.cycle(NOW)
    event=json.loads(s.db.execute("SELECT payload FROM audit WHERE kind='order_submit_error'").fetchone()[0])
    assert event['http_status']==400 and event['venue_code']==-2021
    assert event['request_duration_ms']>=0 and event['uncertain'] is False
    assert v._key not in canonical_json(event)
