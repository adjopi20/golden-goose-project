import unittest
import numpy as np
import pandas as pd
from observe_conditional_sizing import simulate, condition

CFG = dict(account_risk_fraction=.005, max_entry_notional_equity=5)


def trades():
    return pd.DataFrame(dict(entry_timestamp_ny=['2025-01-01', '2025-01-02'],
        risk_price=[1., 1.], entry=[100., 100.], net_r=[1., -1.], gross_r=[1.08, -.92],
        location_selected=[True, True], atr_high=[True, False]))


class SizingTests(unittest.TestCase):
    def test_baseline_compounding(self):
        d = trades()
        ledger, r = simulate(d, condition(d, 'none'), 1., CFG)
        self.assertAlmostEqual(r['net_account_return_pct'], 100*(1.005*.995-1))
        self.assertAlmostEqual(r['max_closed_dd_pct'], .5)

    def test_boost_changes_risk_not_trade_outcome(self):
        d = trades()
        ledger, r = simulate(d, condition(d, 'atr_high'), 2., CFG)
        self.assertEqual(ledger.actual_risk_fraction.tolist(), [.01, .005])
        self.assertEqual(ledger.net_r.tolist(), [1., -1.])
        self.assertAlmostEqual(r['net_account_return_pct'], 100*(1.01*.995-1))

    def test_cap_preserved_and_actual_increase_can_be_zero(self):
        d = trades()
        d['risk_price'] = .01
        ledger, r = simulate(d, condition(d, 'location'), 2., CFG)
        np.testing.assert_allclose(ledger.notional_equity, 5.)
        self.assertEqual(r['actual_increased_trades'], 0)

    def test_intersection_location_equals_all(self):
        d = trades()
        self.assertTrue(condition(d, 'location').all())
        pd.testing.assert_series_equal(condition(d, 'location_and_atr_high'), condition(d, 'atr_high'), check_names=False)

    def test_unsorted_source_aligns_condition(self):
        d = trades().iloc[::-1]
        ledger, _ = simulate(d, condition(d, 'atr_high'), 2., CFG)
        self.assertEqual(ledger.actual_risk_fraction.tolist(), [.01, .005])

    def test_empty_fold_returns_zero(self):
        d = trades().iloc[:0]
        _, r = simulate(d, condition(d, 'none'), 1., CFG)
        self.assertEqual(r['trades'], 0)
        self.assertEqual(r['net_account_return_pct'], 0)


if __name__ == '__main__':
    unittest.main()
