import unittest
import contextlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import numpy as np
import pandas as pd
from model_development.phase_3.volatility_sizing import (daily_variance, forecasts, walk_forward_sizes,
                                            matched_risk, exposure, simulate, main)


def candles(start, end):
    ix=pd.date_range(start,end,freq='1min',tz='UTC')
    return pd.DataFrame(dict(timestamp_ms=ix.as_unit('ms').asi8,
                             close=100*np.exp(np.arange(len(ix))*0.000001),symbol='X'))


class VolSizingTests(unittest.TestCase):
    def test_completed_boundary_and_no_future_leakage(self):
        m=candles('2025-01-01','2025-01-07')
        a=daily_variance(m,'X')
        cutoff=pd.Timestamp('2025-01-04 14:00',tz='UTC').timestamp()*1000
        m.loc[m.timestamp_ms>=cutoff,'close']*=4
        b=daily_variance(m,'X')
        pd.testing.assert_frame_equal(a[a.snapshot_ms<=cutoff],b[b.snapshot_ms<=cutoff])
        good=a[a.complete]
        self.assertGreater(len(good),3)
        self.assertAlmostEqual(good.iloc[0].variance_24h,96*(15e-6)**2,places=12)

    def test_dst_day_has_92_or_100_returns(self):
        for start,end,day,count in [('2025-03-07','2025-03-12','2025-03-09',92),
                                    ('2025-10-31','2025-11-05','2025-11-02',100)]:
            row=daily_variance(candles(start,end),'X').set_index('session_day').loc[day]
            self.assertEqual(row.expected_bars,count)
            self.assertTrue(row.complete)
            self.assertAlmostEqual(row.variance_24h,96*(15e-6)**2,places=12)

    def test_missing_bar_and_conflicting_overlap(self):
        m=candles('2025-01-01','2025-01-07')
        stamp=pd.Timestamp('2025-01-03 01:00',tz='UTC').timestamp()*1000
        bad=m[~m.timestamp_ms.between(stamp,stamp+14*60000)]
        d=daily_variance(bad,'X')
        self.assertFalse(d.set_index('session_day').loc['2025-01-03','complete'])
        other=m.iloc[:1].copy();other['close']*=2
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            daily_variance(pd.concat([m,other]),'X')

    def test_ewma_warmup_and_reset(self):
        d=pd.DataFrame([dict(session_day=str(i),snapshot_ms=i,complete=x is not None,
                             variance_24h=x) for i,x in enumerate([.01,.01,.01,None,.04,.04,.04])])
        f=forecasts(d,20,3)
        self.assertIsNone(f['1']['sigma_24h'])
        self.assertAlmostEqual(f['2']['sigma_24h'],.1)
        self.assertIsNone(f['5']['sigma_24h'])
        self.assertAlmostEqual(f['6']['sigma_24h'],.2)

    def fixture(self):
        dates=pd.date_range('2025-01-01','2025-05-31',freq='3D',tz='America/New_York')
        rows=[dict(session_day=str(t.date()),entry_timestamp_ms=int((t+pd.Timedelta(hours=10)).timestamp()*1000),
                    exit_timestamp_ms=int((t+pd.Timedelta(hours=20)).timestamp()*1000),
                    sample_id=str(i),entry_price=100.) for i,t in enumerate(dates)]
        d=dict(rows=rows,start='2025-01-01',end='2025-05-31',distance=np.full(len(rows),.01),
               r=np.where(np.arange(len(rows))%2,2.,-1.),fee_r=np.full(len(rows),.04))
        f=[dict(sigma_24h=.01 if i%2 else .04) for i in range(len(rows))]
        return d,f

    def test_monthly_training_causal_and_capped(self):
        d,f=self.fixture()
        sized,folds=walk_forward_sizes(d,f,.005,5,30,5)
        self.assertTrue(sized)
        for fold in folds:
            self.assertLess(fold['latest_train_exit_ms'],fold['calibration_cutoff_ms'])
        for s in sized:
            self.assertLessEqual(s['weights']['volatility_target'],s['weights']['fixed_base'])
        # Future volatility and all outcomes cannot change first-month calibration.
        f2=[dict(x) for x in f]
        for i,row in enumerate(d['rows']):
            if row['session_day']>='2025-02-01':f2[i]['sigma_24h']=.8
        d['r']*= -100
        _,fold2=walk_forward_sizes(d,f2,.005,5,30,5)
        self.assertEqual(folds[0],fold2[0])

    def test_matched_risk_and_compounding(self):
        ds=np.array([.001,.01,.02])
        f=matched_risk(ds,.003,.005,5)
        self.assertAlmostEqual(np.mean(exposure(ds,f,5)*ds),.003,places=12)
        d,features=self.fixture()
        sized,_=walk_forward_sizes(d,features,.005,5,30,5)
        stat,ledger,_=simulate(d,sized,'fixed_base',1000)
        expected=1000*np.prod([1+.005*d['r'][s['index']] for s in sized])
        self.assertAlmostEqual(stat['final_equity'],expected)
        self.assertAlmostEqual(ledger[1]['equity_before'],ledger[0]['equity_after'])

    def test_missing_feature_keeps_trade_at_baseline(self):
        d,f=self.fixture();f[-1]['sigma_24h']=None
        sized,_=walk_forward_sizes(d,f,.005,5,30,5)
        last=sized[-1]
        self.assertTrue(last['fallback'])
        self.assertEqual(len(set(last['weights'].values())),1)

    def test_cli_synthetic_end_to_end_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            candles('2024-12-01','2025-06-02').to_parquet(root/'minutes.parquet',index=False)
            d,_=self.fixture()
            for i,r in enumerate(d['rows']):
                r.update(quantity=1,stop=99,actual_initial_risk=1,equity_before_entry=1000,
                         pnl=float(d['r'][i]),fees=.04,r=float(d['r'][i]))
            (root/'ledger.jsonl').write_text('\n'.join(json.dumps(r) for r in d['rows']))
            config=dict(schema_version=1,initial_equity=1000,risk_cap=.005,max_leverage=5,
                        volatility_sizing=dict(half_lives_days=[1],warmup_days=3,min_train_days=30,
                                               min_train_trades=5,cache_dir='cache'),
                        datasets=[dict(name='X',ledger='ledger.jsonl',start_date=d['start'],end_date=d['end'],
                                       candle_caches=['minutes.parquet'])])
            (root/'config.json').write_text(json.dumps(config))
            for name in ('run1','run2'):
                with patch('sys.argv',['test','--config',str(root/'config.json'),'--output-dir',str(root/name)]):
                    with contextlib.redirect_stdout(io.StringIO()):main()
                self.assertTrue((root/name/'complete.json').is_file())
            one=pd.read_csv(root/'run1/summary.csv');two=pd.read_csv(root/'run2/summary.csv')
            pd.testing.assert_frame_equal(one,two)
            self.assertEqual(len(one),3)
            self.assertEqual(one.net_wr.nunique(),1)
            self.assertEqual(one.expectancy_net_r.nunique(),1)
            self.assertEqual(one.trades.nunique(),1)


if __name__=='__main__':unittest.main()
