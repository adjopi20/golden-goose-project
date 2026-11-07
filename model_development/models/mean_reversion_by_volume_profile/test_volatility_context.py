"""Causality, DST, missing-data and geometry checks on reused volatility logic."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import observe_volatility_context as study

ROOT = Path(r"C:\Users\adjop\OneDrive\Documents\golden-goose-project")
sizing, context = study.configure(ROOT)


def minutes(start, end):
    t = pd.date_range(start, end, freq="min", inclusive="left", tz="UTC")
    return pd.DataFrame(dict(timestamp_ms=t.as_unit("ms").asi8,
                            close=np.exp(4 + np.arange(len(t)) * .00001 + .001*np.sin(np.arange(len(t))/83)), symbol="TEST"))


class VolatilityContextTests(unittest.TestCase):
    def test_dst_and_future_cannot_change_snapshot(self):
        m = minutes("2024-03-08", "2024-03-13")
        first = sizing.daily_variance(m, "TEST")
        row = first.set_index("session_day").loc["2024-03-10"]
        self.assertEqual(row.expected_bars, 92)
        cutoff = int(pd.Timestamp("2024-03-10 09:00", tz=study.NY).timestamp()*1000)
        m.loc[m.timestamp_ms.ge(cutoff), "close"] *= 2
        second = sizing.daily_variance(m, "TEST").set_index("session_day").loc["2024-03-10"]
        self.assertEqual(row.variance_24h, second.variance_24h)
        fall = sizing.daily_variance(minutes("2024-11-01", "2024-11-06"), "TEST").set_index("session_day")
        self.assertEqual(fall.loc["2024-11-03", "expected_bars"], 100)

    def test_warmup_resets_and_is_causal(self):
        d = pd.DataFrame(dict(session_day=pd.date_range("2024-01-01", periods=245).strftime("%Y-%m-%d"),
                              snapshot_ms=np.arange(245), complete=True, variance_24h=.04))
        d.loc[120, ["complete", "variance_24h"]] = [False, np.nan]
        f = list(sizing.forecasts(d, 20, 120).values())
        self.assertIsNone(f[118]["sigma_24h"])
        self.assertAlmostEqual(f[119]["sigma_24h"], .2)
        self.assertIsNone(f[239]["sigma_24h"])
        self.assertAlmostEqual(f[240]["sigma_24h"], .2)
        d.loc[241:, "variance_24h"] = 100
        self.assertEqual(f[240], list(sizing.forecasts(d, 20, 120).values())[240])

    def test_missing_whole_bar_invalid_not_single_minute_fill(self):
        m = minutes("2024-01-01", "2024-01-06")
        cutoff = int(pd.Timestamp("2024-01-03 12:00", tz="UTC").timestamp()*1000)
        one = m.loc[m.timestamp_ms.ne(cutoff)]
        self.assertTrue(sizing.daily_variance(one, "TEST").set_index("session_day").loc["2024-01-03", "complete"])
        gap = m.loc[~m.timestamp_ms.between(cutoff, cutoff + 14*60000)]
        self.assertFalse(sizing.daily_variance(gap, "TEST").set_index("session_day").loc["2024-01-03", "complete"])

    def test_macro_original_formula_and_missing_close(self):
        dates = pd.date_range("2024-01-01 09:29", periods=90, freq="D", tz=study.NY)
        close = np.exp(4 + .01*np.sin(np.arange(90)/3) + np.arange(90)*.002)
        m = pd.DataFrame(dict(timestamp_ms=dates.as_unit("ms").asi8, close=close))
        f = study.macro_expansion(m)
        local = pd.to_datetime(f.macro_snapshot_ms, unit="ms", utc=True).dt.tz_convert(study.NY)
        self.assertTrue(local.dt.hour.eq(9).all() and local.dt.minute.eq(30).all())
        r = np.log(pd.Series(close)).diff()
        expected = np.log(r.rolling(20).std()/r.rolling(60).std())
        np.testing.assert_allclose(f.macro_expansion_20_60, expected, equal_nan=True)
        # Missing boundary does not merge adjacent days into a daily return.
        gap = study.macro_expansion(m.drop(index=70))
        self.assertTrue(gap.macro_expansion_20_60.iloc[70:].isna().all())
        changed = m.copy()
        changed.loc[80:, "close"] *= 2
        np.testing.assert_allclose(f.macro_expansion_20_60.iloc[:80], study.macro_expansion(changed).macro_expansion_20_60.iloc[:80], equal_nan=True)

    def test_symmetric_distance_and_zero_volatility(self):
        price = pd.Series([100., 100., 100.])
        d = study.normalized_distance(price, pd.Series([98., 102., 98.]), pd.Series([.02, .02, 0.]))
        self.assertEqual(d.iloc[0], 1.)
        self.assertEqual(d.iloc[1], 1.)
        self.assertTrue(pd.isna(d.iloc[2]))


if __name__ == "__main__":
    unittest.main()
