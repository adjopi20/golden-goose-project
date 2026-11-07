import unittest
import numpy as np
import pandas as pd
import observe_delta_divergence as m


def tape(sell=False, falling=False):
    ts = np.arange(61, dtype=np.int64)*60000
    prices = 100+np.arange(61, dtype=float)*(-1 if falling else 1)
    qty = np.r_[np.full(15, 3.), np.full(15, 2.), np.ones(31)]
    return [ts, prices, qty, np.arange(61, dtype=np.int64), np.full(61, sell, dtype=bool)]


class DeltaTests(unittest.TestCase):
    def test_positive_but_declining_delta_short(self):
        r = m.features(tape(), 45, 0, 'vah')
        self.assertTrue(r['delta15_signal'])
        self.assertEqual([r['delta15_'+str(k)] for k in range(3)], [45, 30, 15])
        self.assertFalse(r['cvd_signal'])

    def test_negative_but_weakening_sells_long(self):
        r = m.features(tape(True, True), 45, 0, 'val')
        self.assertTrue(r['delta15_signal'])
        self.assertFalse(r['cvd_signal'])

    def test_cvd_opposite_price_both_sides(self):
        self.assertTrue(m.features(tape(True), 45, 0, 'vah')['cvd_signal'])
        self.assertTrue(m.features(tape(False, True), 45, 0, 'val')['cvd_signal'])

    def test_unavailable_before_45_minutes(self):
        r = m.features(tape(), 44, 0, 'vah')
        self.assertFalse(r['delta15_observable'])
        self.assertFalse(r['delta15_signal'])

    def test_entry_and_future_prints_excluded(self):
        a = tape()
        before = m.features(a, 45, 0, 'vah')
        a[2][45:] = 1e10
        a[4][45:] = True
        a[1][45:] = -1e10
        self.assertEqual(before, m.features(a, 45, 0, 'vah'))

    def test_last_three_completed_intervals(self):
        r = m.features(tape(), 59, 0, 'vah')
        self.assertEqual(r['delta15_0'], 45)
        self.assertEqual(r['delta15_2'], 15)
        r = m.features(tape(), 60, 0, 'vah')
        self.assertEqual(r['delta15_0'], 30)
        self.assertEqual(r['delta15_2'], 15)
        self.assertFalse(r['delta15_signal'])  # Last two deltas equal.

    def test_price_must_advance_toward_edge(self):
        self.assertFalse(m.features(tape(False, True), 45, 0, 'vah')['delta15_signal'])

    def test_delta_may_cross_zero(self):
        a = tape()
        a[4][30:45] = True
        self.assertTrue(m.features(a, 45, 0, 'vah')['delta15_signal'])

    def test_missing_interval_unavailable(self):
        a = tape()
        a = [x[np.r_[np.arange(15), np.arange(30, 61)]] for x in a]
        r = m.features(a, 30, 0, 'vah')
        self.assertFalse(r['delta15_observable'])
        self.assertEqual(r['delta15_status'], 'missing_interval_prints')

    def test_entry_modes_can_choose_different_edges(self):
        p = np.array([95., 110., 90.])
        source = dict(poc=100., val=90., vah=110.)
        self.assertEqual(m.candidate(p, source, 'first_edge')[:3], (1, -1, 'vah'))
        self.assertEqual(m.candidate(p, source, 'open_poc_bias')[:3], (2, 1, 'val'))

    def test_at_poc_only_bias_mode_skips(self):
        p = np.array([100., 110.])
        source = dict(poc=100., val=90., vah=110.)
        self.assertIsNone(m.candidate(p, source, 'open_poc_bias'))
        self.assertEqual(m.candidate(p, source, 'first_edge')[0], 1)

    def test_matched_partitions(self):
        frame = pd.DataFrame(dict(cvd_observable=[True, True, False], cvd_signal=[True, False, False]))
        self.assertEqual(m.select(frame, 'cvd_matched').index.tolist(), [0, 1])
        self.assertEqual(m.select(frame, 'cvd_divergence').index.tolist(), [0])
        self.assertEqual(m.select(frame, 'cvd_no_divergence').index.tolist(), [1])


if __name__ == '__main__':
    unittest.main()
