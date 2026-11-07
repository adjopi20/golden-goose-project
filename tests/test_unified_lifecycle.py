"""Same order lifecycle, distinct venue adapters/accounts; no real endpoints."""
from copy import deepcopy
from decimal import Decimal as D
import json
import sqlite3

import pytest

from utils.engine.contracts import make_event
from utils.engine.execution.session import create_execution
from utils.engine.execution.preny_futures import FuturesExecutor
from utils.engine.adapters.paper_futures import PaperFutures
from utils.engine.adapters.binance_execution import FuturesOrders
from utils.engine.adapters.binance_execution import VenueError
from test_futures_execution import policy,intent,NOW,FakeVenue
from test_futures_preflight import specification


def public(kind,at,**payload):
    return make_event(venue='binance',product='futures',instrument_id='ETHUSDC',source='native_test',
        source_epoch='test-source',event_type=kind,event_timestamp_ms=at,available_at_ms=at,
        received_timestamp_ms=at,stream_sequence=at,source_sequence=at,feature_version='test',quality='complete',payload=payload)


def quote(at,bid='1000',ask='1000',bid_qty='100',ask_qty='100'):
    return public('quote',at,symbol='ETHUSDC',bidPrice=bid,askPrice=ask,bidQty=bid_qty,askQty=ask_qty)


def trade(at,seq,price,quantity='1',buy=True):
    return public('trade',at,trades=[dict(timestamp_ms=at,agg_trade_id=seq,price=price,quantity=quantity,buy=buy)])


@pytest.fixture
def paper(tmp_path,monkeypatch):
    clock=[NOW]
    monkeypatch.setattr('utils.engine.execution.preny_futures.time.time',lambda:clock[0]/1000)
    cfg=policy();options=dict(database=tmp_path/'venue.sqlite',specification=specification(),maker_fee_bps=2,taker_fee_bps=4,leverage=5,max_notional='100000')
    engine=create_execution(mode='paper',config=cfg,database=tmp_path/'paper.sqlite',adapter_options=options)
    yield engine,clock,options
    engine.s.close();engine.v.close()


def opened(paper):
    e,clock,_=paper
    e.v.on_event(quote(NOW))
    p=intent(environment='server-paper')
    assert e.offer(p,NOW)=='PENDING'
    e.cycle(NOW)
    assert e.s.active()['state']=='ENTRY_PENDING'
    assert e.v.state['fills']==[]  # no retroactive/current-quote fill
    clock[0]=NOW+1;e.v.on_event(quote(clock[0]));e.cycle(clock[0])
    assert e.s.active()['state']=='OPEN'
    return e,clock,p


def test_both_modes_construct_exact_same_executor(tmp_path,monkeypatch,paper):
    captured=[]
    def live_venue(**kwargs):
        captured.append(kwargs);v=FakeVenue();v.execution_mode='live';return v
    monkeypatch.setattr('utils.engine.adapters.binance_execution.FuturesOrders',live_venue)
    live=create_execution(mode='live',config=policy(),database=tmp_path/'live.sqlite',adapter_options={})
    try:
        assert type(live) is type(paper[0]) is FuturesExecutor
        assert captured==[dict(execution_enabled=True)]
        assert live.offer(intent(),NOW)=='PENDING'
        live.cycle(NOW)
        e,clock,_=opened(paper)
        assert [r[0] for r in live.s.db.execute('SELECT role FROM orders ORDER BY rowid')]==[r[0] for r in e.s.db.execute('SELECT role FROM orders ORDER BY rowid')]
        assert live.s.active()['state']==e.s.active()['state']=='OPEN'
        assert live.v.posts[0][1]['quantity']==json.loads(e.s.db.execute("SELECT params FROM orders WHERE role='ENTRY'").fetchone()[0])['quantity']
    finally:live.s.close()


def test_partial_tp_funding_stop_cancel_and_closed_equity_same_lifecycle(paper):
    e,clock,_=opened(paper)
    clock[0]=NOW+2;e.v.on_event(trade(clock[0],1,'1011','.004'));e.cycle(clock[0])
    assert e.s.db.execute("SELECT state FROM orders WHERE role='TP1'").fetchone()[0]=='PARTIALLY_FILLED'
    clock[0]=NOW+3;e.v.on_event(public('funding',clock[0],funding_rate='0.0001',mark_price='1008'))
    e.accounting(clock[0])
    assert e.s.db.execute('SELECT COUNT(*) FROM funding').fetchone()[0]==1
    clock[0]=NOW+4;e.v.on_event(trade(clock[0],2,'989',buy=False));e.cycle(clock[0])
    assert e.s.db.execute("SELECT state FROM orders WHERE role='STOP'").fetchone()[0]=='TRIGGERED'
    clock[0]=NOW+5;e.v.on_event(quote(clock[0],bid='989',ask='990'));e.cycle(clock[0])
    assert e.s.active() is None
    assert e.s.db.execute('SELECT state FROM sessions').fetchone()[0]=='CLOSED'
    assert e.s.db.execute("SELECT state FROM orders WHERE role='TP1'").fetchone()[0]=='CANCELED'
    assert e.s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==3
    assert e.s.equity(200)==D(e.v.state['wallet'])


