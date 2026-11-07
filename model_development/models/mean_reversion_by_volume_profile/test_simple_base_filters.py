import unittest
import pandas as pd
from observe_simple_base_filters import choose, distance_groups, features


class FilterTests(unittest.TestCase):
    def test_tercile_boundaries(self):
        result = distance_groups(pd.Series([.5, 1., 1.5, 2., 3.]), 1., 2.)
        self.assertEqual(result.tolist(), ['near', 'near', 'middle', 'middle', 'far'])

    def test_no_positive_group_skips(self):
        train = pd.DataFrame(dict(group=['a']*30, net_r=[-.1]*30))
        self.assertIsNone(choose(train, 'group'))

    def test_small_positive_group_not_selected(self):
        train = pd.DataFrame(dict(group=['a']*24+['b']*25, net_r=[1.]*24+[.1]*25))
        self.assertEqual(choose(train, 'group'), 'b')

    def test_best_supported_group_and_tie(self):
        train = pd.DataFrame(dict(group=['b']*25+['a']*25, net_r=[.1]*50))
        self.assertEqual(choose(train, 'group'), 'a')

    def test_feature_does_not_use_exit(self):
        data = pd.DataFrame([dict(status='tp_first', opening_price=95., val=90., vah=110.,
            poc=100., entry=90., side=1, risk_price=10., exit_price=100.)])
        a = features(data, dict(fee_bps_per_side=4))
        data['exit_price'] = 10000.
        b = features(data, dict(fee_bps_per_side=4))
        self.assertEqual(a.estimated_fee_r_known.tolist(), b.estimated_fee_r_known.tolist())
        self.assertAlmostEqual(a.estimated_fee_r_known.iloc[0], .0072)

    def test_inside_outside_and_direction(self):
        data = pd.DataFrame([
            dict(status='tp_first', opening_price=80., val=90., vah=110., poc=100., entry=90., side=1, risk_price=10.),
            dict(status='sl_first', opening_price=105., val=90., vah=110., poc=100., entry=110., side=-1, risk_price=10.)])
        out = features(data, dict(fee_bps_per_side=4))
        self.assertEqual(out.location_side.tolist(), ['outside_va_long', 'inside_va_short'])


if __name__ == '__main__':
    unittest.main()
