import unittest
from datetime import date

import numpy as np

from observe_four_profiles import clocks, departure_and_return


class FourProfileTests(unittest.TestCase):
    def test_ever_away_even_when_end_is_closer(self):
        eligible, hit, *_ = departure_and_return(np.array([101., 110., 100.5]), np.array([100.1, 99.9]), 100., "ever")
        self.assertTrue(eligible)
        self.assertEqual(hit, 1)
        self.assertFalse(departure_and_return(np.array([101., 110., 100.5]), np.array([100.1, 99.9]), 100., "endpoint")[0])

    def test_no_departure_and_no_return(self):
        self.assertFalse(departure_and_return(np.array([110., 109., 108.]), np.array([107., 106.]), 100., "ever")[0])
        eligible, hit, *_ = departure_and_return(np.array([101., 110.]), np.array([109., 108.]), 100., "ever")
        self.assertTrue(eligible)
        self.assertIsNone(hit)

    def test_return_before_13_does_not_count_after_13(self):
        eligible, hit, *_ = departure_and_return(np.array([100., 110., 100.]), np.array([101., 102.]), 100., "ever")
        self.assertTrue(eligible)
        self.assertIsNone(hit)
        self.assertEqual(departure_and_return(np.array([100., 110., 100.]), np.array([101., 100.]), 100., "ever")[1], 1)

    def test_fixed_13_ny_dst_conversion(self):
        from observe import iso, WIB
        summer = clocks(date(2026, 8, 10))
        winter = clocks(date(2026, 1, 10))
        self.assertIn("2026-08-11T00:00", iso(summer[3], WIB))
        self.assertIn("2026-01-11T01:00", iso(winter[3], WIB))
        self.assertEqual((summer[3] - summer[2]) / 3600000, 3.5)


if __name__ == "__main__":
    unittest.main()
