"""Durable local venue for the SAME FuturesExecutor used with Binance.

No HTTP client or credentials. Market orders match the next observed BBO, capped
at its displayed size (IOC); limits require an aggressor print through the level.
This is a declared fill proxy, not exchange queue/depth equivalence.
"""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time

from utils.engine.adapters.binance_execution import FuturesOrders, VenueError
from utils.engine.contracts import canonical_json, content_hash, validate_event
from utils.engine.execution.futures_preflight import number, positive, quantity_rules
from utils.engine.state.base import StateStore


class PaperFutures(FuturesOrders):
    execution_mode = 'paper'

    def __init__(self, database, *, symbol, specification, initial_equity,
                 maker_fee_bps, taker_fee_bps, leverage, max_notional, enabled=True):
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        self.symbol, self.armed, self.offset = symbol, enabled, 0
        if specification.get('symbol') != symbol or specification.get('marginAsset') != 'USDC':
            raise ValueError('Paper contract must identify the selected USDC instrument')
        self.rules = quantity_rules(specification)
        equity = positive(initial_equity)
        maker, taker = number(maker_fee_bps), number(taker_fee_bps)
        if max(maker,taker) >= 10000 or type(leverage) is not int or leverage < 1:
            raise ValueError('Invalid paper fees/leverage')
        self.path = Path(database).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.writer_lock = None
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        cfg = dict(symbol=symbol,specification=specification,initial_equity=str(equity),
                   maker_fee_bps=str(maker),taker_fee_bps=str(taker),leverage=leverage,max_notional=str(positive(max_notional)),
                   fill_model='next_bbo_l1_ioc_trade_through_v1')
        try:
            StateStore.acquire_writer(self)
            tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'paper_venue' not in tables:
                raise ValueError('Dedicated paper venue DB required; never reuse an account/live DB')
            if 'paper_venue' in tables:
                prior=json.loads(self.db.execute('SELECT payload FROM paper_venue WHERE id=1').fetchone()[0])
                if prior['config'] != cfg:raise ValueError('Paper venue configuration changed; use a fresh DB')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS paper_venue(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS paper_events(id TEXT PRIMARY KEY,hash TEXT NOT NULL)')
            self.db.execute('INSERT OR IGNORE INTO paper_venue VALUES(1,?)',(canonical_json(dict(config=cfg,
                wallet=str(equity),position='0',entry='0',clock=0,next_id=0,orders={},fills=[],funding=[],
                epoch=None,quote=None,last_trade=None)),))
            self.db.commit()
            self.state=json.loads(self.db.execute('SELECT payload FROM paper_venue WHERE id=1').fetchone()[0])
        except BaseException:
            self.close();raise
        self._key='paper:'+content_hash(cfg)

    def load_credentials(self):
        # Engine identity check only: no exchange key, file, environment or HTTP access.
        return None

    def close(self):
        self.db.close()
        if self.writer_lock:
            self.writer_lock.close();self.writer_lock=None

    def _commit(self):
        self.db.execute('UPDATE paper_venue SET payload=? WHERE id=1',(canonical_json(self.state),))
        self.db.commit()

    def _reload(self):
        self.state=json.loads(self.db.execute('SELECT payload FROM paper_venue WHERE id=1').fetchone()[0])

    def _id(self):
        self.state['next_id']+=1
        return self.state['next_id']

    def _order(self, role, p):
        cid=p.get('clientAlgoId',p.get('newClientOrderId'))
        if cid in self.state['orders']:raise VenueError(400,-4116,reason='Duplicate client ID')
        r=dict(symbol=self.symbol,side=p['side'],positionSide='BOTH',
               submitted_at_ms=max(self.state['clock'],int(time.time()*1000)),role=role,params=deepcopy(p))
        if role=='STOP':
            r.update(clientAlgoId=cid,algoId=self._id(),orderType='STOP_MARKET',algoStatus='NEW',
                     workingType='CONTRACT_PRICE',closePosition=True,priceProtect=False,
                     triggerPrice=p['triggerPrice'],actualOrderId='')
        else:
            r.update(clientOrderId=cid,orderId=self._id(),type=p['type'],origQty=p['quantity'],
                     executedQty='0',avgPrice='0',status='NEW',reduceOnly=p.get('reduceOnly')=='true',
                     price=p.get('price','0'),timeInForce=p.get('timeInForce','GTC'))
        self.state['orders'][cid]=r
        return r

    def _fill(self,r,qty,price,*,maker=False):
        position=D(self.state['position']);qty=min(qty,D(r['origQty'])-D(r['executedQty']))
        delta=qty if r['side']=='BUY' else -qty
        if r['reduceOnly']:
            if not position or position*delta>=0:qty=D(0)
            else:qty=min(qty,abs(position))
        if qty<=0:return D(0)
        delta=qty if r['side']=='BUY' else -qty
        pnl=D(0)
        if position and position*delta<0:
            if qty>abs(position):raise ValueError('Paper reversal outside supported one-allocation contract')
            pnl=(1 if position>0 else -1)*(price-D(self.state['entry']))*qty
        else:
            self.state['entry']=str((abs(position)*D(self.state['entry'])+qty*price)/(abs(position)+qty))
        fee=qty*price*D(self.state['config']['maker_fee_bps' if maker else 'taker_fee_bps'])/10000
        self.state['position']=str(position+delta)
        self.state['wallet']=str(D(self.state['wallet'])+pnl-fee)
        before=D(r['executedQty'])
        r.update(executedQty=str(before+qty),avgPrice=str((before*D(r['avgPrice'])+qty*price)/(before+qty)),
                 status='FILLED' if before+qty==D(r['origQty']) else 'PARTIALLY_FILLED',updateTime=self.state['clock'])
        self.state['fills'].append(dict(symbol=self.symbol,id=len(self.state['fills'])+1,orderId=r['orderId'],
            side=r['side'],qty=str(qty),price=str(price),realizedPnl=str(pnl),commission=str(fee),
            commissionAsset='USDC',maker=maker,time=self.state['clock']))
        return qty

    def on_event(self, raw):
        self._reload()
        event=validate_event(raw)
        if (event['venue'],event['product'],event['instrument_id']) != ('binance','futures',self.symbol):
            raise ValueError('Paper event source/instrument differs')
        if event['quality']!='complete':raise ValueError('Paper execution needs complete public events')
        encoded=content_hash(event);eid=event['event_id']
        prior=self.db.execute('SELECT hash FROM paper_events WHERE id=?',(eid,)).fetchone()
        if prior:
            if prior[0]!=encoded:raise ValueError('Conflicting public event revision')
            return
        epoch=event.get('source_epoch')
        if not epoch or (self.state['epoch'] is not None and epoch!=self.state['epoch']):
            raise ValueError('Missing/changed public source epoch')
        at=event['available_at_ms']
        if at<self.state['clock']:raise ValueError('Paper event availability moved backward')
        try:
            self.state['clock']=at;self.state['epoch']=epoch
            p=event['payload'];kind=event['event_type']
            if kind=='quote':
                if p.get('symbol')!=self.symbol:raise ValueError('Quote symbol differs')
                bid,ask=positive(p['bidPrice']),positive(p['askPrice'])
                if bid>ask:raise ValueError('Crossed quote')
                if self.state['quote'] and event['event_timestamp_ms']<self.state['quote']['time']:
                    raise ValueError('Public quote chronology moved backward')
                depth={'BUY':number(p['askQty']),'SELL':number(p['bidQty'])}
                self.state['quote']=dict(symbol=self.symbol,bidPrice=str(bid),askPrice=str(ask),time=event['event_timestamp_ms'])
                for r in sorted(self.state['orders'].values(),key=lambda r:r.get('orderId',r.get('algoId'))):
                    if r.get('type')!='MARKET' or r.get('status') not in ('NEW','PARTIALLY_FILLED') or event['event_timestamp_ms']<=r['submitted_at_ms']:continue
                    side=r['side'];size=depth[side]
                    qty=(size/self.rules['step']).to_integral_value(rounding='ROUND_FLOOR')*self.rules['step']
                    used=self._fill(r,qty,ask if side=='BUY' else bid)
                    depth[side]-=used
                    if r['status']!='FILLED':r['status']='EXPIRED'
                    if r.get('parent'):
                        self.state['orders'][r['parent']]['algoStatus']='FINISHED'
            elif kind=='trade':
                for t in p['trades']:
                    stamp=t['timestamp_ms'];seq=t['agg_trade_id']
                    if type(stamp) is not int or type(seq) is not int or min(stamp,seq)<0 or stamp>event['event_timestamp_ms'] or type(t['buy']) is not bool or t.get('symbol',self.symbol)!=self.symbol:
                        raise ValueError('Invalid native print identity/time/side')
                    if self.state['last_trade'] is not None and seq!=self.state['last_trade']+1:
                        raise ValueError('Native print sequence gap/reversal; continuity is unverified')
                    self.state['last_trade']=seq
                    price,quantity=positive(t['price']),positive(t['quantity'])
                    for cid,r in list(self.state['orders'].items()):
                        if stamp<=r['submitted_at_ms']:continue
                        if r.get('algoStatus')=='NEW':
                            hit=price<=D(r['triggerPrice']) if r['side']=='SELL' else price>=D(r['triggerPrice'])
                            if hit:
                                size=abs(D(self.state['position']))
                                if not size:r['algoStatus']='EXPIRED';continue
                                child=self._order('CLOSE',dict(symbol=self.symbol,side=r['side'],type='MARKET',
                                    quantity=str(size),newClientOrderId='paper-stop-'+str(r['algoId']),reduceOnly='true'))
                                child['parent']=cid
                                r.update(algoStatus='TRIGGERED',actualOrderId=child['orderId'])
                        elif r.get('type')=='LIMIT' and r['status'] in ('NEW','PARTIALLY_FILLED'):
                            hit=(t['buy'] and r['side']=='SELL' and price>D(r['price'])) or (not t['buy'] and r['side']=='BUY' and price<D(r['price']))
                            if hit:quantity-=self._fill(r,quantity,D(r['price']),maker=True)
            elif kind=='funding':
                rate=number(p['funding_rate'],signed=True);mark=positive(p['mark_price'])
                amount=-D(self.state['position'])*mark*rate
                self.state['wallet']=str(D(self.state['wallet'])+amount)
                self.state['funding'].append(dict(symbol=self.symbol,asset='USDC',incomeType='FUNDING_FEE',
                    income=str(amount),tranId=eid,time=event['event_timestamp_ms']))
            else:raise ValueError('Unsupported paper execution event; use quote/trade/funding')
            self.db.execute('INSERT INTO paper_events VALUES(?,?)',(eid,encoded));self._commit()
        except BaseException:
            self.db.rollback();self._reload();raise

    def get(self,path,**params):
        if path=='/fapi/v1/exchangeInfo':return {'symbols':[deepcopy(self.state['config']['specification'])]}
        if path=='/fapi/v1/ticker/bookTicker' and params.get('symbol')==self.symbol:
            if self.state['quote'] is None:raise ValueError('No measured public quote available')
            return deepcopy(self.state['quote'])
        raise ValueError('Unsupported paper public endpoint')

    def _request(self,method,path,params):
        self._reload()
        try:
            return self._local_request(method,path,params)
        except sqlite3.Error as exc:
            self.db.rollback();self._reload()
            raise VenueError(503,uncertain=True,reason='Paper venue persistence unavailable') from exc
        except BaseException:
            self.db.rollback();self._reload();raise

    def _local_request(self,method,path,params):
        if params.get('symbol',self.symbol)!=self.symbol:raise ValueError('Paper instrument differs')
        orders=self.state['orders']
        if method=='GET':
            if path=='/fapi/v1/accountConfig':return dict(canTrade=True,dualSidePosition=False,multiAssetsMargin=False)
            if path=='/fapi/v3/account':
                margin=abs(D(self.state['position']))*D(self.state['entry'])/self.state['config']['leverage']
                return dict(assets=[dict(asset='USDC',walletBalance=self.state['wallet'],availableBalance=str(max(D(0),D(self.state['wallet'])-margin)))])
            if path=='/fapi/v3/positionRisk':return [] if not D(self.state['position']) else [dict(symbol=self.symbol,positionSide='BOTH',positionAmt=self.state['position'])]
            if path=='/fapi/v1/symbolConfig':return [dict(symbol=self.symbol,leverage=self.state['config']['leverage'],marginType='CROSSED',maxNotionalValue=self.state['config']['max_notional'])]
            if path=='/fapi/v1/openOrders':return deepcopy([r for r in orders.values() if r.get('status') in ('NEW','PARTIALLY_FILLED')])
            if path=='/fapi/v1/openAlgoOrders':return deepcopy([r for r in orders.values() if r.get('algoStatus') in ('NEW','TRIGGERING','TRIGGERED')])
            if path=='/fapi/v1/userTrades':return deepcopy([r for r in self.state['fills'] if str(r['orderId'])==str(params['orderId'])])
            if path=='/fapi/v1/income':return deepcopy([r for r in self.state['funding'] if params['startTime']<=r['time']<=params['endTime']])
        cid=params.get('clientAlgoId',params.get('origClientOrderId',params.get('newClientOrderId')))
        if method=='POST':
            if not self.armed:raise ValueError('Paper execution disabled')
            role='STOP' if path=='/fapi/v1/algoOrder' else 'TP1' if params['type']=='LIMIT' else 'CLOSE' if params.get('reduceOnly')=='true' else 'ENTRY'
            r=self._order(role,params);self._commit();return deepcopy(r)
        r=orders.get(cid)
        if r is None and path=='/fapi/v1/order' and 'orderId' in params:
            r=next((r for r in orders.values() if str(r.get('orderId'))==str(params['orderId'])),None)
        if r is None:raise VenueError(400,-2013)
        if method=='DELETE':
            key='algoStatus' if 'algoStatus' in r else 'status'
            if r[key] in ('NEW','PARTIALLY_FILLED'):r[key]='CANCELED'
            self._commit()
        elif method!='GET':raise ValueError('Unsupported paper venue operation')
        return deepcopy(r)
