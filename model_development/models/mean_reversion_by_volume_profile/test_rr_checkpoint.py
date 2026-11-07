import unittest
import numpy as np
from observe_rr_checkpoint import geometry, replay, base


class RRTests(unittest.TestCase):
    def test_long_risk_and_target(self):
        for rr in (1., 1.2, 1.5, 1.8, 2.):
            reward, risk, sl = geometry(100., 110., 1, rr)
            self.assertEqual(reward, 10)
            self.assertAlmostEqual(risk, 10/rr)
            self.assertAlmostEqual(sl, 100-10/rr)

    def test_short_symmetry(self):
        reward, risk, sl = geometry(100., 90., -1, 2.)
        self.assertEqual((reward, risk, sl), (10., 5., 105.))

    def test_invalid_geometry(self):
        for params in ((100, 90, 1, 1.2), (100, 110, 1, 0), (100, 110, 1, float('nan'))):
            with self.assertRaises(ValueError):
                geometry(*params)

    def test_tighter_stop_replays_instead_of_scaling_old_profit(self):
        ts = np.arange(4, dtype=np.int64)*1000
        prices = np.array([100., 94., 101., 110.])
        arrays = [ts, prices, np.ones(4), np.arange(4, dtype=np.int64)]
        event = dict(session_date='2025-01-01', entry=100., poc=110., side=1,
            entry_agg_trade_id=0, entry_timestamp_ny=base.iso(0), exit_agg_trade_id=3,
            status='tp_first', gross_r=1., net_r=1-.0004*210/10)
        rows = replay(event, arrays, dict(fee_bps_per_side=4))
        self.assertEqual(rows[0]['status'], 'tp_first')
        self.assertEqual(rows[-1]['status'], 'sl_first')
        self.assertAlmostEqual(rows[-1]['gross_r'], -1.2)

    def test_positive_target_exit_same_price_but_new_r_units(self):
        ts = np.array([0, 1000], dtype=np.int64)
        arrays = [ts, np.array([100., 110.]), np.ones(2), np.array([1, 2], dtype=np.int64)]
        event = dict(session_date='2025-01-01', entry=100., poc=110., side=1,
            entry_agg_trade_id=1, entry_timestamp_ny=base.iso(0), exit_agg_trade_id=2,
            status='tp_first', gross_r=1., net_r=1-.0004*210/10)
        rows = replay(event, arrays, dict(fee_bps_per_side=4))
        self.assertAlmostEqual(rows[-1]['gross_r'], 2.)
        self.assertAlmostEqual(rows[-1]['net_r'], 2-.0004*210/5)
        self.assertAlmostEqual(rows[0]['gross_r']*rows[0]['risk_price']/100,
            rows[-1]['gross_r']*rows[-1]['risk_price']/100)

    def test_short_first_stop_before_later_target(self):
        from observe_profile_end_stop import scan
        self.assertEqual(scan(np.array([100., 106., 90.]), -1, 90., 105., 5.), ('sl_first', 1, -1.))


if __name__ == '__main__':
    unittest.main()
