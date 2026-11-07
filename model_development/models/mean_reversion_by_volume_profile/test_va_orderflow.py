import unittest

import numpy as np
import pandas as pd

from observe_va_orderflow import feature_at, fit_gate, apply_gate, evaluate, controls


def bars():
    index = pd.date_range("2026-01-02T09:50:00Z", periods=21, freq="min")
    c = pd.DataFrame(dict(open=100., high=101., low=99., close=100.1, volume=10.,
                          buy_volume=8., sell_volume=2., valid=True), index=index)
    h = pd.DataFrame(dict(atr=[2.]), index=pd.DatetimeIndex(["2026-01-02T10:00:00Z"]))
    return c, h


def train():
    return pd.DataFrame(dict(feature_status=["valid"]*30, outward_delta_imbalance=np.linspace(.1, .9, 30),
                              outward_progress_atr=np.linspace(.02, .6, 30)))


class OrderflowTests(unittest.TestCase):
    def test_incomplete_minute_and_future_bars_cannot_change_signal(self):
        c, h = bars()
        stamp = pd.Timestamp("2026-01-02T10:03:30Z").value//1_000_000
        result = feature_at(c, h, stamp, -1)
        self.assertEqual(result["feature_status"], "valid")
        self.assertAlmostEqual(result["outward_delta_imbalance"], .6)
        self.assertAlmostEqual(result["outward_progress_atr"], .05)
        changed = c.copy()
        changed.loc[changed.index >= "2026-01-02T10:03:00Z", ["close", "buy_volume", "sell_volume"]] = 999999.
        self.assertEqual(result, feature_at(changed, h, stamp, -1))
        mirror = feature_at(c, h, stamp, 1)
        self.assertAlmostEqual(mirror["outward_delta_imbalance"], -.6)
        self.assertAlmostEqual(mirror["outward_progress_atr"], -.05)

    def test_exact_minute_boundary_and_missing_or_stale_inputs(self):
        c, h = bars()
        stamp = pd.Timestamp("2026-01-02T10:03:00Z").value//1_000_000
        result = feature_at(c, h, stamp, -1)
        self.assertEqual(result["feature_cutoff_utc"], "2026-01-02T10:03:00+00:00")
        c.loc["2026-01-02T10:02:00Z", "valid"] = False
        self.assertEqual(feature_at(c, h, stamp, -1)["feature_status"], "missing_five_minutes")
        c, h = bars()
        h.index -= pd.Timedelta(hours=1)
        self.assertEqual(feature_at(c, h, stamp, -1)["feature_status"], "missing_hourly_atr")

    def test_gate_mirrors_pressure_and_does_not_inspect_outcomes(self):
        tr = train()
        gate = fit_gate(tr)
        tr["gross_r"] = np.arange(len(tr))*100.
        self.assertEqual(gate, fit_gate(tr))
        te = pd.DataFrame(dict(feature_status=["valid"]*4,
                               outward_delta_imbalance=[.8, -.8, .1, .8],
                               outward_progress_atr=[.01, .01, .01, 1.]))
        self.assertEqual(apply_gate(te, gate).tolist(), [True, False, False, False])
        self.assertIsNone(fit_gate(tr.iloc[:24]))
        self.assertFalse(apply_gate(te, None).any())

    def test_test_outcomes_cannot_change_thresholds_or_trade_selection(self):
        tr = train()
        tr["session_date"] = pd.date_range("2025-05-01", periods=30).strftime("%Y-%m-%d")
        te = train().iloc[:2].copy()
        te["session_date"] = ["2026-01-02", "2026-01-03"]
        te["outward_delta_imbalance"] = [.9, .01]
        te["outward_progress_atr"] = [.01, 1.]
        ledger = pd.concat([tr, te], ignore_index=True)
        ledger["symbol"] = "TEST"
        ledger["exit_timestamp_ny"] = ledger.session_date+"T17:00:00-05:00"
        ledger["gross_r"], ledger["net_r"] = 1., .9
        ledger["risk_price"], ledger["entry"] = 1., 100.
        ledger["status"], ledger["target_distance_pct"], ledger["hold_minutes"] = "tp_first", 1., 1.
        cfg = dict(account_risk_fraction=.005, max_entry_notional_equity=5)
        folds = pd.DataFrame([dict(symbol="TEST", fold=1, phase="test", train_start="2025-05-01",
                                   test_start="2026-01-01", test_end_exclusive="2026-03-01", valid_sessions=59)])
        predictions, metrics, calibration = evaluate(ledger, folds, cfg)
        altered = ledger.copy()
        altered.loc[altered.session_date.ge("2026-01-01"), ["gross_r", "net_r"]] = [-1., -1.1]
        future, _, future_calibration = evaluate(altered, folds, cfg)
        self.assertEqual(calibration, future_calibration)
        self.assertEqual(predictions.selected.tolist(), future.selected.tolist())
        self.assertEqual(predictions.selected.tolist(), [True, False])
        # Control routines must support a small but nonempty selected cohort.
        result = controls(predictions, metrics, repeats=40)
        self.assertEqual(result.status.iloc[0], "passed")


if __name__ == "__main__":
    unittest.main()
