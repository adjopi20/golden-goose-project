"""One explicitly authorized close-only native stop, then cancel; never an entry."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from live_engine.adapters.binance_futures_execution import ACKNOWLEDGEMENT, FuturesOrders, VenueError
from live_engine.adapters.binance_futures_readonly import PreflightError
from live_engine.futures_execution import FuturesExecutor, rounded
from live_engine.futures_preflight import number, one, positive, quantity_rules
from live_engine.state_store import StateStore
from trading_core.contracts import canonical_json

ACK = 'I_AUTHORIZE_ONE_CLOSE_ONLY_STOP_PROBE'
TERMINAL = ('CANCELED','EXPIRED','REJECTED','FINISHED')


class Journal:
    acquire_writer = StateStore.acquire_writer

    def __init__(self,path):
        self.path=Path(path);self.writer_lock=None
        self.path.parent.mkdir(parents=True,exist_ok=True)

    def record(self,event,**data):
        row=dict(event=event,local_ms=int(time.time()*1000),**data)
        created=not self.path.exists()
        with self.path.open('a',encoding='utf-8') as f:
            f.write(canonical_json(row)+'\n');f.flush();os.fsync(f.fileno())
        if created and os.name!='nt':
            fd=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def read(self):
        if not self.path.exists():return []
        rows=[]
        for line in self.path.read_text(encoding='utf-8').splitlines():
            try:rows.append(json.loads(line))
            except ValueError:break  # Durable first header still identifies an interrupted request.
        if not rows:raise ValueError('Empty/damaged probe journal; inspect before any new request')
        return rows


def account_state(v):
    rows=v.read('/fapi/v3/positionRisk');orders=v.read('/fapi/v1/openOrders');algos=v.read('/fapi/v1/openAlgoOrders')
    if not all(isinstance(rs,list) and all(isinstance(r,dict) for r in rs) for rs in (rows,orders,algos)):
        raise ValueError('Cannot verify positions/orders')
    wallet=number(one(v.read('/fapi/v3/account')['assets'],'asset','USDC')['walletBalance'],signed=True)
    return dict(positions=sum(number(r['positionAmt'],signed=True)!=0 for r in rows),
                regular_orders=len(orders),algo_orders=len(algos),wallet=str(wallet))


def require_worker_off(config_path,db_path):
    policy=json.loads(Path(config_path).read_text())
    if policy.get('initial_equity')!='200' or any(policy.get(k) is not False for k in ('execution_enabled','accept_new_entries')):
        raise ValueError('200 allocation worker policy must remain OFF')
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        meta=dict(db.execute('SELECT key,value FROM metadata'))
        age=int(time.time()*1000)-int(meta.get('worker_heartbeat_ms',0))
        if (meta.get('owner'),meta.get('worker_armed'))!=('binance_futures_pilot','0') or not 0<=age<60000 or meta.get('worker_error'):
            raise ValueError('Worker must be fresh, error-free and disarmed')
        if db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] or db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]:
            raise ValueError('Pilot journal already has execution activity; inspect first')


def run_probe(v,journal_path):
    journal=Journal(journal_path);journal.acquire_writer()
    try:
        previous=journal.read()
        if previous:
            header=previous[0]
            if header.get('event')!='prepared' or header.get('kind')!='eth_close_only_stop_probe_v1':
                raise ValueError('Foreign probe journal')
            params=header['params'];before=header['before']
        else:
            cfg=v.read('/fapi/v1/accountConfig')
            if cfg.get('dualSidePosition') is not False or cfg.get('multiAssetsMargin') is not False:
                raise ValueError('Require one-way/single-asset mode')
            before=account_state(v)
            if any(before[k] for k in ('positions','regular_orders','algo_orders')):
                raise ValueError('Account must be completely flat/order-free; no probe sent')
            spec=one(v.get('/fapi/v1/exchangeInfo')['symbols'],'symbol','ETHUSDC')
            if (spec.get('status'),spec.get('marginAsset'))!=('TRADING','USDC'):raise ValueError('Wrong/unavailable contract')
            quote=v.get('/fapi/v1/ticker/bookTicker',symbol='ETHUSDC')
            if quote.get('symbol')!='ETHUSDC' or type(quote.get('time')) is not int or not -1000<=int(time.time()*1000)+(v.offset or 0)-quote['time']<=10000:
                raise ValueError('Stale/wrong quote')
            bid=positive(quote['bidPrice'])
            trigger=rounded(bid*Decimal('.90'),quantity_rules(spec)['tick_size'])
            if not 0<trigger<bid:raise ValueError('Unsafe trigger geometry')
            params=dict(algoType='CONDITIONAL',symbol='ETHUSDC',side='SELL',positionSide='BOTH',type='STOP_MARKET',
                        triggerPrice=str(trigger),workingType='CONTRACT_PRICE',closePosition='true',priceProtect='false',
                        clientAlgoId='ggp-stopprobe-'+secrets.token_hex(8))
            journal.record('prepared',kind='eth_close_only_stop_probe_v1',params=params,before=before,bid=str(bid))
        # Stored requests are also checked before cancellation/recovery; no new POST on rerun.
        expected=dict(algoType='CONDITIONAL',symbol='ETHUSDC',side='SELL',positionSide='BOTH',type='STOP_MARKET',
                      workingType='CONTRACT_PRICE',closePosition='true',priceProtect='false')
        if set(params)!=set(expected)|{'triggerPrice','clientAlgoId'} or any(params[k]!=val for k,val in expected.items()):
            raise ValueError('Probe request changed; do not execute')
        cid=params['clientAlgoId'];v.check_id(cid) if hasattr(v,'check_id') else None
        if not cid.startswith('ggp-stopprobe-'):raise ValueError('Foreign probe order ID')
        positive(params['triggerPrice'])
        validator=FuturesExecutor(None,v,{'symbol':'ETHUSDC'})
        row=dict(role='STOP',client_id=cid,params=canonical_json(params))
        valid=False;rejected=any(r.get('event')=='post_rejected' for r in previous);errors=[];cancelled=False;last=None
        post_error=next((r for r in reversed(previous) if r.get('event') in ('post_rejected','post_unknown')),None)
        try:
            if not previous:
                journal.record('sending',client_id=cid)
                started=time.perf_counter_ns()
                try:
                    native=v.submit('STOP',params)
                    duration=(time.perf_counter_ns()-started)/1000000
                    state=validator._validate_response(native,row)
                    journal.record('post_accepted',client_id=cid,state=state,algo_id=native['algoId'],duration_ms=duration)
                except VenueError as e:
                    rejected=not e.uncertain
                    post_error=dict(http_status=e.status,venue_code=e.code,venue_reason=e.reason)
                    journal.record('post_rejected' if rejected else 'post_unknown',client_id=cid,**post_error)
            native=v.query(cid,stop=True,symbol='ETHUSDC')
            if native:
                state=validator._validate_response(native,row);valid=True;last=native
                journal.record('query_validated',client_id=cid,state=state,algo_id=native['algoId'])
        except Exception as e:
            errors.append(type(e).__name__)
            journal.record('probe_error',client_id=cid,error_type=type(e).__name__)
        finally:
            # DELETE is idempotent cleanup of this single saved ID, never a new order.
            for attempt in range(3):
                if rejected:break
                try:v.cancel(cid,stop=True,symbol='ETHUSDC')
                except (VenueError,PreflightError):pass
                try:
                    native=v.query(cid,stop=True,symbol='ETHUSDC')
                    if native:
                        last=native;state=validator._validate_response(native,row)
                        journal.record('cleanup_observed',client_id=cid,state=state)
                        if state=='CANCELED':cancelled=True;break
                        if state in TERMINAL:break
                except Exception as e:errors.append(type(e).__name__)
                if attempt<2:time.sleep(.5)
        after=account_state(v)
        no_execution=not last or (last.get('actualOrderId') in (None,'','0',0) and not number(last.get('triggerTime',0)))
        clean=not any(after[k] for k in ('positions','regular_orders','algo_orders')) and after['wallet']==before['wallet']
        result=dict(stage='native_close_only_stop_probe',client_id=cid,trigger_price=params['triggerPrice'],
                    recovery_only=bool(previous),accepted_and_query_validated=valid,cancellation_confirmed=cancelled,
                    last_native_state=last.get('algoStatus') if last else None,before=before,after=after,
                    no_trigger_observed=no_execution,passed=valid and cancelled and clean and no_execution and not errors,
                    rejected=rejected,post_error={k:post_error.get(k) for k in ('http_status','venue_code','venue_reason')} if post_error else None,
                    errors=errors,journal=str(journal.path),automatic_entries_enabled=False)
        journal.record('final_report',report=result)
        return result
    finally:
        if journal.writer_lock:journal.writer_lock.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--acknowledge-native-stop-probe',required=True)
    args=p.parse_args()
    if args.acknowledge_native_stop_probe!=ACK:p.error('Explicit one-stop authorization required')
    v=FuturesOrders(execution_enabled=True,acknowledgement=ACKNOWLEDGEMENT,
                   api_key=os.environ.get('BINANCE_API_KEY'),private_key_file='/run/secrets/key.pem')
    try:
        require_worker_off('/app/config/pilot.json','/data/execution-200.sqlite')
        v.sync_time()
        result=run_probe(v,'/data/native-stop-probe.jsonl')
        print(json.dumps(result,indent=2,allow_nan=False),flush=True)
        return 0 if result['passed'] else 2
    except (VenueError,PreflightError) as e:
        print(json.dumps({'passed':False,'error':str(e),'automatic_entries_enabled':False}),flush=True);return 2
    except Exception as e:
        print(json.dumps({'passed':False,'error_type':type(e).__name__,'automatic_entries_enabled':False,
                          'attention':'inspect persistent native-stop-probe.jsonl; never rerun a fresh order blindly'}),flush=True);return 2
    finally:v.close()


if __name__=='__main__':raise SystemExit(main())
