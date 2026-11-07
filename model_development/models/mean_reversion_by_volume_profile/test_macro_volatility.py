import unittest
import numpy as np
import pandas as pd
import observe_macro_volatility as m


def tr(high, low, close):
    return pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)


def bars(n=70):
    return pd.DataFrame(dict(start_ms=np.arange(n, dtype=np.int64)*m.DAY,
        open=np.full(n, 100.), high=np.full(n, 101.), low=np.full(n, 99.),
        close=np.full(n, 100.), valid=np.ones(n, dtype=bool)))


class MacroVolTests(unittest.TestCase):
    def test_atr_sma_30_and_return_std(self):
        states = m.build_states(bars(), tr)
        self.assertTrue(states.atr30_pct.iloc[:30].isna().all())
        self.assertEqual(states.atr30_pct.iloc[30], 2.)
        self.assertEqual(states.std30_return_pct.iloc[30], 0.)

    def test_rolling_window_forgets_old_shock(self):
        d = bars()
        d.loc[1, ['high', 'low']] = [120., 80.]
        states = m.build_states(d, tr)
        self.assertAlmostEqual(states.atr30.iloc[30], (40+29*2)/30)
        self.assertEqual(states.atr30.iloc[31], 2.)

    def test_sample_std_log_returns(self):
        d = bars()
        d['close'] = 100*np.exp(np.arange(len(d))**2*.0001)
        d['high'], d['low'], d['open'] = d.close+1, d.close-1, d.close
        states = m.build_states(d, tr)
        expected = np.std(np.diff(np.log(d.close.iloc[:31])), ddof=1)*100
        self.assertAlmostEqual(states.std30_return_pct.iloc[30], expected)

    def test_missing_day_breaks_consecutive_window(self):
        states = m.build_states(bars().drop(index=15), tr)
        self.assertTrue(states.atr30_pct.iloc[15:46].isna().all())
        self.assertEqual(states.atr30_pct.iloc[46], 2.)

    def test_latest_completed_day_and_exact_boundary(self):
        states = m.build_states(bars(), tr)
        times = [pd.Timestamp(t, unit='ms', tz='UTC').isoformat() for t in (30*m.DAY+12*3600000, 31*m.DAY)]
        labeled = m.entry_states(pd.DataFrame(dict(entry_timestamp_ny=times)), states)
        self.assertTrue(pd.isna(labeled.atr30_pct.iloc[0]))
        self.assertEqual(labeled.atr30_pct.iloc[1], 2.)
        self.assertEqual(labeled.macro_available_ms.iloc[1], 31*m.DAY)

    def test_future_day_does_not_change_prior_feature(self):
        a = bars()
        before = m.build_states(a, tr)
        a.loc[40:, ['high', 'low', 'close']] = [200., 50., 150.]
        after = m.build_states(a, tr)
        pd.testing.assert_frame_equal(before.loc[:39, list(m.FEATURES)], after.loc[:39, list(m.FEATURES)])

    def test_percentage_features_price_scale_invariant(self):
        a = bars()
        b = a.copy()
        b[['open', 'high', 'low', 'close']] *= 3
        pd.testing.assert_frame_equal(m.build_states(a, tr)[list(m.FEATURES)], m.build_states(b, tr)[list(m.FEATURES)])

    def test_regime_cut_boundaries_and_missing(self):
        labels = m.classify(pd.Series([1., 2., 3., 4., float('nan')]), 2., 3.)
        self.assertEqual(labels.tolist(), ['low', 'low', 'middle', 'high', 'unavailable'])


if __name__ == '__main__':
    unittest.main()
