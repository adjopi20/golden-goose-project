import unittest
import numpy as np
from backtest_poc_rr import outcome, aligned_bias


class BarrierTests(unittest.TestCase):
    def test_both_bias_conditions_required(self):
        self.assertEqual(aligned_bias(100., 110., 105.), 1)
        self.assertEqual(aligned_bias(110., 100., 105.), -1)
        self.assertEqual(aligned_bias(100., 103., 105.), 0)
        self.assertEqual(aligned_bias(110., 107., 105.), 0)
        self.assertEqual(aligned_bias(100., 100., 95.), 0)
        self.assertEqual(aligned_bias(100., 105., 105.), 0)

    def test_sl_before_later_poc(self):
        result = outcome(np.array([100., 106., 95.]), 95., 1, "endpoint")
        self.assertEqual(result["status"], "sl_first")
        self.assertEqual(result["sl"], 105.)

    def test_tp_first_long(self):
        self.assertEqual(outcome(np.array([100., 105., 90.]), 105., -1, "endpoint")["status"], "tp_first")

    def test_wrong_side_and_flat_bias(self):
        self.assertEqual(outcome(np.array([100., 105.]), 105., 1, "endpoint")["status"], "poc_wrong_side")
        self.assertEqual(outcome(np.array([100., 105.]), 105., 0, "endpoint")["status"], "flat_bias_or_at_poc")

    def test_time_exit_and_poc_mode(self):
        self.assertAlmostEqual(outcome(np.array([100., 99.]), 95., 1, "endpoint")["gross_r"], .2)
        self.assertEqual(outcome(np.array([100., 105.]), 105., 1, "poc")["status"], "tp_first")


if __name__ == "__main__":
    unittest.main()
