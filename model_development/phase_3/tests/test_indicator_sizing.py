import unittest
import numpy as np
import pandas as pd

from model_development.phase_3.indicator_sizing import attach_features, multipliers, walk_forward
from model_development.phase_3.volatility_sizing import simulate, exposure


class IndicatorSizingTests(unittest.TestCase):
    def fixture(self):
        rows = []
        for i, day in enumerate(pd.date_range('2025-01-01', periods=140)):
            stamp = int(day.tz_localize('UTC').timestamp()*1000)
            rows.append(dict(sample_id=str(i), session_day=str(day.date()), entry_timestamp_ms=stamp,
                             exit_timestamp_ms=stamp+3600000, entry_price=100., direction='long'))
        n = len(rows)
        data = dict(rows=rows, start='2025-01-01', end='2025-05-20',
                    distance=np.full(n, .01), r=np.where(np.arange(n)%3==0, 2., -1.),
                    fee_r=np.full(n, .08))
        feat = [dict(ema_directional_gap_pct=.2 if i%2 else -.2,
                     adx14=30. if i%3 else 10.) for i in range(n)]
        return data, feat

    def test_separate_features_and_boundaries(self):
        f = [dict(ema_directional_gap_pct=0., adx14=15.), dict(ema_directional_gap_pct=-1., adx14=30.)]
        np.testing.assert_equal(multipliers(f, 'ema200', 0, .5)[0], [1, .5])
        np.testing.assert_equal(multipliers(f, 'adx', 15, .5)[0], [1, 1])
        np.testing.assert_equal(multipliers(f, 'adx', 25, .5)[0], [.5, 1])

    def test_missing_falls_back(self):
        m, _ = multipliers([dict(ema_directional_gap_pct=None, adx14=None)], 'adx', 20, .5)
        self.assertEqual(m[0], 1.)

    def test_monthly_training_purge_and_matched_risk(self):
        d, f = self.fixture()
        sized, folds = walk_forward(d, f, 'ema200', 0, .005, 5., .5, 90, 10)
        self.assertEqual(folds[0]['test_month'], '2025-04')
        for fold in folds:
            self.assertLess(fold['latest_train_exit_ms'], fold['calibration_cutoff_ms'])
            self.assertAlmostEqual(fold['fixed_matched_risk'], fold['train_challenger_mean_risk'])
        self.assertEqual(len(sized), 50)

    def test_no_trade_removed_or_enlarged(self):
        d, f = self.fixture()
        sized, _ = walk_forward(d, f, 'adx', 25, .005, 5., .5, 90, 10)
        for s in sized:
            self.assertGreater(s['weights']['indicator_sizing'], 0)
            self.assertLessEqual(s['weights']['indicator_sizing'], s['weights']['fixed_base'])
        a, aa, _ = simulate(d, sized, 'fixed_base', 1000.)
        b, bb, _ = simulate(d, sized, 'indicator_sizing', 1000.)
        self.assertEqual(a['net_wr'], b['net_wr'])
        self.assertEqual(a['expectancy_net_r'], b['expectancy_net_r'])
        self.assertEqual([r['sample_id'] for r in aa], [r['sample_id'] for r in bb])
        expected = 1000.
        for row in bb:
            expected *= 1 + row['net_r']*row['planned_risk_fraction']
            self.assertAlmostEqual(expected, row['equity_after'])

    def test_future_outcomes_cannot_change_sizing(self):
        d, f = self.fixture()
        before, folds = walk_forward(d, f, 'adx', 20, .005, 5, .5, 90, 10)
        d['r'] *= 100
        after, after_folds = walk_forward(d, f, 'adx', 20, .005, 5, .5, 90, 10)
        self.assertEqual(before, after)
        self.assertEqual(folds, after_folds)

    def test_leverage_cap(self):
        np.testing.assert_allclose(exposure(np.array([.0001, .1]), .005, 5.), [5., .05])

    def context_fixture(self):
        signal = dict(sample_id='x', symbol='ETHUSDC', direction='short', session_day='2025-01-01',
                      feature_as_of_ms=900000, entry_eligible_timestamp_ms=900000)
        trade = dict(**signal, entry_timestamp_ms=900002)
        ctx = dict(**signal, trend_15m=dict(end_ms=900000, price_vs_ema200_pct=-.5, adx14=20.))
        return dict(rows=[trade]), signal, ctx

    def test_short_ema_direction(self):
        d, s, c = self.context_fixture()
        self.assertEqual(attach_features(d, [s], {'x': c}, 'ETHUSDC')[0]['ema_directional_gap_pct'], .5)

    def test_future_feature_rejected(self):
        d, s, c = self.context_fixture()
        c['trend_15m']['end_ms'] = 1800000
        with self.assertRaises(ValueError): attach_features(d, [s], {'x': c}, 'ETHUSDC')

    def test_missing_context_rejected(self):
        d, s, _ = self.context_fixture()
        with self.assertRaises(ValueError): attach_features(d, [s], {}, 'ETHUSDC')

    def test_direction_mismatch_rejected(self):
        d, s, c = self.context_fixture()
        c['direction'] = 'long'
        with self.assertRaises(ValueError): attach_features(d, [s], {'x': c}, 'ETHUSDC')


if __name__ == '__main__': unittest.main()
