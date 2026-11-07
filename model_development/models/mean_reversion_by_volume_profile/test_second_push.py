import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd

import observe_second_push as model

ROOT = Path(r"C:\Users\adjop\OneDrive\Documents\golden-goose-project")
CFG = json.loads(Path(model.__file__).with_name("second_push_config.json").read_text())
DAY = date(2026, 1, 2)


def path(day=DAY):
    start, opening, end = model.base.bounds(day)
    ts = np.arange(start, end, 60000, dtype=np.int64)
    return ts, np.full(len(ts), 100.), np.ones(len(ts)), np.arange(len(ts), dtype=np.int64)


def bar(arrays, stamp, minutes, opening, close, wick=None):
    ts, prices, _, _ = arrays
    start = pd.Timestamp(stamp).value//1_000_000
    i, j = np.searchsorted(ts, [start, start+minutes*60000])
    prices[i:j] = np.linspace(opening, close, j-i)
    if wick is not None:
        prices[i+(j-i)//2] = wick


def fixed_profile(frame, **kwargs):
    return dict(val=90., vah=110., poc_price=100., total_volume=float(frame.qty.sum()))


def setup():
    a = path()
    bar(a, "2026-01-02T14:30Z", 15, 100, 112)
    bar(a, "2026-01-02T14:45Z", 15, 112, 105)
    bar(a, "2026-01-02T15:00Z", 15, 105, 111)
    bar(a, "2026-01-02T15:15Z", 15, 109, 100)
    return a


def outputs(a, builder=fixed_profile, cached=None):
    return {(r["timeframe"], r["entry_mode"]):r for r in model.analyse(DAY, a, CFG, builder, cached)}


class SecondPushTests(unittest.TestCase):
    def test_completed_body_then_separate_pullback_then_two_entry_modes(self):
        r = outputs(setup())
        intrabar, confirmed = r[("15m", "intrabar")], r[("15m", "body_close")]
        self.assertEqual(intrabar["touch_edge"], "vah")
        self.assertEqual(intrabar["side"], -1)
        self.assertEqual(intrabar["first_push_available_ny"], "2026-01-02T09:45:00-05:00")
        self.assertEqual(intrabar["pullback_available_ny"], "2026-01-02T10:00:00-05:00")
        self.assertLess(intrabar["entry_timestamp_ny"], confirmed["entry_timestamp_ny"])
        self.assertEqual(confirmed["entry_timestamp_ny"], "2026-01-02T10:15:00-05:00")
        self.assertEqual(confirmed["entry"], 109.)  # Not the last close at 111.
        self.assertEqual(confirmed["tp"], 100.)
        self.assertEqual(confirmed["sl"], 118.)
        self.assertAlmostEqual(confirmed["net_r"], confirmed["gross_r"]-.0004*(109+confirmed["exit_price"])/9)

    def test_long_mirrors_short(self):
        a = setup()
        a[1][:] = 200-a[1]
        r = outputs(a)[("15m", "body_close")]
        self.assertEqual(r["side"], 1)
        self.assertEqual(r["touch_edge"], "val")
        self.assertEqual(r["entry"], 91.)
        self.assertEqual(r["sl"], 82.)
        self.assertEqual(r["tp"], 100.)

    def test_wick_only_first_push_does_not_count(self):
        a = path()
        bar(a, "2026-01-02T14:30Z", 15, 100, 105, wick=112)
        r = outputs(a)
        self.assertEqual(r[("15m", "intrabar")]["status"], "no_first_body_push")

    def test_adjacent_body_contacts_are_not_two_pushes(self):
        a = path()
        bar(a, "2026-01-02T14:30Z", 15, 100, 112)
        bar(a, "2026-01-02T14:45Z", 15, 112, 105)
        r = outputs(a)
        self.assertEqual(r[("15m", "intrabar")]["status"], "no_second_push")
        self.assertEqual(r[("15m", "body_close")]["status"], "no_second_push")

    def test_intrabar_keeps_a_second_contact_that_finishes_as_wick(self):
        a = path()
        bar(a, "2026-01-02T14:30Z", 15, 100, 112)
        bar(a, "2026-01-02T14:45Z", 15, 112, 105)
        bar(a, "2026-01-02T15:00Z", 15, 105, 106, wick=112)
        r = outputs(a)
        self.assertIn(r[("15m", "intrabar")]["status"], model.base.ACTIVE)
        self.assertEqual(r[("15m", "body_close")]["status"], "no_second_push")

    def test_intrabar_signal_does_not_depend_on_later_close_or_trade_outcome(self):
        a = setup()
        r = outputs(a)[("15m", "intrabar")]
        stamp = pd.Timestamp(r["entry_timestamp_ny"]).value//1_000_000
        a[1][a[0] > stamp] = 104.  # Final second body no longer contains VAH.
        changed = outputs(a)[("15m", "intrabar")]
        for key in ("entry", "entry_timestamp_ny", "signal_timestamp_ny", "signal_agg_trade_id", "touch_edge"):
            self.assertEqual(r[key], changed[key])

    def test_utc_candle_crossing_ny_open_preserves_original_open(self):
        a = path()
        bar(a, "2026-01-02T12:00Z", 240, 100, 112)
        bar(a, "2026-01-02T16:00Z", 240, 112, 105)
        bar(a, "2026-01-02T20:00Z", 240, 105, 111)
        bar(a, "2026-01-03T00:00Z", 240, 109, 100)
        r = outputs(a)[("4h", "body_close")]
        self.assertEqual(r["first_push_start_ny"], "2026-01-02T07:00:00-05:00")
        self.assertGreaterEqual(r["first_push_touch_ny"], "2026-01-02T09:30:00-05:00")
        self.assertEqual(r["entry_timestamp_ny"], "2026-01-02T19:00:00-05:00")

    def test_pre_ny_only_body_contact_is_not_first_push(self):
        a = path()
        bar(a, "2026-01-02T12:00Z", 90, 115, 105)
        start = pd.Timestamp("2026-01-02T13:30Z").value//1_000_000
        end = pd.Timestamp("2026-01-02T16:00Z").value//1_000_000
        a[1][(a[0] >= start) & (a[0] < end)] = 105.
        r = outputs(a)[("4h", "intrabar")]
        self.assertEqual(r["status"], "no_first_body_push")

    def test_pullback_must_be_inside_and_closer_to_poc(self):
        a = path()
        bar(a, "2026-01-02T14:30Z", 15, 100, 111)
        bar(a, "2026-01-02T14:45Z", 15, 111, 95)  # Inside, but farther from POC 108.
        cfg_builder = lambda frame, **kwargs: dict(fixed_profile(frame), poc_price=108.)
        r = outputs(a, cfg_builder)[("15m", "intrabar")]
        self.assertEqual(r["status"], "no_completed_pullback")

    def test_final_partial_utc_candle_only_supports_intrabar(self):
        a = path()
        bar(a, "2026-01-02T12:00Z", 240, 100, 112)
        bar(a, "2026-01-02T16:00Z", 240, 112, 105)
        bar(a, "2026-01-03T12:00Z", 150, 105, 112)
        r = outputs(a)
        self.assertIn(r[("4h", "intrabar")]["status"], model.base.ACTIVE)
        self.assertEqual(r[("4h", "body_close")]["status"], "no_second_push")

    def test_first_invalid_fill_consumes_opportunity(self):
        a = setup()
        # Confirmed candle ends with the next entry print already at POC.
        idx = np.searchsorted(a[0], pd.Timestamp("2026-01-02T15:15Z").value//1_000_000)
        a[1][idx] = 100.
        bar(a, "2026-01-02T15:30Z", 15, 105, 111)
        r = outputs(a)[("15m", "body_close")]
        self.assertEqual(r["status"], "poc_wrong_side")
        self.assertEqual(r["entry_timestamp_ny"], "2026-01-02T10:15:00-05:00")

    def test_cached_levels_are_reused_without_calling_builder(self):
        a = setup()
        cached = dict(poc=100., val=90., vah=110., profile_volume=1440.)
        builder = Mock(side_effect=AssertionError("Do not rebuild cached profile"))
        r = outputs(a, builder, cached)
        self.assertEqual(r[("15m", "body_close")]["entry"], 109.)
        builder.assert_not_called()
        pivot = np.searchsorted(a[0], model.base.bounds(DAY)[1])
        a[3][pivot:] += 1
        broken = outputs(a, builder, cached)
        self.assertEqual(broken[("15m", "body_close")]["status"], "opening_aggregate_id_gap")

    def test_outside_opening_skips_all_ten_variants(self):
        a = path()
        pivot = np.searchsorted(a[0], model.base.bounds(DAY)[1])
        a[1][pivot] = 115.
        r = outputs(a)
        self.assertEqual(len(r), 10)
        self.assertTrue(all(row["status"] == "open_not_inside_va" for row in r.values()))

    def test_two_edges_in_one_first_body_follow_observed_trade_order(self):
        a = path()
        bar(a, "2026-01-02T14:30Z", 15, 100, 105)
        bar(a, "2026-01-02T14:45Z", 15, 85, 115)
        r = outputs(a)[("15m", "intrabar")]
        self.assertEqual(r["touch_edge"], "val")

    def test_intrabar_next_id_can_fill_at_same_millisecond(self):
        a = setup()
        original = outputs(a)[("15m", "intrabar")]
        stamp = pd.Timestamp(original["signal_timestamp_ny"]).value//1_000_000
        i = np.searchsorted(a[0], stamp)
        a[0][i+1] = a[0][i]
        r = outputs(a)[("15m", "intrabar")]
        self.assertEqual(r["entry_timestamp_ny"], r["signal_timestamp_ny"])
        self.assertGreater(r["entry_agg_trade_id"], r["signal_agg_trade_id"])

    def test_confirmed_signal_at_period_end_has_no_fill(self):
        a = path()
        bar(a, "2026-01-02T14:30Z", 15, 100, 112)
        bar(a, "2026-01-02T14:45Z", 15, 112, 105)
        bar(a, "2026-01-03T14:15Z", 15, 105, 111)
        r = outputs(a)
        self.assertIn(r[("15m", "intrabar")]["status"], model.base.ACTIVE)
        self.assertEqual(r[("15m", "body_close")]["status"], "no_fill_before_period_end")

    def test_calendar_fold_reporting_purges_overlapping_train_outcomes(self):
        template = outputs(setup())[("15m", "body_close")]
        rows = []
        for stamp in pd.date_range("2022-01-01", "2023-02-28"):
            next_day = stamp+pd.Timedelta(days=1)
            rows.append(dict(template, symbol="BTCUSDC", session_date=str(stamp.date()),
                              status="time_exit", gross_r=.1, net_r=.09,
                              exit_timestamp_ny=str(next_day.date())+"T09:29:00-05:00"))
        with tempfile.TemporaryDirectory(dir=Path(model.__file__).parent) as folder:
            out = Path(folder)
            model.report(pd.DataFrame(rows), CFG, out)
            f = pd.read_csv(out/"fold_metrics.csv")
            self.assertEqual(len(f), 6)
            self.assertTrue(f.purged_train_outcomes.eq(1).all())
            expected = len(pd.date_range("2022-09-01", "2023-02-28"))
            self.assertEqual(f.loc[f.phase.eq("test"), "trades"].sum(), expected)
            summary = pd.read_csv(out/"summary.csv")
            self.assertEqual(summary.loc[summary["sample"].eq("walk_forward_test"), "trades"].iloc[0], expected)

    def test_real_v14_cache_matches_config_without_reading_raw_archives(self):
        source = ROOT/"model_development/models/mean_reversion_by_volume_profile/runs/previous_va_touch_v14"
        snapshot = json.loads((source/"config_snapshot.json").read_text())
        cached, manifest = model.profile_cache(source, snapshot["sources"], CFG)
        self.assertTrue(cached)
        self.assertIn(("BTCUSDC", "2025-01-01"), cached)
        self.assertIn("sessions_sha256", manifest)
        wrong = dict(CFG, profile_bins=30)
        with self.assertRaises(AssertionError):
            model.profile_cache(source, snapshot["sources"], wrong)

    def test_full_cli_four_synthetic_assets_and_resume(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory(dir=Path(model.__file__).parent) as folder:
            folder = Path(folder)
            a = path()
            prices = 100+2*np.sin(np.arange(len(a[0]))*.02)
            cfg = dict(CFG, assets={s:f"{s}.parquet" for s in CFG["assets"]})
            for symbol, relative in cfg["assets"].items():
                pq.write_table(pa.table(dict(timestamp=a[0], price=prices, qty=a[2], agg_trade_id=a[3])), folder/relative, row_group_size=500)
            config = folder/"config.json"
            config.write_text(json.dumps(cfg))
            out = folder/"run"
            cmd = [sys.executable, "-B", str(Path(model.__file__)), "--root", str(folder), "--config", str(config),
                   "--start", str(DAY), "--end", str(DAY), "--output", str(out)]
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            done = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            audit = json.loads((out/"audit.json").read_text())
            self.assertEqual(audit["status"], "passed")
            self.assertEqual(audit["combinations"], 40)
            self.assertEqual(audit["rows"], 40)
            summary = pd.read_csv(out/"summary.csv")
            self.assertEqual(len(summary), 80)
            checkpoints = {p.name:p.read_bytes() for p in out.glob("*_daily.jsonl")}
            done = subprocess.run(cmd+["--resume"], capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(checkpoints, {p.name:p.read_bytes() for p in out.glob("*_daily.jsonl")})
            self.assertEqual(json.loads((out/"audit.json").read_text()), audit)
            cfg["fee_bps_per_side"] = 5
            config.write_text(json.dumps(cfg))
            done = subprocess.run(cmd+["--resume"], capture_output=True, text=True, env=env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("Resume inputs changed", done.stderr)


if __name__ == "__main__":
    unittest.main()
