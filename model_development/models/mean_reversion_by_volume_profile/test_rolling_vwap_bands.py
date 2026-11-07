import unittest
import numpy as np
import pandas as pd

from observe_frozen_vwap_bands import measure
from observe_rolling_vwap_bands import rolling_states


def candles(prices, qty, times=None):
    return pd.DataFrame(dict(timestamp_ms=np.arange(len(prices)) * 60000 if times is None else times,
        open=prices, high=prices, low=prices, close=prices, volume=qty))


class RollingTests(unittest.TestCase):
    def test_window_discards_old_price_and_uses_weighted_dispersion(self):
        c = candles(np.array([100., 101., 110., 120.]), np.array([1., 2., 3., 4.]))
        states = rolling_states(c, 3)
        last = states.iloc[-1]
        mean = np.average([101., 110., 120.], weights=[2., 3., 4.])
        sigma = np.sqrt(np.average((np.array([101., 110., 120.]) - mean) ** 2, weights=[2., 3., 4.]))
        self.assertAlmostEqual(last.vwap, mean)
        self.assertAlmostEqual(last.sigma, sigma)
        self.assertEqual(last.timestamp_ms, 240000)

    def test_future_candles_cannot_change_an_earlier_state(self):
        c = candles(np.array([100., 101., 102., 103., 104.]), np.ones(5))
        a = rolling_states(c, 3)
        c.loc[3:, ['open', 'high', 'low', 'close']] = 900.
        b = rolling_states(c, 3)
        self.assertEqual(a.vwap.iloc[2], b.vwap.iloc[2])
        self.assertEqual(a.sigma.iloc[2], b.sigma.iloc[2])

    def test_missing_minute_invalidates_elapsed_time_window(self):
        c = candles(np.array([100., 101., 102., 103.]), np.ones(4), np.array([0, 60000, 180000, 240000]))
        self.assertFalse(rolling_states(c, 3).valid.any())

    def test_band_shift_can_create_event_without_price_rise(self):
        ts = np.array([0, 60000, 120000])
        prices = np.array([103., 103., 100.])
        means = np.array([101., 100., 100.])
        sigmas = np.ones(3)
        rows = measure(ts, prices, 103., means, sigmas, [3.], {"p": 100.}, 0, 240 * 60000, (101., 1.))
        row = next(r for r in rows if r['direction'] == 'upper')
        self.assertEqual(row['event_status'], 'first_reach')
        self.assertEqual(row['minutes_after_1300'], 1)
        self.assertEqual(row['vwap'], 100.)
        self.assertEqual(row['minutes_to_poc'], 1)


if __name__ == '__main__':
    unittest.main()
