import importlib.util
from pathlib import Path

path=Path(__file__).parents[1]/'model_development/models/trend_following_preny_profile_15m/legacy/prepare_selected_entries.py'
spec=importlib.util.spec_from_file_location('selected_entries',path)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def row(labels=(),state='WAIT',direction='long',ema=.6,adx=25):
    return dict(direction=direction,trend_15m=dict(price_vs_ema200_pct=ema,adx14=adx,trade_alignment='aligned'),
        effort_result_pre_entry=dict(setup_direction=dict(labels=list(labels),state=state)),
        research_gates={'delta_without_result':{'gate':False}})

def test_union_directions_and_absorption():
    r=row(labels=['possible_absorption'])
    assert m.keep(r,'c2_union_i_a_p')
    assert not m.keep(r,'c2_union_i_p')
    assert m.keep(row(state='SUPPORT_PERSISTING'),'c2_union_i_p')
    assert m.keep(row(labels=['initiative']),'c2_union_i_p')
    assert not m.keep(row(),'c2_union_i_p')

def test_signed_ema_and_separate_adx():
    assert m.keep(row(),'ema_0p5')
    assert not m.keep(row(direction='short'),'ema_0p5')
    assert m.keep(row(direction='short',ema=-.6),'ema_0p5')
    assert m.keep(row(ema=-.6),'adx_25')
    assert not m.keep(row(),'adx_30')
    assert m.keep(row(ema=None),'ema_0p5')
    assert not m.keep(row(),'c1_delta')

def test_grid_population():
    assert [len(m.POLICIES[s]) for s in ('BTCUSDC','ETHUSDC','BNBUSDC','HYPEUSDT')]==[3,6,3,9]
