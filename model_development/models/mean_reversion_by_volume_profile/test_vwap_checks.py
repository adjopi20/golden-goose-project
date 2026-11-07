import unittest
import numpy as np
import pandas as pd

from observe_vwap_filters import feature, filters


class VWAPTests(unittest.TestCase):
    def test_daily_weighted_average_and_monotonic_efficiency(self):
        ts = np.arange(0, 17 * 3600000, 60000)
        price = np.linspace(100, 110, len(ts))
        c = pd.DataFrame(dict(timestamp_ms=ts, open=price, high=price,
                              low=price, close=price, volume=np.ones(len(ts))))
        result = feature(c, int(13.5 * 3600000), 17 * 3600000,
                         lambda p, q: (p * q).cumsum() / q.cumsum())
        self.assertAlmostEqual(result["vwap_1300"], 105.)
        self.assertAlmostEqual(result["sigma"], price.std())
        self.assertAlmostEqual(result["efficiency_ratio_5m"], 1.)

    def test_alignment_distance_and_slope_all_required(self):
        frame = pd.DataFrame(dict(price_before_1300=[110, 110, 110], vwap_1300=[100, 100, 100],
                                   morning_bias=[1, 1, -1], distance_vwap_sigma=[1.2, 1.2, 1.2],
                                   vwap_slope_30m_sigma=[.03, .1, .03]))
        result = filters(frame)
        self.assertEqual(result["vwap_aligned_1sigma_low_slope"].tolist(), [True, False, False])


if __name__ == "__main__":
    unittest.main()
