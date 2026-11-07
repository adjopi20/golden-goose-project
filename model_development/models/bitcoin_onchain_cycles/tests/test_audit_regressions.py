from argparse import Namespace
from pathlib import Path
import runpy
from unittest.mock import patch

import pandas as pd
import pytest

from model_development.phase_3.portfolio_report import calculate_portfolio_metrics
from model_development.models.bitcoin_onchain_cycles.allocation import simulate_accumulation_dca, simulate_ladder_accumulation
from model_development.models.bitcoin_onchain_cycles.cost_basis import cost_basis_ledger
from model_development.models.bitcoin_onchain_cycles.data_sources import prepared_window
from model_development.models.bitcoin_onchain_cycles.run_oos_accumulation import _load_daily
from model_development.models.bitcoin_onchain_cycles.run_daily_monitor import account_proposal
from model_development.models.bitcoin_onchain_cycles.strategy import prepare_daily_data

MODEL = Path(__file__).resolve().parents[1]


def history(dates, prices, z):
    return pd.DataFrame({'date': pd.to_datetime(dates), 'close': prices, 'mvrv_zscore': z})


def test_forward_drawdown_preserves_peak_trough_order():
    calc = runpy.run_path(str(MODEL / 'research/audit_2026-09-26/z_forward_information.py'))['maximum_drawdown']
    assert calc([100, 150, 200]) == 0
    assert calc([200, 100, 300]) == -.5
    assert calc([100, 80, 200, 180]) == pytest.approx(-.2)


def test_expiry_cannot_consume_a_new_reserve_as_an_old_lot():
    d = history(['2020-01-30','2020-01-31','2020-02-01'], [100]*3, [3]*3)
    l = simulate_accumulation_dca(d, initial_capital=100, monthly_contribution=100,
                                 base_share=.5, powder_expiry_days=2)
    assert l.cash.tolist() == [50,50,50]
    assert l.purchase_notional.tolist() == [50,0,100]
    assert l.powder_expired_notional.tolist() == [0,0,50]


def test_expiry_waits_during_no_buy_zone():
    d = history(pd.date_range('2020-01-30', periods=4), [100]*4, [3,3,6,3])
    l = simulate_accumulation_dca(d, initial_capital=100, monthly_contribution=100,
                                 base_share=.5, powder_expiry_days=2)
    assert l.purchase_notional.tolist() == [50,0,0,50]
    assert l.cash.iloc[-1] == 100


def test_tier_spending_reduces_future_expiry():
    d = history(pd.date_range('2020-01-30', periods=3), [100]*3, [3,1,3])
    l = simulate_accumulation_dca(d, initial_capital=100, monthly_contribution=100,
                                 base_share=0, tiers=((2,.5),), powder_expiry_days=2)
    assert l.powder_expired_notional.iloc[-1] == 50
    assert l.cash.iloc[-1] == 100


def test_ladder_base_and_rung_share_one_spending_allowance():
    d = history(['2020-01-31','2020-02-01'], [100,80], [3,3])
    l = simulate_ladder_accumulation(d, initial_capital=100, monthly_contribution=100,
                                   base_share=.9, reserve_floor=.1, rungs=((-0.2,1.),))
    assert l.cash.tolist() == [10,20]
    assert (l.purchase_notional.cumsum() <= .9*l.net_flow.cumsum()+1e-8).all()


def test_cached_oos_respects_end(tmp_path):
    d = history(pd.date_range('2013-12-07',periods=4),[100]*4,[1]*4)
    p = tmp_path/'daily.csv'
    d.to_csv(p,index=False)
    got,_ = _load_daily(Namespace(no_refresh=True,prepared=p,end='2013-12-08'))
    assert got.date.iloc[-1] == pd.Timestamp('2013-12-08')
    assert len(got)==2


def test_cached_history_rejects_an_internal_gap():
    d=history(['2020-01-01','2020-01-03'],[100,100],[1,1])
    with pytest.raises(ValueError,match='calendar'):
        prepared_window(d,'2020-01-01','2020-01-03')


def test_opening_basis_is_not_a_trade():
    l=pd.DataFrame({'date':pd.date_range('2020-01-01',periods=2),
        'asset_price':[100,200], 'equity':[100,300], 'cash':[0,0],
        'btc_units':[1,1.5], 'net_flow':[100,100], 'btc_bought':[0,.5],
        'purchase_notional':[0,100], 'traded_notional':[0,100],
        'opening_btc_units':[1,0], 'opening_cost_basis_usd':[100,0]})
    c=cost_basis_ledger(l)
    assert c.average_cost_price.tolist()==pytest.approx([100,200/1.5])
    m=calculate_portfolio_metrics(l)
    assert m['average_btc_acquisition_price']==pytest.approx(200/1.5)
    assert m['contributions']==200
    assert m['turnover']==.5


def test_missing_cdd_is_not_zero_and_warmup_is_required():
    b=pd.DataFrame({'time':['2010-07-18','2010-07-19'], 'PriceUSD':[1,2],
                    'CapMrktCurUSD':[100,200], 'CapMVRVCur':[2,2]})
    c=pd.DataFrame({'date':['18.07.2010'],'cdd':[1]})
    with patch('model_development.models.bitcoin_onchain_cycles.strategy.pd.read_csv',side_effect=[b,c]):
        with pytest.raises(ValueError,match='CDD source has gaps'):
            prepare_daily_data('btc','cdd',start='2010-07-19',end='2010-07-19')
    with patch('model_development.models.bitcoin_onchain_cycles.strategy.pd.read_csv',return_value=b.iloc[1:]):
        with pytest.raises(ValueError,match='warmup'):
            prepare_daily_data('btc',start='2010-07-19',end='2010-07-19')


def test_new_account_does_not_inherit_reference_tier_spending():
    d=history(['2026-01-01','2026-01-02'],[100,100],[-.1,-.3])
    account={'strategy':'frozen_accumulation','as_of':'2026-01-01',
             'cash_usd':1000,'btc_units':0,'unallocated_inflow_usd':0,
             'tiers_spent_this_cycle':[]}
    p=account_proposal(d,account)
    assert p['proposed_buy_usd']==1000
    assert p['proposed_tiers']==[1,2,3]
    assert account['tiers_spent_this_cycle']==[]  # no assumed fills
    account['tiers_spent_this_cycle']=[1,2,3]
    assert account_proposal(d,account)['signal']=='WAIT'


def test_actual_account_rearms_only_from_available_subsequent_history():
    d=history(['2026-01-01','2026-01-02','2026-01-03'],[100]*3,[1,5,.4])
    account={'strategy':'frozen_accumulation','as_of':'2026-01-01',
             'cash_usd':1000,'btc_units':1,'unallocated_inflow_usd':0,
             'tiers_spent_this_cycle':[1,2,3]}
    assert account_proposal(d,account)['proposed_buy_usd']==500
    account['cash_usd']=-1
    with pytest.raises(ValueError,match='balances'):
        account_proposal(d,account)


def test_leading_treasury_days_do_not_borrow_a_future_rate():
    from model_development.models.bitcoin_onchain_cycles.run_ladder_yield_experiment import _treasury_rates
    series = pd.Series([.05], index=pd.to_datetime(['2020-01-03']))
    with patch('model_development.models.bitcoin_onchain_cycles.run_ladder_yield_experiment.load_treasury_yield',return_value=series):
        got = _treasury_rates(Path('unused'),pd.Timestamp('2020-01-01'),pd.Timestamp('2020-01-03'),offline=True)
    assert got.tolist()==[0,0,.05]
