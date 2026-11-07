import numpy as np
import pandas as pd

from combine_va_adx_sizing import VA_FEATURE, allocate, gate
import observe_edge_sizing as sizing


def fixture():
    n = 180
    train = pd.DataFrame(dict(session_date=pd.date_range('2024-01-01',periods=n).strftime('%Y-%m-%d'),
        macro_4h_adx=np.arange(n,dtype=float), distance=np.r_[np.full(90,.001),np.full(90,.01)],
        gross_r=np.repeat([1.,.2,-1.],60)))
    train[VA_FEATURE] = np.r_[np.zeros(90),np.ones(90)]
    artifact = sizing.fit_component(train.macro_4h_adx,train.gross_r)
    va = dict(calibration=dict(kind='numeric',cutpoints=[0.,.5]),selected_category='low')
    test = train.iloc[[0,60,120]].copy()
    test['session_date'] = ['2024-09-01','2024-09-02','2024-09-03']
    return train,test,va,artifact


def test_fixed_control_matches_selected_train_not_unrestricted_train():
    train,test,va,adx = fixture()
    result, control = allocate(train,test,va,adx)
    selected = gate(train,va)
    scored = sizing.effective_risk(1+sizing.apply_component(selected.macro_4h_adx,adx),selected.distance)
    fixed_risk = np.minimum(control['fixed_matched_risk'],sizing.CAP*selected.distance)
    np.testing.assert_allclose(fixed_risk.mean(),scored.mean(),atol=1e-12)
    assert control['va_train_entries'] == 90 and len(result) == 2
    unrestricted_fixed = sizing.match_fixed_risk(train.distance,
        sizing.effective_risk(1+sizing.apply_component(train.macro_4h_adx,adx),train.distance).mean())
    assert abs(unrestricted_fixed-control['fixed_matched_risk']) > 1e-5


def test_future_test_outcomes_cannot_change_weights_or_control():
    train,test,va,adx = fixture()
    first, a = allocate(train,test,va,adx)
    test['gross_r'] = [-1e6, 1e6, 3e6]
    second, b = allocate(train,test,va,adx)
    np.testing.assert_array_equal(first.multiplier,second.multiplier)
    np.testing.assert_array_equal(first.matched_risk,second.matched_risk)
    assert a == b


def test_missing_adx_does_not_remove_va_entries():
    train,test,va,adx = fixture()
    test['macro_4h_adx'] = np.nan
    result, control = allocate(train,test,va,adx)
    assert len(result) == 2 and control['adx_missing_test_entries'] == 2
    np.testing.assert_array_equal(result.multiplier,1.)
    assert result.sized_risk.gt(0).all()
