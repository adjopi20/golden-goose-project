"""Checks for subset-dependent uncertainty and percentage/equity interpretation."""
import unittest
import numpy as np
import pandas as pd
from compare_adx_quality import distribution, equity, uncertainty


class AdxQualityTests(unittest.TestCase):
    def test_drawdown_starts_with_initial_capital_and_modes_preserve_ties(self):
        d=distribution([-1.,1.,-1.,1.])
        self.assertEqual(d["max_drawdown"],1.)
        self.assertEqual(d["mode"],"[-1.0, 1.0]")
        self.assertEqual(d["mode_count"],2)
        self.assertEqual(distribution([.1,.2,.3])["mode"],"[]")
        e=equity([.01,-.01])
        self.assertAlmostEqual(e["compounded_return_pct"],-.01)
        self.assertAlmostEqual(e["max_drawdown_pct"],1.)

    def test_random_control_and_bootstrap_preserve_percentage_scale(self):
        selected=np.arange(40)%2==0
        r=np.where(selected,1.,-1.)
        base=pd.DataFrame(dict(session_date=pd.date_range("2024-01-01",periods=40).strftime("%Y-%m-%d"),
            fold=1,selected=selected,gross_r=r,position_return_pct=r*.5))
        schedule=pd.DataFrame([dict(fold=1,test_start="2024-01-01",test_end_exclusive="2024-02-10")])
        result=uncertainty(base,schedule,np.random.default_rng(1),2000)
        self.assertLess(result["random_mean_r_tail_fraction"],.01)
        self.assertGreater(result["bootstrap_mean_r_kept_minus_base_ci025"],0)
        self.assertAlmostEqual(result["random_mean_pct_p025"],.5*result["random_mean_r_p025"])
        self.assertAlmostEqual(result["bootstrap_mean_pct_kept_minus_base_ci025"],.5*result["bootstrap_mean_r_kept_minus_base_ci025"])
        self.assertAlmostEqual(result["bootstrap_mean_r_kept_minus_discarded_ci025"],2.)

    def test_full_retention_null_diagnostic_is_exact(self):
        # One fold retains all its trades; another retains none. Fold-stratified
        # random selection has no freedom and must match the observed choice.
        base=pd.DataFrame(dict(session_date=["2024-01-01","2024-01-02","2024-02-01","2024-02-02"],
            fold=[1,1,2,2],selected=[True,True,False,False],gross_r=[1.,-.5,1.,-1.],position_return_pct=[.2,-.1,.2,-.2]))
        schedule=pd.DataFrame([dict(fold=1,test_start="2024-01-01",test_end_exclusive="2024-01-04"),
                               dict(fold=2,test_start="2024-02-01",test_end_exclusive="2024-02-04")])
        result=uncertainty(base,schedule,np.random.default_rng(5),100)
        self.assertEqual(result["random_mean_r_tail_fraction"],1.)
        self.assertEqual(result["random_mean_r_p025"],.25)
        self.assertEqual(result["random_mean_r_p975"],.25)


if __name__=="__main__":
    unittest.main()
