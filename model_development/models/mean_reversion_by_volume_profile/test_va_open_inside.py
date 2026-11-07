import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

from observe_va_open_inside import filter_sessions, replay_outward, eligible

CFG = dict(max_internal_gap_seconds=300, max_boundary_gap_seconds=60, fee_bps_per_side=4)


def path(prices):
    from previous_value_area_reversal import bounds
    first, end = bounds(__import__("datetime").date(2026, 1, 2))[1:]
    ts = np.arange(first, end, 60000, dtype=np.int64)
    p = np.full(len(ts), float(prices[-1]))
    p[:len(prices)] = prices
    return ts, p, np.ones(len(ts)), np.arange(len(ts), dtype=np.int64)


def row():
    return dict(symbol="BTCUSDC", session_date="2026-01-02", valid_session=True,
                val=90., poc=100., vah=110., opening_price=95., status="tp_first",
                touch_direction="toward_value", entry=95., gross_r=1., net_r=.9)


class InsideTests(unittest.TestCase):
    def test_waits_after_opening_and_can_choose_opposite_edge(self):
        # Pre-open below VAL -> opening 95 would enter long under v14.
        # New rule waits; price first reaches VAH, so the new trade is SHORT.
        result = replay_outward(row(), path([95., 105., 111., 100.]), CFG)
        self.assertEqual(result["side"], -1)
        self.assertEqual(result["entry"], 111.)
        self.assertEqual(result["tp"], 100.)
        self.assertEqual(result["sl"], 122.)
        self.assertEqual(result["gross_r"], 1.)
        self.assertAlmostEqual(result["net_r"], 1-.0004*(111+100)/11)
        self.assertEqual(result["hold_minutes"], 1.)

    def test_opening_cross_does_not_trade_if_no_later_boundary_touch(self):
        result = replay_outward(row(), path([95., 100., 105.]), CFG)
        self.assertEqual(result["status"], "no_touch")
        self.assertNotIn("entry", result)
        self.assertNotIn("gross_r", result)

    def test_outside_and_boundary_openings_cannot_enter(self):
        for p in (85., 90., 110., 115.):
            self.assertFalse(eligible(dict(row(), opening_price=p)))
        data = pd.DataFrame([dict(row(), opening_price=85.)])
        loader = Mock(side_effect=AssertionError("Must not read raw data"))
        result, repairs = filter_sessions(data, CFG, loader)
        self.assertEqual(result.status.iloc[0], "no_entry_open_not_inside_va")
        self.assertTrue(pd.isna(result.entry.iloc[0]))
        self.assertTrue(result.valid_session.iloc[0])
        self.assertTrue(repairs.empty)

    def test_saved_outward_touch_is_reused_and_original_is_not_mutated(self):
        saved = dict(row(), touch_direction="out_of_value", side=1., entry=89., risk_price=11., exit_price=100.,
                     net_r=1-.0004*189/11)
        data = pd.DataFrame([saved])
        before = data.copy(deep=True)
        loader = Mock(side_effect=AssertionError("No replay for unchanged candidate"))
        result, repairs = filter_sessions(data, CFG, loader)
        pd.testing.assert_frame_equal(data, before)
        self.assertEqual(result.entry.iloc[0], 89.)
        self.assertTrue(repairs.empty)
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
