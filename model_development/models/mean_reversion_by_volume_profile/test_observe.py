import json
import unittest
from datetime import date
from pathlib import Path

import numpy as np

from observe import analyse, bounds, profile


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(Path(__file__).with_name("config.json").read_text())
        self.day = date(2026, 8, 10)
        start, pivot, end = bounds(self.day, self.cfg)
        self.ts = np.arange(start, end, 60_000, dtype=np.int64)
        split = np.searchsorted(self.ts, pivot)
        self.prices = np.r_[np.linspace(100, 120, split),
                            np.linspace(121, 90, len(self.ts) - split)]
        self.qty = np.ones(len(self.ts))
        self.ids = np.arange(1, len(self.ts) + 1)

    def test_reversal_profile_and_event_order(self):
        row = analyse(self.day, self.ts, self.prices, self.qty, self.ids, self.cfg)
        self.assertEqual(row["status"], "valid")
        self.assertEqual(row["prior_session_direction"], 1)
        self.assertTrue(row["opposite_direction_at_17"])
        self.assertTrue(row["counter_move_025r"])
        self.assertTrue(row["returned_after_breakout"])
        self.assertTrue(row["poc_reached"])
        self.assertGreater(row["first_reentry_ny"], row["first_breakout_ny"])
        self.assertAlmostEqual(row["max_counter_move_ranges"], 1.5)

    def test_future_volume_does_not_change_profile(self):
        baseline = analyse(self.day, self.ts, self.prices, self.qty, self.ids, self.cfg)
        _, pivot, _ = bounds(self.day, self.cfg)
        changed = self.qty.copy()
        changed[self.ts >= pivot] = 1e9
        after = analyse(self.day, self.ts, self.prices, changed, self.ids, self.cfg)
        self.assertEqual(baseline["poc"], after["poc"])
        self.assertEqual(baseline["profile_volume"], after["profile_volume"])

    def test_missing_ids_and_duplicate_ids_are_excluded(self):
        gap = self.ids.copy()
        gap[100:] += 1
        self.assertEqual(analyse(self.day, self.ts, self.prices, self.qty, gap, self.cfg)["status"], "aggregate_id_gaps")
        duplicate = self.ids.copy()
        duplicate[100] = duplicate[99]
        self.assertEqual(analyse(self.day, self.ts, self.prices, self.qty, duplicate, self.cfg)["status"], "unordered_or_duplicate_trades")

    def test_dst_boundaries(self):
        spring = bounds(date(2026, 3, 8), self.cfg)
        autumn = bounds(date(2026, 11, 1), self.cfg)
        self.assertEqual((spring[2] - spring[1]) / 3_600_000, 15)
        self.assertEqual((autumn[2] - autumn[1]) / 3_600_000, 17)

    def test_known_profile(self):
        result = profile(np.array([100., 101., 102., 103.]), np.array([1., 10., 1., 1.]), 3, .7)
        self.assertEqual(result["poc"], 101.5)
        self.assertEqual(result["val"], 101.)
        self.assertEqual(result["vah"], 102.)


if __name__ == "__main__":
    unittest.main()
