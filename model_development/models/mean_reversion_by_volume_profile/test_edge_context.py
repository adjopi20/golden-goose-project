"""Checks for temporal availability, missing history and train-only selection."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(r"C:\Users\adjop\OneDrive\Documents\golden-goose-project")
sys.path[:0] = [str(PROJECT), str(PROJECT / "model_development/models/mean_reversion_by_volume_profile")]
import observe_edge_context as study


def candles(n):
    stamps = pd.date_range("2024-01-01", periods=n, freq="min", tz="UTC")
    price = 100 + np.arange(n) * .001 + np.sin(np.arange(n) / 50) * .1
    return pd.DataFrame(dict(timestamp_ms=stamps.as_unit("ms").asi8,
        open=price, high=price + .1, low=price - .1, close=price,
        volume=2., buy_volume=1.2, sell_volume=.8))


class EdgeContextTests(unittest.TestCase):
    def test_only_past_fill_and_long_gap_invalid(self):
        c = candles(15).drop(index=range(2, 10))
        result = study.minute_grid(c)
        self.assertTrue(result.valid.iloc[2:7].all())
        self.assertFalse(result.valid.iloc[7:10].any())
        self.assertEqual(result.close.iloc[2], result.close.iloc[1])
        self.assertEqual(result.volume.iloc[2], 0)
        self.assertTrue(result.valid.iloc[10])

    def test_invalid_existing_data_is_not_repaired(self):
        c = candles(10)
        c.loc[3, "high"] = 1
        with self.assertRaises(ValueError):
            study.minute_grid(c)

    def test_macro_completed_only_and_future_invariance(self):
        c = study.minute_grid(candles(240 * 205))
        state = study.completed_states(c, "4h", PROJECT)
        first_ready = c.index[0] + pd.Timedelta(hours=4 * 200)
        self.assertTrue(pd.isna(study.state_before(state, first_ready - pd.Timedelta(seconds=1), "4h").direction))
        before = study.state_before(state, first_ready, "4h")
        changed = c.copy()
        changed.loc[changed.index >= first_ready, ["open", "high", "low", "close"]] *= 2
        after = study.state_before(study.completed_states(changed, "4h", PROJECT), first_ready, "4h")
        self.assertAlmostEqual(before.ema_gap, after.ema_gap)
        self.assertAlmostEqual(before.adx, after.adx)

    def test_calendar_windows_nonoverlap(self):
        folds = list(study.fold_schedule("2022-01-01", "2023-04-30"))
        self.assertEqual(str(folds[0][1].date()), "2022-09-01")
        self.assertEqual(str(folds[1][0].date()), "2022-03-01")
        self.assertEqual(folds[0][2], folds[1][1])
        partial = list(study.fold_schedule("2022-01-05", "2022-11-30"))
        self.assertEqual(str(partial[0][0].date()), "2022-02-01")

    def test_value_failure_count_is_outside_to_inside(self):
        fraction, failures = study.acceptance(np.array([99., 102., 103., 100., 102., 99.]), 98, 101, 1)
        self.assertEqual(fraction, .5)
        self.assertEqual(failures, 2)

    def test_train_only_calibration_and_purge(self):
        days = pd.date_range("2022-01-01", "2022-10-31", freq="D")
        g = pd.DataFrame(dict(symbol="BTCUSDC", profile="ny_0930_1300", session_date=days.strftime("%Y-%m-%d"),
            profile_status="valid", status="tp_first", gross_r=np.where(np.arange(len(days)) % 3 == 0, 1., -.2),
            distance_poc_atr=np.arange(len(days)) % 3,
            exit_timestamp_ny=(days + pd.Timedelta(hours=17)).tz_localize(study.NY).astype(str)))
        g.loc[g.session_date.eq("2022-08-31"), "exit_timestamp_ny"] = "2022-09-01T01:00:00-04:00"
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            study.analyze(g, out)
            first = json.loads((out / "calibration.json").read_text())
            folds = pd.read_csv(out / "folds.csv")
            self.assertEqual(folds.purged_train_outcomes.iloc[0], 1)
            self.assertEqual(first[0]["selected_category"], "low")
            g.loc[g.session_date.ge("2022-09-01"), "gross_r"] *= -100
            study.analyze(g, out)
            second = json.loads((out / "calibration.json").read_text())
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
