import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import observe_second_push_regimes as m
import validate_second_push_regimes as v


class RegimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(os.environ.get("RESEARCH_ROOT", Path(__file__).resolve().parents[3]))
        spec = importlib.util.spec_from_file_location("project_trend", root/"indicator/trend.py")
        cls.trend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.trend)

    def bars(self, count=260, rule="4h"):
        width = m.WIDTH[rule]
        start = pd.Timestamp("2023-01-01", tz="UTC").value//1000000
        c = 100+np.arange(count)*.1+np.sin(np.arange(count)*.7)
        z = pd.DataFrame(dict(open=c, high=c+1, low=c-1, close=c+.2, observations=50,
            first_observation_ms=start+np.arange(count)*width,
            last_observation_ms=start+np.arange(count)*width+width-1),
            index=pd.Index(start+np.arange(count)*width, name="start_ms"))
        return m.regular_bars(z, rule, (start, start+count*width-1))

    def test_exact_close_available_no_unfinished_bar(self):
        z = m.feature_states(self.bars(), "4h", self.trend)
        boundary = int(z.available_ms.iloc[220])
        got = m.values_at(z, [boundary-1, boundary, boundary+1], "4h")
        np.testing.assert_allclose(got.adx14_4h, [z.adx14_4h.iloc[219], z.adx14_4h.iloc[220], z.adx14_4h.iloc[220]])
        self.assertTrue((got.macro_available_ms_4h <= [boundary-1, boundary, boundary+1]).all())

    def test_future_price_perturbation_cannot_change_past(self):
        a = self.bars()
        b = a.copy()
        b.loc[b.index[220:], ["open", "high", "low", "close"]] *= 10
        x = m.feature_states(a, "4h", self.trend)
        y = m.feature_states(b, "4h", self.trend)
        pd.testing.assert_frame_equal(x.iloc[:220], y.iloc[:220])

    def test_empty_bar_resets_warmup_and_stale_snapshot_is_missing(self):
        a = self.bars()
        a.loc[a.index[150], "valid"] = False
        z = m.feature_states(a, "4h", self.trend)
        self.assertTrue(z.adx14_4h.iloc[150:250].isna().all())
        self.assertTrue(np.isfinite(z.adx14_4h.iloc[250]))
        got = m.values_at(z, [z.available_ms.iloc[-1]+m.WIDTH["4h"]], "4h")
        self.assertTrue(got.adx14_4h.isna().all())

    def test_feature_specific_warmup(self):
        z = m.feature_states(self.bars(), "4h", self.trend)
        self.assertEqual(z.er30_4h.first_valid_index(), 30)
        self.assertEqual(z.adx14_4h.first_valid_index(), 99)
        self.assertEqual(z.atr_pct_4h.first_valid_index(), 99)
        self.assertEqual(z.ema50_slope_abs_4h.first_valid_index(), 199)

    def test_cutpoints_ignore_test_and_exact_test_boundary(self):
        z = m.feature_states(self.bars(), "4h", self.trend)
        stop = int(z.available_ms.iloc[230])
        x = m.train_cutpoints(z, "adx14_4h", 0, stop)
        y = z.copy()
        y.loc[y.available_ms.ge(stop), "adx14_4h"] = 100000
        self.assertEqual(x, m.train_cutpoints(y, "adx14_4h", 0, stop))
        self.assertEqual(x["train_state_count"], 131)

    def test_boundaries_missing_and_unsupported(self):
        cuts = dict(supported=True, q_low=1, q_high=2)
        self.assertEqual(list(m.categories([np.nan, 1, 1.5, 2, 3], cuts)),
                         ["missing_value", "low", "middle", "middle", "high"])
        cuts["supported"] = False
        self.assertEqual(list(m.categories([np.nan, 1], cuts)), ["missing_value", "unsupported_train"])

    def test_minute_aggregation_never_interpolates_empty_bar(self):
        width = m.WIDTH["4h"]
        c = pd.DataFrame(dict(timestamp_ms=[0, 60000, 2*width], open=[10, 12, 20],
            high=[11, 14, 21], low=[9, 11, 19], close=[10, 13, 20]))
        z = m.aggregate_minutes(c, "4h", (0, 3*width-1))
        self.assertEqual(z.close.iloc[0], 13)
        self.assertFalse(z.valid.iloc[1])
        self.assertTrue(pd.isna(z.close.iloc[1]))
        with self.assertRaises(ValueError):
            m.aggregate_minutes(pd.concat([c, c.iloc[:1]]), "4h", (0, 3*width-1))

    def test_raw_ohlc_and_id_continuity(self):
        width = m.WIDTH["4h"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"trades.parquet"
            c = pd.DataFrame(dict(timestamp=[1, 2, width+1], price=[10., 13., 11.], agg_trade_id=[8, 9, 10]))
            c.to_parquet(path, index=False)
            bars, count = m.raw_bars(path, (0, 2*m.WIDTH["1d"]-1))
            self.assertEqual(count, 3)
            self.assertEqual(bars["4h"].iloc[0].high, 13)
            self.assertEqual(bars["4h"].iloc[0].close, 13)
            c.loc[2, "agg_trade_id"] = 11
            c.to_parquet(path, index=False)
            with self.assertRaises(ValueError):
                m.raw_bars(path, (0, 2*m.WIDTH["1d"]-1))

    def test_ny_midnight_dst(self):
        a = m.midnight_ms("2024-03-10")
        b = m.midnight_ms("2024-03-11")
        self.assertEqual(b-a, 23*3600000)

    def test_iso_timestamp_precision_cannot_change_epoch_unit(self):
        values = pd.Series(["2024-10-01T09:30:00-04:00", "2024-10-01T09:30:00.123000-04:00"])
        expected = pd.Timestamp("2024-10-01T13:30:00Z").value//1000000
        self.assertEqual(list(m.utc_ms(values)), [expected, expected+123])
        for unit in ("us", "ns", "ms"):
            parsed = pd.to_datetime(values, utc=True, format="ISO8601").dt.as_unit(unit)
            self.assertEqual(list(m.utc_ms(parsed)), [expected, expected+123])

    def test_fold_labels_shared_and_train_outcomes_purged(self):
        # Two variants/snapshots get identical cutpoints; an empty test fold remains represented.
        times = pd.date_range("2024-01-01", "2024-12-31", freq="4h", tz="UTC").asi8//1000000
        clocks = {}
        for rule in m.WIDTH:
            clocks[rule] = pd.DataFrame(dict(available_ms=times))
            for feature in m.FEATURES:
                clocks[rule][feature] = np.arange(len(times), dtype=float)
        rows = []
        for tf in ("15m", "4h"):
            for snap in ("ny_open", "pre_entry"):
                for day, exit_day in (("2024-08-31", "2024-09-01"), ("2024-09-15", "2024-09-16")):
                    row = dict(symbol="TEST", timeframe=tf, entry_mode="intrabar", snapshot=snap,
                        session_date=day, exit_ms=m.midnight_ms(exit_day), gross_r=1., net_r=.9,
                        risk_price=1., entry=100., status="tp_first", target_distance_pct=1., hold_minutes=10.)
                    row.update({f: 500. for f in m.FEATURES})
                    rows.append(row)
        meta = pd.DataFrame([dict(symbol="TEST", fold=1, train_start="2024-01-01", test_start="2024-09-01", test_end_exclusive="2024-11-01"),
                             dict(symbol="TEST", fold=2, train_start="2024-03-01", test_start="2024-11-01", test_end_exclusive="2025-01-01")])
        cuts, fm, labels = m.label_folds(pd.DataFrame(rows), {"TEST": clocks}, meta)
        self.assertEqual(len(cuts), 2*len(m.FEATURES))
        self.assertTrue(fm.loc[fm.fold.eq(1) & fm.phase.eq("train"), "trades"].eq(0).all())
        self.assertTrue(fm.loc[fm.fold.eq(2) & fm.phase.eq("test"), "trades"].eq(0).all())
        self.assertEqual(len(labels), 4*len(m.FEATURES))
        self.assertEqual(labels.category.nunique(), 1)

    def test_block_bootstrap_constant_returns_have_zero_lift(self):
        dates = pd.date_range("2024-09-01", periods=18).strftime("%Y-%m-%d")
        rows = pd.DataFrame(dict(symbol="BTCUSDC", timeframe="2h", entry_mode="body_close",
            feature="adx14_4h", snapshot="pre_entry", session_date=dates, fold=1,
            category=["low", "middle", "high"]*6, net_r=.25))
        folds = pd.DataFrame([dict(symbol="BTCUSDC", fold=1, test_start="2024-09-01", test_end_exclusive="2024-09-19")])
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            (out/"VALIDATION.json").write_text('{}')
            v.uncertainty(out, rows, folds, repeats=30, feature_list=["adx14_4h"])
            result = pd.read_csv(out/"primary_uncertainty.csv")
            np.testing.assert_allclose(result[["net_mean_ci025", "net_mean_ci975"]], .25)
            np.testing.assert_allclose(result[["net_lift_ci025", "net_lift_ci975"]], 0)


if __name__ == "__main__":
    unittest.main()
