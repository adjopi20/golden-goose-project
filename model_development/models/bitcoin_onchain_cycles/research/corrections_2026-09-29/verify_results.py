"""Reconcile repaired ledgers and compare preserved old results; no downloads."""
from pathlib import Path
import json
import runpy

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent
MODEL = OUT.parents[1]


def main():
    checks = []
    for family in ('accumulation', 'ladder', 'oos_accumulation', 'cvdd_entry'):
        frame = pd.read_csv(OUT / family / 'daily_ledgers.csv.gz', low_memory=False)
        for name, ledger in frame.groupby('strategy', sort=False):
            ledger = ledger.sort_values('date').reset_index(drop=True)
            assert not ledger.date.duplicated().any()
            assert np.isfinite(ledger[['cash', 'btc_units', 'equity', 'asset_price']]).all().all()
            assert ledger.cash.min() > -1e-7 and ledger.btc_units.min() > -1e-7
            equity_error = (ledger.equity-ledger.cash-ledger.btc_units*ledger.asset_price).abs().max()
            # Opening stocks are not trades; validate subsequent daily cash conservation.
            cash_error = (ledger.cash.diff()+ledger.btc_units.diff()*ledger.asset_price-ledger.net_flow).iloc[1:].abs().max()
            assert equity_error < 1e-6 and cash_error < 1e-6
            checks.append(dict(family=family, strategy=name,
                               equity_error=float(equity_error), cash_error=float(cash_error)))
    differences = []
    for new, old in [('accumulation','accumulation_results'), ('ladder','ladder_results')]:
        a=pd.read_csv(OUT/new/'performance_report.csv').set_index('strategy')
        b=pd.read_csv(MODEL/'research/portfolio_allocation'/old/'performance_report.csv').set_index('strategy')
        for name in a.index:
            delta=float(a.loc[name,'ending_portfolio_value']-b.loc[name,'ending_portfolio_value'])
            if abs(delta)>.01:
                differences.append(dict(family=new,strategy=name,
                    old_wealth=float(b.loc[name,'ending_portfolio_value']),
                    new_wealth=float(a.loc[name,'ending_portfolio_value']),change=delta))
    f=pd.read_csv(OUT/'ladder/daily_ledgers.csv.gz')
    for name,floor in [('ladder_share_f60_b35',.60),('ladder_share_f40_b35',.40)]:
        l=f.loc[f.strategy.eq(name)]
        assert (l.purchase_notional.cumsum() <= (1-floor)*l.net_flow.cumsum()+1e-7).all()

    calc=runpy.run_path(str(MODEL/'research/audit_2026-09-26/z_forward_information.py'))
    daily=pd.read_csv(MODEL/'data/prepared/paper_daily.csv')
    prices=daily.close.to_numpy()
    forward=[]
    for label,lo,hi in calc['BUCKETS']:
        indexes=np.flatnonzero((daily.mvrv_zscore.to_numpy()[:-365]>=lo)&(daily.mvrv_zscore.to_numpy()[:-365]<hi))
        dd=[calc['maximum_drawdown'](prices[i:i+366]) for i in indexes]
        ret=[prices[i+365]/prices[i]-1 for i in indexes]
        forward.append(dict(bucket=label,eligible_days=len(indexes),
                            median_return=float(np.median(ret)),median_max_drawdown=float(np.median(dd))))
    result=dict(ledger_checks=checks,changed_ending_values=differences,forward_365_days=forward)
    (OUT/'verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(ledgers_verified=len(checks),changed_ending_values=differences,forward_365_days=forward),indent=2))


if __name__=='__main__':
    main()
