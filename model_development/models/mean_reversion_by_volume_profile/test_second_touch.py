import unittest
import numpy as np
import observe_second_touch as m


def tape():
    ts = np.arange(61, dtype=np.int64)*60000
    prices = np.full(61, 108.)
    prices[0], prices[1], prices[5], prices[6], prices[29], prices[30] = 100, 110, 105, 110, 105, 110
    return ts, prices


class SecondTouchTests(unittest.TestCase):
    def test_wick_entry_immediate_retouch(self):
        ts, p = tape()
        r = m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half')
        self.assertEqual((r['pullback_index'], r['entry_index']), (5, 6))

    def test_close_waits_for_15m_completion(self):
        ts, p = tape()
        r = m.setup(ts, p, 1, 'vah', 110, 100, 0, 'close_half')
        self.assertEqual((r['pullback_index'], r['pullback_available_ms'], r['entry_index']), (29, 1800000, 30))

    def test_long_symmetry(self):
        ts, p = tape()
        for mode, expected in (('wick_half', 6), ('close_half', 30)):
            r = m.setup(ts, 200-p, 1, 'val', 90, 100, 0, mode)
            self.assertEqual(r['entry_index'], expected)

    def test_shallow_repeated_touches_do_not_qualify(self):
        ts, p = tape()
        p[2], p[3] = 110, 109
        self.assertEqual(m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half')['entry_index'], 6)

    def test_no_half_pullback(self):
        ts, p = tape()
        p[5], p[29] = 106, 106
        for mode in m.MODES:
            self.assertEqual(m.setup(ts, p, 1, 'vah', 110, 100, 0, mode)['status'], 'no_half_pullback')

    def test_no_retouch(self):
        ts, p = tape()
        p[6], p[30] = 109, 109
        for mode in m.MODES:
            self.assertEqual(m.setup(ts, p, 1, 'vah', 110, 100, 0, mode)['status'], 'no_second_touch')

    def test_first_touch_interval_can_qualify_after_close(self):
        ts, p = tape()
        p[14], p[15] = 105, 110
        self.assertEqual(m.setup(ts, p, 1, 'vah', 110, 100, 0, 'close_half')['entry_index'], 15)

    def test_same_timestamp_later_id_allowed_for_wick(self):
        ts, p = tape()
        ts[6] = ts[5]
        r = m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half')
        self.assertEqual(r['entry_index'], 6)

    def test_crossing_uses_observed_print(self):
        ts, p = tape()
        p[6] = 111
        self.assertEqual(m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half')['entry_index'], 6)

    def test_past_poc_does_not_cancel_setup(self):
        ts, p = tape()
        p[5] = 99
        self.assertEqual(m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half')['entry_index'], 6)

    def test_future_does_not_change_selected_entry(self):
        ts, p = tape()
        r = m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half')
        p[7:] = 10000
        self.assertEqual(r, m.setup(ts, p, 1, 'vah', 110, 100, 0, 'wick_half'))


if __name__ == '__main__':
    unittest.main()
