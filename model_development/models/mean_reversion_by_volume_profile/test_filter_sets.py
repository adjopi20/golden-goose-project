import unittest
import pandas as pd
from observe_filter_sets import masks


class SetTests(unittest.TestCase):
    def test_and_or_truth_table(self):
        a, b = pd.Series([False, False, True, True]), pd.Series([False, True, False, True])
        intersection, union = masks(a, b)
        self.assertEqual(intersection.tolist(), [False, False, False, True])
        self.assertEqual(union.tolist(), [False, True, True, True])

    def test_no_duplicate_overlap_in_union(self):
        a, b = pd.Series([True, True, False]), pd.Series([True, False, True])
        intersection, union = masks(a, b)
        self.assertEqual(int(union.sum()), int(a.sum()+b.sum()-intersection.sum()))
        self.assertEqual(int(union.sum()), 3)

    def test_location_skip_preserves_distance_in_union(self):
        intersection, union = masks(pd.Series([True, False]), pd.Series([False, False]))
        self.assertFalse(intersection.any())
        self.assertEqual(union.tolist(), [True, False])

    def test_null_membership_rejected(self):
        with self.assertRaises(AssertionError):
            masks(pd.Series([True, None]), pd.Series([True, False]))


if __name__ == '__main__':
    unittest.main()
