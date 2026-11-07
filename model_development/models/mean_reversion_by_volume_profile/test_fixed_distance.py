import unittest
import pandas as pd
from observe_fixed_distance import eligible, THRESHOLDS


class FixedDistanceTests(unittest.TestCase):
    def test_user_thresholds(self):
        self.assertEqual(sorted(THRESHOLDS.values()), [0., .3, .5, .7, 1., 2.])

    def test_inclusive_boundary(self):
        self.assertEqual(eligible(pd.Series([.2999, .3, .3001]), .3).tolist(), [False, True, True])

    def test_nested_filters(self):
        values = pd.Series([.1, .3, .5, .7, 1., 2., 3.])
        previous = set(values.index)
        for threshold in sorted(THRESHOLDS.values()):
            current = set(values.index[eligible(values, threshold)])
            self.assertTrue(current <= previous)
            previous = current

    def test_missing_distance_not_entry(self):
        self.assertFalse(eligible(pd.Series([float('nan')]), .3).iloc[0])


if __name__ == '__main__':
    unittest.main()
