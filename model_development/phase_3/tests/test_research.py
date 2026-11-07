import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from model_development.phase_3.ledger_io import ledger_load, equal
from model_development.phase_3.monte_carlo import stressed_r, block_indices
from model_development.phase_3.kelly import empirical_kelly
from model_development.phase_3.monte_carlo import path_stats


class ResearchTests(unittest.TestCase):
    def test_kelly_known_binary(self):
        # 60% +1R, 40% -1R -> 20% full Kelly.
        r=np.array([1.]*6+[-1.]*4)
        self.assertAlmostEqual(empirical_kelly(r,np.ones((1,10)))[0],.2,places=8)

    def test_negative_edge_and_no_loss(self):
        self.assertEqual(empirical_kelly(np.array([-1.,.1]),np.ones((1,2)))[0],0.)
        self.assertTrue(np.isnan(empirical_kelly(np.array([1.,2.]),np.ones((1,2)))[0]))

    def test_stress_partial_remaining_and_cost_units(self):
        d=dict(r=np.array([1.,-1.]),fee_r=np.array([.1,.1]),distance=np.array([.01,.02]),stop_remaining=np.array([0.,.8]))
        np.testing.assert_allclose(stressed_r(d,{}),d['r'])
        np.testing.assert_allclose(stressed_r(d,dict(fee_multiplier=1.5)),[.95,-1.05])
        np.testing.assert_allclose(stressed_r(d,dict(extra_roundtrip_bps=2)),[.98,-1.01])
        np.testing.assert_allclose(stressed_r(d,dict(stop_extra_r=.25)),[1.,-1.2])
        np.testing.assert_allclose(stressed_r(d,dict(drop_top_winners=1)),[-.1,-1.])
        np.testing.assert_allclose(stressed_r(d,dict(winner_gross_multiplier=.75)),[.725,-1.])

    def test_insolvency_absorbing_and_start_peak(self):
        final,dd=path_stats(np.array([[-.1,.1],[-1.2,20.]]),1000)
        np.testing.assert_allclose(final,[990,0])
        np.testing.assert_allclose(dd,[.1,1.])

    def test_trade_shuffle_preserves_every_return_and_geometric_terminal_equity(self):
        from model_development.phase_3.monte_carlo import shuffle_trade_paths
        returns=np.array([-.2,.4,-.1,.2])
        paths=shuffle_trade_paths(returns,1000.,simulations=500,seed=7)
        self.assertEqual(paths.shape,(500,5))
        np.testing.assert_array_equal(paths[:,0],np.full(500,1000.))
        observed=paths[:,1:]/paths[:,:-1]-1
        np.testing.assert_allclose(np.sort(observed,axis=1),np.tile(np.sort(returns),(500,1)),atol=1e-14)
        np.testing.assert_allclose(paths[:,-1],1000*np.prod(1+returns),atol=1e-10)
        np.testing.assert_array_equal(paths,shuffle_trade_paths(returns,1000.,simulations=500,seed=7))
        self.assertFalse(np.array_equal(paths,shuffle_trade_paths(returns,1000.,simulations=500,seed=8)))
        drawdown=np.max(1-paths/np.maximum.accumulate(paths,axis=1),axis=1)
        self.assertGreater(float(drawdown.max()),float(drawdown.min()))
        self.assertGreaterEqual(float(drawdown.min()),.2-1e-12)

    def test_trade_shuffle_zero_equity_is_absorbing(self):
        from model_development.phase_3.monte_carlo import shuffle_trade_paths
        paths=shuffle_trade_paths(np.array([-1.2,3.,.1]),1000.,simulations=50,seed=3)
        np.testing.assert_array_equal(paths[:,-1],np.zeros(50))
        for path in paths:
            first_zero=int(np.flatnonzero(path==0)[0])
            np.testing.assert_array_equal(path[first_zero:],np.zeros(len(path)-first_zero))
        single=shuffle_trade_paths(np.array([.1]),1000.,simulations=3)
        np.testing.assert_allclose(single,[[1000.,1100.]]*3)

    def test_trade_shuffle_rejects_invalid_parameters(self):
        from model_development.phase_3.monte_carlo import shuffle_trade_paths
        for values in ([],[np.nan],[np.inf],[[.1]]):
            with self.subTest(values=values),self.assertRaises(ValueError):
                shuffle_trade_paths(values,1000.)
        for params in (dict(simulations=True),dict(simulations=0),dict(seed=-1),dict(seed=True)):
            with self.subTest(params=params),self.assertRaises(ValueError):
                shuffle_trade_paths([.1],1000.,**params)
        with self.assertRaises(ValueError):shuffle_trade_paths([.1],0.)

    def test_same_indices_and_numeric_selector(self):
        a=block_indices(30,7,10,np.random.default_rng(5))
        b=block_indices(30,7,10,np.random.default_rng(5))
        np.testing.assert_array_equal(a,b)
        self.assertEqual(a.shape,(10,30))
        self.assertTrue(equal('2.0',[1,2]));self.assertFalse(equal('3',[1,2]))

    def test_ledger_validation(self):
        row=dict(sample_id='a',session_day='2026-01-01',entry_timestamp_ms=1,exit_timestamp_ms=2,
            quantity=1,entry_price=100,stop=90,actual_initial_risk=10,pnl=9,fees=1,
            gross_pnl_before_costs=10,r=.9,equity_before_entry=1000)
        spec=dict(start_date='2026-01-01',end_date='2026-01-02')
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'trades.jsonl';p.write_text(json.dumps(row)+'\n')
            self.assertEqual(ledger_load(p,spec)['days'],2)
            p.write_text(json.dumps(row)+'\n'+json.dumps(row)+'\n')
            with self.assertRaises(ValueError):ledger_load(p,spec)
            row['r']=float('nan');p.write_text(json.dumps(row)+'\n')
            with self.assertRaises(ValueError):ledger_load(p,spec)


if __name__=='__main__':unittest.main()
