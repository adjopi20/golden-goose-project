import unittest
import numpy as np
from observe_profile_end_stop import stop_geometry, scan


class StopTests(unittest.TestCase):
    def test_long_profile_stop(self):
        r = stop_geometry(100, 110, 1, 96, 120)
        self.assertEqual((r['sl'], r['risk_price'], r['planned_rr']), (96, 4, 2.5))
        self.assertFalse(r['stop_capped_to_equal'])

    def test_short_profile_stop(self):
        r = stop_geometry(100, 90, -1, 80, 104)
        self.assertEqual((r['sl'], r['risk_price'], r['planned_rr']), (104, 4, 2.5))

    def test_cap_long_and_short(self):
        for poc, side, sl in ((110, 1, 90), (90, -1, 110)):
            r = stop_geometry(100, poc, side, 80, 120)
            self.assertEqual((r['sl'], r['planned_rr']), (sl, 1))
            self.assertTrue(r['stop_capped_to_equal'])

    def test_equal_risk_is_valid(self):
        r = stop_geometry(100, 110, 1, 90, 120)
        self.assertEqual(r['planned_rr'], 1)
        self.assertFalse(r['stop_capped_to_equal'])

    def test_nonpositive_geometry(self):
        for args in ((100, 110, 1, 100, 120), (100, 110, 1, 101, 120),
                     (100, 90, -1, 80, 100), (100, 90, 1, 80, 120)):
            self.assertIsNone(stop_geometry(*args))

    def test_tp_returns_multiple_r(self):
        self.assertEqual(scan(np.array([100, 102, 110]), 1, 110, 96, 4), ('tp_first', 2, 2.5))
        self.assertEqual(scan(np.array([100, 98, 90]), -1, 90, 104, 4), ('tp_first', 2, 2.5))

    def test_stop_wins_race_even_if_tp_later(self):
        self.assertEqual(scan(np.array([100, 96, 110]), 1, 110, 96, 4), ('sl_first', 1, -1))
        self.assertEqual(scan(np.array([100, 104, 90]), -1, 90, 104, 4), ('sl_first', 1, -1))

    def test_time_exit(self):
        self.assertEqual(scan(np.array([100, 102]), 1, 110, 96, 4), ('time_exit', 1, .5))

    def test_trigger_overshoot_keeps_barrier_r_separate(self):
        self.assertEqual(scan(np.array([100, 94]), 1, 110, 96, 4), ('sl_first', 1, -1))
        self.assertEqual((94-100)/4, -1.5)


if __name__ == '__main__':
    unittest.main()
