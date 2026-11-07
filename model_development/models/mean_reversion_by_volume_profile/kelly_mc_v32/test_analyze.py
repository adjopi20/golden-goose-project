import unittest
import numpy as np
import pandas as pd
from analyze import kelly, log_growth, indices, monte_carlo, ny_dates


class KellyTests(unittest.TestCase):
    def test_equity_timestamps_mixed_fractional_seconds_and_dst(self):
        values = pd.Series(['2024-04-11T09:30:00-04:00', '2024-04-17T10:19:05.308000-04:00',
                            '2024-12-01T10:00:00-05:00'])
        parsed = ny_dates(values)
        self.assertEqual(parsed.iloc[1], pd.Timestamp('2024-04-17T10:19:05.308000'))
        self.assertEqual(parsed.iloc[2], pd.Timestamp('2024-12-01T10:00:00'))

    def test_binary_analytic_optimum(self):
        r = np.r_[np.ones(60), -np.ones(40)]
        fit = kelly(r, np.full(100, 10.), np.ones(100))
        self.assertAlmostEqual(fit['target_fraction'], .2, places=10)

    def test_no_positive_empirical_log_growth_selects_zero(self):
        r = np.r_[np.ones(40), -np.ones(60)]
        self.assertEqual(kelly(r, np.full(100, 10.), np.ones(100))['target_fraction'], 0.)

    def test_global_cap_search_including_nonconcave_regions(self):
        rng = np.random.default_rng(42)
        r = rng.choice([1., -.8, -1.1], 100)
        cap = rng.uniform(.01, .5, 100)
        boost = rng.choice([1., 2.], 100)
        fit = kelly(r, cap, boost)
        grid = np.linspace(0, fit['search_upper'], 20001)
        scores = [log_growth(f, r, cap, boost) for f in grid]
        self.assertGreaterEqual(fit['mean_log_growth']+1e-10, max(scores))
        self.assertTrue((np.minimum(fit['target_fraction']*boost, cap) <= cap).all())

    def test_constant_multiplier_reparameterizes_same_allocation(self):
        r = np.r_[np.ones(60), -np.ones(40)]
        cap = np.linspace(.03, .5, 100)
        a = kelly(r, cap, np.ones(100))
        b = kelly(r, cap, np.full(100, 2.))
        self.assertAlmostEqual(a['target_fraction'], 2*b['target_fraction'], places=10)

    def test_bootstrap_seed_and_zero_growth(self):
        a = indices(100, 90, 30, 42)
        np.testing.assert_array_equal(a, indices(100, 90, 30, 42))
        self.assertTrue(((a >= 0) & (a < 90)).all())
        metrics, band = monte_carlo(np.zeros(90), a)
        self.assertEqual(metrics['return_p95_pct'], 0.)
        self.assertEqual(metrics['max_dd_p95_pct'], 0.)
        np.testing.assert_array_equal(band, 10_000.)

    def test_compounding_known_path_and_permutation(self):
        returns = np.array([.1, -.1])
        metrics, band = monte_carlo(returns, np.array([[0, 1], [1, 0]], dtype=np.int32))
        self.assertAlmostEqual(metrics['return_p50_pct'], -1.)
        self.assertAlmostEqual(band[-1, 1], 9900.)
        self.assertAlmostEqual(metrics['max_dd_p50_pct'], 10.)

    def test_fifty_trade_minimum_and_bankruptcy_bound(self):
        with self.assertRaises(ValueError):
            kelly(np.ones(49), np.ones(49), np.ones(49))
        r = np.r_[np.ones(80), np.full(20, -2.)]
        fit = kelly(r, np.ones(100), np.ones(100))
        self.assertLess(fit['search_upper'], .5)
        self.assertTrue(np.isfinite(fit['mean_log_growth']))


if __name__ == '__main__':
    unittest.main()
