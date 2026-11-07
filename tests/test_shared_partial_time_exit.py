from dataclasses import replace
import pytest
from model_development.phase_2.backtest import Config, Portfolio, replay, parameter_grid, validate_config

def signal(day='2026-08-01', at=0, end=10, side='long', stop=90):
    return dict(sample_id=day,session_day=day,direction=side,stop=stop,entry_reference=100,
        entry_eligible_timestamp_ms=at,entry_deadline_timestamp_ms=at+1,force_exit_timestamp_ms=end)
def ticks(prices):
    return iter(dict(timestamp_ms=i,agg_trade_id=i,price=p) for i,p in enumerate(prices))
def run(prices,side='long',stop=90,fraction=.2,end=4,fee=0):
    cfg=Config(fee_bps=fee,tp1_r=1,tp1_fraction=fraction)
    p=Portfolio('test',[signal(end=end,side=side,stop=stop)],1000)
    replay([p],ticks(prices),cfg)
    return p

def test_partial_then_time():
    p=run([100,110,112,115,120])
    t=p.trades[0]
    assert t['tp1_price']==112
    assert t['r']==pytest.approx(.2*1.2+.8*2)
    assert t['quantity']==.5
    assert t['remaining_exit_quantity']==.4
    assert [o['quantity'] for o in p.orders]==[.5,.1,.4]

def test_partial_then_original_stop_and_fees():
    p=run([100,110,112,89,88],fee=4)
    t=p.trades[0]
    fees=(100*.5+112*.1+88*.4)*.0004
    assert t['fees']==pytest.approx(fees)
    assert t['pnl']==pytest.approx(1.2-4.8-fees)
    assert t['stop']==90
    assert t['exit_reason']=='initial_stop_next_print'
    assert sum(o['fee'] for o in p.orders)==pytest.approx(fees)

def test_short():
    p=run([100,90,88,85,80],side='short',stop=110)
    assert p.trades[0]['r']==pytest.approx(1.84)

def test_never_tp1():
    p=run([100,105,103,104,106])
    assert not p.trades[0]['tp1_filled']
    assert p.trades[0]['r']==pytest.approx(.6)

def test_tp1_pending_at_time_exit():
    p=run([100,105,106,110,120])
    assert p.trades[0]['tp1_filled']
    assert p.trades[0]['r']==pytest.approx(2)
    assert len(p.orders)==3

def test_pending_partial_gap_through_stop():
    p=run([100,110,89,88],end=10)
    assert p.trades[0]['r']==pytest.approx(-1.18)
    assert p.trades[0]['exit_reason']=='initial_stop_next_print'

def test_compounds_and_no_repeated_partial():
    cfg=Config(fee_bps=0,tp1_r=1,tp1_fraction=.5)
    p=Portfolio('test',[signal(end=4),signal('2026-08-02',at=5,end=9)],1000)
    replay([p],ticks([100,110,110,115,120,100,110,110,115,120]),cfg)
    assert p.trades[0]['pnl']==pytest.approx(7.5)
    assert p.trades[1]['equity_before_entry']==1007.5
    assert p.trades[1]['actual_initial_risk']==pytest.approx(1007.5*.005)
    assert len([o for o in p.orders if o['action']=='PARTIAL_EXIT'])==2
    assert p.equity==pytest.approx(1000*1.0075**2)

def test_nine_grid_and_invalid():
    assert len(parameter_grid(Config(),{'tp1_r':[1,1.5,2],'tp1_fraction':[.1,.2,.5]}))==9
    for cfg in [Config(tp1_r=1),Config(tp1_r=-1,tp1_fraction=.2),Config(tp1_r=1,tp1_fraction=1)]:
        with pytest.raises(ValueError):validate_config(cfg)

def test_legacy_fixed_unchanged():
    p=Portfolio('fixed',[signal(end=5)],1000,target_r=1)
    replay([p],ticks([100,110,112]),Config(fee_bps=0))
    assert p.trades[0]['r']==pytest.approx(1.2)
    assert p.trades[0]['exit_reason']=='fixed_target_next_print'


def test_raw_file_grid_equity_exports(tmp_path):
    import json
    from datetime import date
    import pyarrow as pa
    import pyarrow.parquet as pq
    from model_development.phase_2.backtest import run as replay_file
    raw=tmp_path/'ticks.parquet'
    pq.write_table(pa.table({'timestamp':[0,1,2,3,4], 'agg_trade_id':[0,1,2,3,4],
                            'price':[100.,120.,120.,125.,130.]}),raw)
    sig=tmp_path/'signals.jsonl'
    sig.write_text(json.dumps(signal(end=4))+'\n')
    cfg=Config(fee_bps=0)
    grid=parameter_grid(cfg,{'tp1_r':[1,1.5,2],'tp1_fraction':[.1,.2,.5]})
    result=replay_file(raw,{'sample':sig},tmp_path/'out',cfg,date(2026,8,1),date(2026,8,1),
                       variants=grid,plot_equity=True)
    assert len(result)==9
    assert (tmp_path/'out'/'complete.json').exists()
    for label,r in result.items():
        assert r['metrics']['trade_count']==1
        assert (tmp_path/'out'/label/'equity_curve.png').stat().st_size>1000
        trades=json.loads((tmp_path/'out'/label/'trades.jsonl').read_text())
        assert trades['fees']==0
        assert r['metrics']['final_equity']==pytest.approx(trades['equity_after_exit'])
