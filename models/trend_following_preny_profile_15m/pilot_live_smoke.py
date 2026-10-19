"""Operator-run real-money ETH execution diagnostics, never a strategy signal."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import time

from live_engine.adapters.binance_futures_execution import ACKNOWLEDGEMENT, FuturesOrders, VenueError
from live_engine.adapters.binance_futures_readonly import PreflightError
from live_engine.futures_execution import FuturesExecutor, order_id, rounded
from live_engine.futures_preflight import number, positive
from live_engine.futures_store import FuturesStore
from models.trend_following_preny_profile_15m.pilot_stop_probe import Journal, require_worker_off
from models.trend_following_preny_profile_15m.pilot_worker import load_policy
from trading_core.contracts import canonical_json, make_intention

ACK = 'I_AUTHORIZE_REAL_ETH_SMOKE_200_USDC'


class TracedOrders(FuturesOrders):
    store = None
    last_quote = None

    def _request(self, method, path, params):
        started = time.perf_counter_ns()
        error = None
        try:
            return super()._request(method, path, params)
        except VenueError as e:
            error = dict(http_status=e.status, venue_code=e.code, venue_reason=e.reason, uncertain=e.uncertain)
            raise
        finally:
            if self.store:
                self.store.audit('smoke_api_request', dict(method=method, endpoint=path, error=error,
                    duration_ms=(time.perf_counter_ns()-started)/1000000))

    def get(self, path, **params):
        result = super().get(path, **params)
        if path == '/fapi/v1/ticker/bookTicker' and self.store:
            self.last_quote = dict(result)
            self.store.audit('smoke_quote', dict(quote=result, server_offset_ms=self.offset))
        return result

    def submit(self, role, params):
        if self.store:
            self.store.audit('smoke_order_context', dict(role=role,
                client_id=params.get('clientAlgoId', params.get('newClientOrderId')),
                quote=self.last_quote, server_offset_ms=self.offset))
        return super().submit(role, params)


class SmokeExecutor(FuturesExecutor):
    """Only diagnostic TP placement changes; protection/recovery use the shared engine."""
    def _prepare(self, session, role, params):
        if role == 'TP1' and params['newClientOrderId'].endswith('-tp1'):
            cid = params['newClientOrderId']
            old = self.s.db.execute('SELECT params FROM orders WHERE client_id=?', (cid,)).fetchone()
            if old:
                params = json.loads(old[0])
            elif self.c['smoke_scenario'] == 'partial-time':
                params = dict(params)
                quote = self.quote(int(time.time()*1000))
                tick = positive(json.loads(self.s.active()['plan'])['tick_size'])
                # Ordinary GTC, matching production. A resting quote is not a maker guarantee.
                params['price'] = str(positive(quote['askPrice'])+tick if params['side']=='SELL'
                                      else max(tick, positive(quote['bidPrice'])-tick))
                with self.s.transaction():
                    self.s.set_meta('smoke_maker_until_ms', int(time.time()*1000)+15000)
        return super()._prepare(session, role, params)

    def drive(self, cid):
        row = self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (cid,)).fetchone()
        if row and row['role']=='TP1' and row['state']=='PREPARED' and self.c['smoke_scenario']=='stop':
            self.cancel(row)  # No TP on the stop-only scenario; never send this prepared order.
            return self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (cid,)).fetchone()
        return super().drive(cid)


def seed_once(e, now_ms):
    s = e.s
    if s.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]:
        return False  # A repeated command only reconciles this one saved entry.
    if s.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:
        raise ValueError('Orders without a diagnostic session; operator review required')
    e.entry_guard(now_ms)
    q = e.quote(now_ms)
    reference = positive(q['askPrice'] if e.c['smoke_direction']=='long' else q['bidPrice'])
    distance = reference * Decimal('0.001' if e.c['smoke_scenario']=='partial-time' else '0.0002')
    sign = 1 if e.c['smoke_direction']=='long' else -1
    now_day = datetime.fromtimestamp(now_ms/1000, timezone.utc).date().isoformat()
    p = make_intention(venue='binance', product='futures', instrument_id='ETHUSDC', environment='server-live',
        account_id='eth-execution-smoke', allocation_id='eth-execution-smoke', model_id='execution_diagnostic',
        model_version='v1', contract_version='NOT_A_MODEL_TRADE', config_hash=s.meta('config_hash'),
        feature_version='manual-diagnostic', calibration_id='not-applicable', decision_snapshot_ref='manual-diagnostic',
        session_id=now_day, direction=e.c['smoke_direction'], feature_as_of_ms=now_ms,
        signal_timestamp_ms=now_ms, eligible_timestamp_ms=now_ms, expires_timestamp_ms=now_ms+30000,
        force_exit_timestamp_ms=now_ms+90000, risk_fraction=.0025,
        initial_stop=float(reference-sign*distance), entry_reference=float(reference), exit_policy='diagnostic_90_second_deadline')
    with s.transaction():
        s.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
            (now_day,p['intention_id'],canonical_json(p),None,'PENDING','operator_execution_test'))
        s.set_meta('smoke_started_ms',now_ms)
        s.audit('smoke_authorized',dict(scenario=e.c['smoke_scenario'],direction=p['direction'],
            allocation='200',risk_fraction='.0025',price_risk_budget='0.50',time_exit_ms=p['force_exit_timestamp_ms'],
            stop_distance_bps='10' if e.c['smoke_scenario']=='partial-time' else '2',
            strategy_entry_rules_bypassed=True,normal_model_unchanged=True))
    return True


def marketable_partial(e, now_ms):
    s=e.s
    if e.c['smoke_scenario']!='partial-time' or now_ms<int(s.meta('smoke_maker_until_ms',now_ms+1)):
        return
    session=s.active()
    if not session or session['state']!='OPEN':return
    plan=json.loads(session['plan'])
    old=s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='TP1' ORDER BY rowid LIMIT 1",(session['session'],)).fetchone()
    if not old or not e.cancel(old):return  # Cancellation must reconcile before replacing any remainder.
    old=s.db.execute('SELECT * FROM orders WHERE client_id=?',(old['client_id'],)).fetchone()
    executed=number(json.loads(old['response'])['executedQty']) if old['response'] else Decimal(0)
    amount,other=e.position()
    if other:raise ValueError('Other instrument position appeared; stop diagnostics')
    qty=rounded(min(max(Decimal(0),positive(plan['actual_entry_quantity'])/10-executed),abs(amount)),positive(plan['quantity_step']))
    if not qty or qty<positive(plan['min_qty']):return
    cid=order_id(session['id'],'tp1taker')
    old_replacement=s.db.execute('SELECT client_id FROM orders WHERE client_id=?',(cid,)).fetchone()
    if old_replacement:return  # Never re-price/re-POST a previously attempted replacement.
    q=e.quote(now_ms);tick=positive(plan['tick_size']);sell=plan['direction']=='long'
    price=max(tick,positive(q['bidPrice'])-2*tick) if sell else positive(q['askPrice'])+2*tick
    params=dict(symbol='ETHUSDC',side='SELL' if sell else 'BUY',positionSide='BOTH',type='LIMIT',
        quantity=str(qty),price=str(price),timeInForce='GTC',reduceOnly='true',newClientOrderId=cid)
    with s.transaction():s.audit('smoke_marketable_partial',dict(remaining_quantity=str(qty),quote=q,
        note='Taker intent only; actual maker flag comes from Binance fills'))
    e.drive(e._prepare(session['session'],'TP1',params))


def summarize(e):
    s=e.s;amount,other=e.position()
    regular=e.v.read('/fapi/v1/openOrders');algos=e.v.read('/fapi/v1/openAlgoOrders')
    wallet,_=e.wallet()
    contexts={r['client_id']:r for r in (json.loads(x[0]) for x in s.db.execute("SELECT payload FROM audit WHERE kind='smoke_order_context'"))}
    fills=[]
    for row in s.db.execute('SELECT * FROM fills'):
        f=json.loads(row['payload'])
        order=next((r for r in s.db.execute('SELECT * FROM orders') if r['response'] and str(json.loads(r['response']).get('actualOrderId') if r['role']=='STOP' else json.loads(r['response']).get('orderId'))==row['order_id']),None)
        context=contexts.get(order['client_id'],{}) if order else {}
        quote=context.get('quote');metrics={}
        if quote:
            bid,ask=positive(quote['bidPrice']),positive(quote['askPrice']);mid=(bid+ask)/2
            qty,price=positive(f['qty']),positive(f['price']);sign=1 if f['side']=='BUY' else -1
            executable=ask if sign==1 else bid
            metrics=dict(reference_quote_ms=quote.get('time'),spread_price=str(ask-bid),
                fill_vs_reference_quote_ms=f.get('time',0)-quote.get('time',0),
                spread_bps=str((ask-bid)/mid*10000),estimated_half_spread_USDC=str(qty*(ask-bid)/2),
                signed_shortfall_vs_mid_USDC=str(qty*sign*(price-mid)),
                signed_slippage_vs_executable_USDC=str(qty*sign*(price-executable)))
        fills.append(dict(role=order['role'] if order else 'unattributed',fill=f,quote_metrics=metrics))
    fee_by_asset={}
    for r in s.db.execute('SELECT commission_asset,commission FROM fills'):
        fee_by_asset[r[0]]=str(Decimal(fee_by_asset.get(r[0],'0'))+number(r[1]))
    realized=sum((number(r[0],signed=True) for r in s.db.execute('SELECT realized FROM fills')),Decimal(0))
    funding=sum((number(r[0],signed=True) for r in s.db.execute('SELECT amount FROM funding')),Decimal(0))
    stop_filled=any(f['role']=='STOP' for f in fills)
    stop_ack=s.db.execute("SELECT COUNT(*) FROM orders WHERE role='STOP' AND response IS NOT NULL").fetchone()[0]>0
    time_closed=s.db.execute("SELECT COUNT(*) FROM sessions WHERE reason='time_exit' AND state='CLOSED'").fetchone()[0]>0
    return dict(real_money=True,diagnostic_only=True,normal_model_armed=False,scenario=e.c['smoke_scenario'],
        flat=amount==0 and not other,open_regular_orders=len(regular),open_algo_orders=len(algos),
        ledger_reconciled=s.active() is None,wallet_USDC=str(wallet),realized_PnL_USDC=str(realized),
        actual_fee_by_asset=fee_by_asset,actual_funding_USDC=str(funding),
        net_PnL_USDC=str(realized+funding-Decimal(fee_by_asset.get('USDC','0'))),
        actual_maker_fill_count=sum(f['fill']['maker'] for f in fills),
        actual_taker_fill_count=sum(not f['fill']['maker'] for f in fills),
        partial_fill_observed=any(f['role']=='TP1' for f in fills),native_stop_fill_observed=stop_filled,
        native_stop_ack_observed=stop_ack,time_exit_observed=time_closed,fills=fills,
        orders=s.status()['orders'],
        spread_cost_note='Quote-based estimates, not extra fees; already reflected in fill PnL. Do not subtract twice.',
        database=str(s.path))


def run(e, *, clock=lambda:int(time.time()*1000), sleep=time.sleep, abort=lambda:False):
    seed_once(e,clock())
    deadline=clock()+150000  # Recovery gets time to close; the original trade deadline never changes.
    while e.s.active() and clock()<deadline:
        try:
            if abort():
                active=e.s.active()
                if active:
                    with e.s.transaction():e.s.session_state(active['session'],
                        'EXIT_PENDING' if active['plan'] else 'SKIPPED','operator_interrupt')
            e.cycle(clock(),new_entries_ok=not abort())
            if e.s.active() and not abort():marketable_partial(e,clock())
            if e.s.active():e.quote(clock())
        except (VenueError,PreflightError,ValueError,KeyError,TypeError) as error:
            e.s.audit('smoke_cycle_error',dict(error=str(error) if isinstance(error,VenueError) else type(error).__name__))
            # On diagnostic errors never keep entering/waiting for profit; shared emergency exit retains STOP.
            active=e.s.active()
            if active:
                with e.s.transaction():e.s.session_state(active['session'],
                    'EXIT_PENDING' if active['plan'] else 'SKIPPED','diagnostic_error')
        sleep(1)
    result=summarize(e)
    with e.s.transaction():e.s.audit('smoke_final_report',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--acknowledge-real-smoke',required=True)
    p.add_argument('--scenario',choices=['partial-time','stop'],required=True)
    p.add_argument('--direction',choices=['long','short'],required=True)
    args=p.parse_args()
    if args.acknowledge_real_smoke!=ACK:p.error('Explicit real-money diagnostic acknowledgement required')
    require_worker_off('/app/config/pilot.json','/data/execution-200.sqlite')
    config=load_policy('/app/config/pilot.json')
    config.update(allocation_id='eth-execution-smoke',execution_enabled=True,accept_new_entries=True,
                  smoke_scenario=args.scenario,smoke_direction=args.direction)
    lock=Journal('/data/eth-live-smoke-account');lock.acquire_writer()
    s=None;v=None
    interrupted=[False]
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:interrupted.__setitem__(0,True))
    try:
        path=Path('/data')/('eth-live-smoke-'+args.scenario+'-'+args.direction+'-v1.sqlite')
        s=FuturesStore(path,config);s.acquire_writer()
        v=TracedOrders(execution_enabled=True,acknowledgement=ACKNOWLEDGEMENT,
            api_key=os.environ.get('BINANCE_API_KEY'),private_key_file='/run/secrets/key.pem')
        v.store=s;v.sync_time()
        e=SmokeExecutor(s,v,config)
        result=run(e,abort=lambda:interrupted[0])
        print(json.dumps(result,indent=2,allow_nan=False),flush=True)
        return 0 if result['flat'] and result['ledger_reconciled'] and not result['open_regular_orders'] and not result['open_algo_orders'] else 2
    except (VenueError,PreflightError,ValueError,KeyError,TypeError,OSError) as error:
        print(json.dumps(dict(real_money=True,passed=False,error=str(error) if isinstance(error,VenueError) else type(error).__name__,
            attention='Inspect saved diagnostic DB and Binance positions/orders. Rerun EXACT same scenario/direction to reconcile, never delete DB.')),flush=True)
        return 2
    finally:
        if v:v.close()
        if s:s.close()
        if lock.writer_lock:lock.writer_lock.close()


if __name__=='__main__':raise SystemExit(main())