def test_orders_and_event_receipts_survive_restart_without_resubmission(paper):
    e,clock,options=paper
    e.v.on_event(quote(NOW));p=intent(environment='server-paper');e.offer(p,NOW);e.cycle(NOW)
    account=e.s.path;e.s.close();e.v.close()
    replacement=create_execution(mode='paper',config=policy(),database=account,adapter_options=options)
    # Keep fixture cleanup bound to the reopened resources.
    e.s,e.v=replacement.s,replacement.v
    e.cycle(NOW)
    assert len(e.v.state['orders'])==1
    event=quote(NOW+1);clock[0]=NOW+1;e.v.on_event(event);e.v.on_event(event);e.cycle(clock[0])
    assert len(e.v.state['fills'])==1
    assert e.s.active()['state']=='OPEN'
    assert e.offer(p,clock[0])=='duplicate'


def test_partial_market_depth_expires_remainder_and_preserves_protection(paper):
    e,clock,_=paper;e.v.on_event(quote(NOW));e.offer(intent(environment='server-paper'),NOW);e.cycle(NOW)
    clock[0]=NOW+1;e.v.on_event(quote(clock[0],ask_qty='.05'));e.cycle(clock[0])
    assert e.s.db.execute("SELECT state FROM orders WHERE role='ENTRY'").fetchone()[0]=='EXPIRED'
    assert e.s.db.execute("SELECT state FROM orders WHERE role='STOP'").fetchone()[0]=='NEW'
    assert e.s.active()['state']=='OPEN'
    assert e.v.state['position']=='.05' or D(e.v.state['position'])==D('.05')


def test_no_limit_fill_at_touch_no_backdating_and_atomic_bad_event(paper):
    e,clock,_=opened(paper)
    event=trade(NOW+2,1,'1010','.01');e.v.on_event(event)
    assert len(e.v.state['fills'])==1  # touch is not a queue fill
    before=deepcopy(e.v.state)
    with pytest.raises(ValueError):e.v.on_event(quote(NOW+3,bid_qty='-1'))
    assert e.v.state==before
    with pytest.raises(ValueError,match='backward'):e.v.on_event(quote(NOW-1))
    assert e.v.state==before
    changed=deepcopy(event);changed['payload']['trades'][0]['price']='1009'
    with pytest.raises(ValueError,match='Conflicting'):e.v.on_event(changed)


def test_modes_and_existing_ledgers_cannot_be_swapped(paper,tmp_path):
    e,_,options=paper
    with pytest.raises(ValueError,match='adapter mismatch'):
        FuturesExecutor(e.s,e.v,policy())
    with pytest.raises(ValueError,match='live ledger identity'):
        create_execution(mode='live',config=policy(),database=e.s.path,adapter_options={})
    with pytest.raises(ValueError,match='separate'):
        create_execution(mode='paper',config=policy(),database=tmp_path/'same.sqlite',adapter_options=dict(options,database=tmp_path/'same.sqlite'))


def test_paper_inherits_live_order_validation_without_network(paper,monkeypatch):
    e,_,_=paper
    def forbidden(*args,**kwargs):raise AssertionError('Paper attempted HTTP')
    monkeypatch.setattr('requests.Session.request',forbidden)
    with pytest.raises(ValueError):
        e.v.submit('ENTRY',{})
    assert isinstance(e.v,PaperFutures) and isinstance(e.v,FuturesOrders)
    e.v.load_credentials()
    assert e.v._key.startswith('paper:')


def test_short_stop_uses_buy_side_liquidity_and_reconciles(paper):
    e,clock,_=paper;e.v.on_event(quote(NOW))
    e.offer(intent(environment='server-paper',direction='short',initial_stop=1010.),NOW);e.cycle(NOW)
    clock[0]=NOW+1;e.v.on_event(quote(clock[0]));e.cycle(clock[0])
    assert D(e.v.state['position'])<0
    clock[0]=NOW+2;e.v.on_event(trade(clock[0],1,'1011'));e.cycle(clock[0])
    clock[0]=NOW+3;e.v.on_event(quote(clock[0],bid='1010',ask='1011'));e.cycle(clock[0])
    assert e.s.active() is None and D(e.v.state['position'])==0
    assert e.s.equity(200)==D(e.v.state['wallet'])<200
    assert [r['side'] for r in e.v.state['fills']]==['SELL','BUY']


