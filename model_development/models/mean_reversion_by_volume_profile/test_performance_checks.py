import unittest
import numpy as np
import pandas as pd
from performance_poc_rr import stats, account_stats


class PerformanceTests(unittest.TestCase):
    def test_initial_losses_and_partial_time_exits(self):
        s = stats([-1., -.5, 1., .25, -.25])
        self.assertAlmostEqual(s["total_r"], -.5)
        self.assertAlmostEqual(s["max_drawdown_r"], 1.5)
        self.assertEqual(s["max_consecutive_losses"], 2)
        self.assertEqual(s["win_rate_pct"], 40.)
        self.assertAlmostEqual(s["profit_factor"], 1.25 / 1.75)

    def test_risk_percent_compounds_and_cash_does_not(self):
        r = pd.Series([1., -1.])
        _, compounded = account_stats(r, 1000., 1., None)
        _, fixed = account_stats(r, 1000., None, 10.)
        self.assertAlmostEqual(compounded[-1], 999.9)
        self.assertAlmostEqual(fixed[-1], 1000.)

    def test_breakeven_breaks_losing_streak(self):
        s = stats([-1., 0., -1.])
        self.assertEqual(s["max_consecutive_losses"], 1)
        self.assertEqual(s["breakeven_closes"], 1)


if __name__ == "__main__":
    unittest.main()
