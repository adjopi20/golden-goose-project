import unittest
import numpy as np
import pandas as pd

from observe_frozen_vwap_bands import first_reaches, measure, summarize


class FrozenBandTests(unittest.TestCase):
    def test_first_reach_and_return_order_with_price_jump(self):
        prices = np.array([101., 103.2, 102., 100., 99., 97., 101.])
        ts = np.arange(len(prices)) * 60000
        rows = measure(ts, prices, 100.5, 100., 1., [1., 2., 3.], {"p": 100.}, 0, 240 * 60000)
        upper1 = next(r for r in rows if r["direction"] == "upper" and r["level_sigma"] == 1)
        self.assertEqual(upper1["minutes_to_poc"], 3)
        self.assertAlmostEqual(upper1["further_away_before_poc_sigma"], 2.2)
        upper3 = next(r for r in rows if r["direction"] == "upper" and r["level_sigma"] == 3)
        self.assertEqual(upper3["minutes_after_1300"], 1)
        self.assertEqual(upper3["minutes_to_poc"], 2)
        lower3 = next(r for r in rows if r["direction"] == "lower" and r["level_sigma"] == 3)
        self.assertEqual(lower3["minutes_to_poc"], 1)

    def test_initially_outside_is_separate_and_can_return_on_first_trade(self):
        rows = measure(np.array([0, 60000]), np.array([99., 98.]), 103., 100., 1.,
                       [3.], {"p": 100.}, 0, 240 * 60000)
        upper = next(r for r in rows if r["direction"] == "upper")
        self.assertEqual(upper["event_status"], "already_outside_1300")
        self.assertEqual(upper["minutes_to_poc"], 0)
        self.assertEqual(upper["further_away_before_poc_sigma"], 0)

    def test_event_at_poc_is_not_a_return_and_outward_target_is_separate(self):
        rows = measure(np.array([0, 60000, 120000]), np.array([103., 104., 100.]),
                       100., 100., 1., [3.], {"same": 103., "outward": 104.}, 0, 240 * 60000)
        same = next(r for r in rows if r["profile"] == "same" and r["direction"] == "upper")
        outward = next(r for r in rows if r["profile"] == "outward" and r["direction"] == "upper")
        self.assertEqual(same["target_position"], "at_poc")
        self.assertFalse(same["returned_to_poc"])
        self.assertEqual(outward["target_position"], "outward")
        frame = pd.DataFrame([dict(symbol="S", session_date="2026-01-01", **r) for r in rows])
        combined = summarize(frame)
        self.assertTrue(combined.inward_target_events.eq(0).all())

    def test_short_remaining_time_is_not_counted_as_fixed_horizon_failure(self):
        rows = measure(np.array([230 * 60000, 239 * 60000]), np.array([103., 102.]),
                       100., 100., 1., [3.], {"p": 100.}, 0, 240 * 60000)
        frame = pd.DataFrame([dict(symbol="S", session_date="2026-01-01", **r) for r in rows])
        r = summarize(frame).query("direction == 'upper' and event_status == 'first_reach'").iloc[0]
        self.assertEqual(r.inward_target_events, 1)
        self.assertEqual(r.eligible_30m, 0)
        self.assertTrue(np.isnan(r.returned_30m_pct))


if __name__ == "__main__":
    unittest.main()
