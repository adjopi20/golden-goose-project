import unittest
import numpy as np
from observe_open_poc_touch import candidate, base


class TouchTests(unittest.TestCase):
    def test_four_opening_locations(self):
        for prices, side, edge in (([80, 85, 90], 1, 'val'), ([95, 92, 90], 1, 'val'),
                                   ([120, 115, 110], -1, 'vah'), ([105, 108, 110], -1, 'vah')):
            self.assertEqual(candidate(np.array(prices), 100, 90, 110), (2, side, edge, 90 if side == 1 else 110))

    def test_open_exact_edge(self):
        self.assertEqual(candidate(np.array([90, 95]), 100, 90, 110)[0], 0)

    def test_at_poc_and_no_touch(self):
        self.assertIsNone(candidate(np.array([100, 90]), 100, 90, 110))
        self.assertIsNone(candidate(np.array([95, 100, 110]), 100, 90, 110))

    def test_bias_does_not_switch_after_poc(self):
        self.assertEqual(candidate(np.array([95, 110, 105, 90]), 100, 90, 110), (3, 1, 'val', 90))

    def test_crossing_not_exact_price(self):
        self.assertEqual(candidate(np.array([80, 89, 91]), 100, 90, 110)[0], 2)

    def test_jump_over_poc_invalidates_first_fill(self):
        prices = np.array([80, 105, 90, 100])
        i, side, _, _ = candidate(prices, 100, 90, 110)
        self.assertEqual(base.outcome(prices[i:], 100, -side, 'endpoint')['status'], 'poc_wrong_side')

    def test_equal_risk_tp_sl_time(self):
        for prices, side, expected in (([90, 100], 1, 'tp_first'), ([90, 80], 1, 'sl_first'),
                                       ([110, 100], -1, 'tp_first'), ([110, 120], -1, 'sl_first'),
                                       ([90, 95], 1, 'time_exit')):
            result = base.outcome(np.array(prices), 100, -side, 'endpoint')
            self.assertEqual(result['status'], expected)
            self.assertEqual(abs(result['sl']-result['entry']), result['risk_price'])


if __name__ == '__main__':
    unittest.main()