def test_time_exit_uses_same_cancel_and_close_lifecycle(paper):
    e,clock,_=opened(paper)
    clock[0]=json.loads(e.s.active()['plan'])['force_exit_ms'];e.cycle(clock[0])
    assert e.s.active()['state']=='EXIT_PENDING'
    assert e.s.db.execute("SELECT state FROM orders WHERE role='CLOSE'").fetchone()[0]=='NEW'
    clock[0]+=1;e.v.on_event(quote(clock[0]));e.cycle(clock[0])
    assert e.s.active() is None
    assert e.s.equity(200)==D(e.v.state['wallet'])


def test_only_one_writer_can_own_the_paper_venue(paper):
    e,_,options=paper
    with pytest.raises(OSError):
        PaperFutures(options['database'],symbol='ETHUSDC',initial_equity=200,
                     **{k:v for k,v in options.items() if k!='database'})


def test_delayed_decision_cannot_fill_from_a_pre_submission_quote(paper):
    e,clock,_=paper;e.v.on_event(quote(NOW));e.offer(intent(environment='server-paper'),NOW)
    clock[0]=NOW+500;e.cycle(clock[0])
    old=quote(NOW+1);old['available_at_ms']=old['received_timestamp_ms']=NOW+501
    clock[0]=NOW+501;e.v.on_event(old);e.cycle(clock[0])
    assert e.v.state['fills']==[]
    clock[0]=NOW+502;e.v.on_event(quote(clock[0]));e.cycle(clock[0])
    assert len(e.v.state['fills'])==1


def test_unsupported_contract_rejected_before_creating_an_account(tmp_path):
    with pytest.raises(ValueError,match='TF-PRENY-ETH-001'):
        create_execution(mode='paper',config=dict(policy(),symbol='BNBUSDC'),database=tmp_path/'unsupported.sqlite',adapter_options={})
    assert not (tmp_path/'unsupported.sqlite').exists()


@pytest.mark.parametrize('fault',['negative_id','negative_time','wrong_symbol','gap'])
def test_invalid_or_discontinuous_print_never_mutates_venue(paper,fault):
    e,clock,_=opened(paper)
    e.v.on_event(trade(NOW+2,1,'1000'))
    bad=trade(NOW+3,2,'1000');row=bad['payload']['trades'][0]
    if fault=='negative_id':row['agg_trade_id']=-1
    elif fault=='negative_time':row['timestamp_ms']=-1
    elif fault=='wrong_symbol':row['symbol']='BNBUSDC'
    else:row['agg_trade_id']=3
    before=deepcopy(e.v.state)
    with pytest.raises(ValueError):e.v.on_event(bad)
    assert e.v.state==before


def test_failed_venue_commit_never_creates_a_phantom_order(paper,monkeypatch):
    e,clock,_=paper;e.v.on_event(quote(NOW));e.offer(intent(environment='server-paper'),NOW)
    original=e.v._commit
    def unavailable():raise sqlite3.OperationalError('test disk failure')
    monkeypatch.setattr(e.v,'_commit',unavailable)
    e.cycle(NOW)
    assert e.v.state['orders']=={}
    assert e.s.db.execute("SELECT state FROM orders WHERE role='ENTRY'").fetchone()[0]=='UNKNOWN'
    monkeypatch.setattr(e.v,'_commit',original)
    clock[0]=NOW+1;e.v.on_event(quote(clock[0]));e.cycle(clock[0])
    assert e.v.state['orders']=={} and e.v.state['fills']==[]


def test_lost_ack_after_venue_commit_queries_instead_of_resubmitting(paper,monkeypatch):
    e,clock,_=paper;e.v.on_event(quote(NOW));e.offer(intent(environment='server-paper'),NOW)
    original=e.v._commit
    def lost_ack():
        original();raise VenueError(503,uncertain=True)
    monkeypatch.setattr(e.v,'_commit',lost_ack)
    e.cycle(NOW)
    assert len(e.v.state['orders'])==1
    monkeypatch.setattr(e.v,'_commit',original)
    clock[0]=NOW+1;e.v.on_event(quote(clock[0]));e.cycle(clock[0])
    assert len(e.v.state['fills'])==1
    assert e.s.active()['state']=='OPEN'
