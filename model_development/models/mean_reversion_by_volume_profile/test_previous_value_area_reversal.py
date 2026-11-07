import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import previous_value_area_reversal as model

ROOT = Path(r"C:\Users\adjop\OneDrive\Documents\golden-goose-project")
CFG = json.loads(Path(model.__file__).with_name("previous_value_area_config.json").read_text())
sys.path.insert(0, str(ROOT))
from indicator.volume_profile import build_basic_volume_profile


def fixed_profile(frame, **kwargs):
    return dict(poc_price=100., val=90., vah=110., total_volume=float(frame.qty.sum()))


def arrays(day, first_prices, previous_price=100.):
    start, opening, end = model.bounds(day)
    ts = np.arange(start, end, 60000, dtype=np.int64)
    price = np.full(len(ts), previous_price)
    split = int(np.searchsorted(ts, opening))
    price[split:] = first_prices[-1]
    price[split:split+len(first_prices)] = first_prices
    return ts, price, np.ones(len(ts)), np.arange(len(ts), dtype=np.int64)


class PreviousValueAreaTests(unittest.TestCase):
    def test_both_directions_and_no_immediate_entry_outside(self):
        self.assertIsNone(model.first_touch(np.array([85., 86.]), 80., 90., 110.))
        h = model.first_touch(np.array([85., 91.]), 80., 90., 110.)
        self.assertEqual((h["index"], h["edge"], h["side"], h["approach"]), (1, "val", 1, "from_below"))
        h = model.first_touch(np.array([100., 89.]), 100., 90., 110.)
        self.assertEqual((h["index"], h["edge"], h["approach"]), (1, "val", "from_above"))
        h = model.first_touch(np.array([115., 109.]), 120., 90., 110.)
        self.assertEqual((h["index"], h["edge"], h["side"], h["approach"]), (1, "vah", -1, "from_above"))

    def test_first_candidate_and_opening_crossing(self):
        h = model.first_touch(np.array([90., 110., 90.]), 100., 90., 110.)
        self.assertEqual((h["index"], h["edge"]), (0, "val"))
        h = model.first_touch(np.array([85.]), 100., 90., 110.)
        self.assertEqual(h["index"], 0)
        h = model.first_touch(np.array([120.]), 80., 90., 110.)
        self.assertEqual(h["edge"], "val")

    def test_gap_price_is_not_replaced_by_boundary_or_ideal_exit(self):
        row = model.analyse(date(2026, 1, 2), arrays(date(2026, 1, 2), [100., 89., 102., 90.]), CFG, fixed_profile)
        self.assertEqual(row["entry"], 89.)
        self.assertEqual(row["tp"], 100.)
        self.assertEqual(row["sl"], 78.)
        self.assertEqual(row["barrier_gross_r"], 1.)
        self.assertAlmostEqual(row["gross_r"], 13/11)
        self.assertEqual(row["exit_price"], 102.)
        self.assertAlmostEqual(row["net_r"], 13/11-.0004*(89+102)/11)

    def test_short_and_one_opportunity_even_when_later_trade_wins(self):
        day = date(2026, 1, 2)
        row = model.analyse(day, arrays(day, [100., 111., 123., 90., 100.]), CFG, fixed_profile)
        self.assertEqual(row["side"], -1)
        self.assertEqual(row["status"], "sl_first")
        self.assertEqual(row["sl"], 122.)
        self.assertEqual(row["exit_price"], 123.)
        # A gap across both boundaries consumes the first candidate; do not
        # select a later favorable entry after discovering bad geometry.
        row = model.analyse(day, arrays(day, [120., 110., 100., 90.], previous_price=80.), CFG, fixed_profile)
        self.assertEqual(row["status"], "poc_wrong_side")
        self.assertEqual(row["touch_edge"], "val")

    def test_same_timestamp_order_uses_ids_and_time_exit_is_before_cutoff(self):
        day = date(2026, 1, 2)
        a = list(arrays(day, [100., 90., 100., 80.]))
        split = np.searchsorted(a[0], model.bounds(day)[1])
        a[0][split+2] = a[0][split+1]
        row = model.analyse(day, a, CFG, fixed_profile)
        self.assertEqual(row["status"], "tp_first")
        self.assertEqual(row["entry_timestamp_ny"], row["exit_timestamp_ny"])
        self.assertLess(row["entry_agg_trade_id"], row["exit_agg_trade_id"])
        row = model.analyse(day, arrays(day, [100., 90., 95.]), CFG, fixed_profile)
        self.assertEqual(row["status"], "time_exit")
        self.assertAlmostEqual(row["gross_r"], .5)
        self.assertEqual(row["exit_timestamp_ny"], "2026-01-03T09:29:00-05:00")

    def test_profile_is_causal_and_quality_checks_cross_boundary(self):
        day = date(2026, 1, 2)
        seen = []
        def builder(frame, **kwargs):
            seen.append(frame.price.to_numpy().copy())
            return fixed_profile(frame, **kwargs)
        a = arrays(day, [100., 90., 100.], previous_price=99.)
        model.analyse(day, a, CFG, builder)
        b = arrays(day, [100., 110., 100.], previous_price=99.)
        model.analyse(day, b, CFG, builder)
        np.testing.assert_array_equal(seen[0], seen[1])
        split = np.searchsorted(a[0], model.bounds(day)[1])
        a[3][split:] += 1
        row = model.analyse(day, a, CFG, fixed_profile)
        self.assertEqual(row["status"], "boundary_aggregate_id_gap")

    def test_dst_and_8_2_calendar(self):
        for day, hours in ((date(2026, 3, 7), 23), (date(2026, 10, 31), 25)):
            b = model.bounds(day)
            self.assertEqual((b[2]-b[1])/3600000, hours)
        folds = list(model.fold_schedule("2022-01-01", "2023-02-28"))
        self.assertEqual(len(folds), 3)
        self.assertEqual(str(folds[0][1].date()), "2022-09-01")
        self.assertEqual(str(folds[1][0].date()), "2022-03-01")

    def test_report_purges_train_boundary_and_test_folds_do_not_overlap(self):
        # Synthetic session rows exercise full calendar folds without raw I/O.
        rows = []
        for stamp in pd.date_range("2022-01-01", "2023-02-28"):
            day = stamp.date()
            row = model.analyse(day, arrays(day, [100., 90., 95.]), CFG, fixed_profile)
            rows.append(dict(row, symbol="XRPUSDT"))
        with tempfile.TemporaryDirectory(dir=Path(model.__file__).parent) as folder:
            model.write_reports(pd.DataFrame(rows), CFG, Path(folder))
            folds = pd.read_csv(Path(folder)/"fold_metrics.csv")
            self.assertTrue(folds.train_outcomes_purged.eq(1).all())
            test = folds.loc[folds.phase.eq("test")]
            expected = len(pd.date_range("2022-09-01", "2023-02-28"))
            self.assertEqual(test.trades.sum(), expected)
            summary = pd.read_csv(Path(folder)/"summary.csv")
            self.assertEqual(summary.loc[summary["sample"].eq("walk_forward_test"), "trades"].iloc[0], expected)

    def test_end_to_end_synthetic_raw_profile_and_resume(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory(dir=Path(model.__file__).parent) as folder:
            folder = Path(folder)
            day = date(2026, 1, 2)
            ts, price, qty, ids = arrays(day, [100., 90., 100.])
            price = 100+10*np.sin(np.arange(len(ts))*.01)
            path = folder/"synthetic.parquet"
            pq.write_table(pa.table(dict(timestamp=ts, price=price, qty=qty, agg_trade_id=ids)), path, row_group_size=500)
            cfg = dict(CFG, assets={"SYNTHETIC":str(path)})
            config = folder/"config.json"
            config.write_text(json.dumps(cfg))
            out = folder/"run"
            cmd = [sys.executable, str(Path(model.__file__)), "--root", str(ROOT), "--config", str(config),
                   "--start", str(day), "--end", str(day), "--output", str(out)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            audit = json.loads((out/"audit.json").read_text())
            self.assertEqual(audit["status"], "passed")
            self.assertEqual(audit["periods"], 1)
            sessions = pd.read_csv(out/"sessions.csv")
            self.assertTrue(sessions.valid_session.iloc[0])
            self.assertTrue(sessions.status.isin(model.ACTIVE).iloc[0])
            before = (out/"SYNTHETIC_sessions.jsonl").read_bytes()
            subprocess.run(cmd+["--resume"], check=True, capture_output=True, text=True)
            self.assertEqual((out/"SYNTHETIC_sessions.jsonl").read_bytes(), before)
            self.assertEqual(json.loads((out/"audit.json").read_text()), audit)
            cfg["fee_bps_per_side"] = 5
            config.write_text(json.dumps(cfg))
            failed = subprocess.run(cmd+["--resume"], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Resume inputs changed", failed.stderr)

    def test_empty_archive_coverage_still_writes_readable_outputs(self):
        data = pd.DataFrame([dict(symbol="BTCUSDC", session_date="2022-01-01", valid_session=False,
                                  status="outside_full_archive")])
        with tempfile.TemporaryDirectory(dir=Path(model.__file__).parent) as folder:
            out = Path(folder)
            model.write_reports(data, CFG, out)
            summary = pd.read_csv(out/"summary.csv")
            self.assertTrue(summary.trades.eq(0).all())
            for name in ("fold_metrics.csv", "yearly_metrics.csv", "cohort_metrics.csv"):
                self.assertTrue(pd.read_csv(out/name).empty)


if __name__ == "__main__":
    unittest.main()
