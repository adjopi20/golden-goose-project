import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

import observe_half_pullback as model
import test_second_push as fixture


def source_rows(arrays):
    return {r["timeframe"]:r for r in fixture.model.analyse(fixture.DAY, arrays, fixture.CFG, fixture.fixed_profile)
            if r["entry_mode"] == "body_close" and r["timeframe"] in ("15m", "30m", "1h")}


def result(arrays, cached=None):
    rows = model.analyse(fixture.DAY, arrays, cached or source_rows(arrays), fixture.CFG)
    return {(r["timeframe"], r["pullback_mode"]):r for r in rows}


class HalfPullbackTests(unittest.TestCase):
    def test_exact_half_and_close_confirmed_next_print(self):
        rows = result(fixture.setup())
        self.assertEqual(len(rows), 6)
        for mode in model.MODES:
            r = rows[("15m", mode)]
            self.assertEqual(r["entry_timestamp_ny"], "2026-01-02T10:15:00-05:00")
            self.assertEqual(r["entry"], 109.)
            self.assertEqual(r["half_level"], 105.)
            self.assertEqual(r["pullback_depth_fraction"], .5)
            self.assertTrue(r["same_entry_as_v17"])
            self.assertEqual(r["tp"], 100.)
            self.assertEqual(r["sl"], 118.)
            self.assertAlmostEqual(r["net_r"], r["gross_r"]-.0004*(r["entry"]+r["exit_price"])/r["risk_price"])

    def test_wick_half_qualifies_when_close_does_not(self):
        a = fixture.setup()
        fixture.bar(a, "2026-01-02T14:45Z", 15, 112, 108, wick=104)
        rows = result(a)
        self.assertIn(rows[("15m", "wick_half")]["status"], model.base.ACTIVE)
        self.assertNotIn(rows[("15m", "close_half")]["status"], model.base.ACTIVE)
        self.assertGreater(rows[("15m", "wick_half")]["pullback_depth_fraction"], .5)

    def test_shallow_pullback_cannot_enter_early(self):
        a = fixture.setup()
        fixture.bar(a, "2026-01-02T14:45Z", 15, 112, 106)
        source = source_rows(a)
        self.assertIn(source["15m"]["status"], model.base.ACTIVE)
        for mode in model.MODES:
            self.assertNotIn(result(a, source)[("15m", mode)]["status"], model.base.ACTIVE)

    def test_wick_only_first_push_does_not_count(self):
        a = fixture.path()
        fixture.bar(a, "2026-01-02T14:30Z", 15, 100, 105, wick=112)
        for row in result(a).values():
            self.assertEqual(row["status"], "no_first_body_push")

    def test_wick_only_retest_does_not_count(self):
        a = fixture.setup()
        fixture.bar(a, "2026-01-02T15:00Z", 15, 105, 106, wick=112)
        for mode in model.MODES:
            self.assertNotIn(result(a)[("15m", mode)]["status"], model.base.ACTIVE)

    def test_same_candle_cannot_supply_pullback_and_retest(self):
        a = fixture.path()
        fixture.bar(a, "2026-01-02T14:30Z", 15, 100, 112)
        fixture.bar(a, "2026-01-02T14:45Z", 15, 112, 105)
        for mode in model.MODES:
            self.assertNotIn(result(a)[("15m", mode)]["status"], model.base.ACTIVE)

    def test_pullback_close_must_remain_inside(self):
        a = fixture.setup()
        fixture.bar(a, "2026-01-02T14:45Z", 15, 112, 112, wick=104)
        for mode in model.MODES:
            self.assertNotIn(result(a)[("15m", mode)]["status"], model.base.ACTIVE)

    def test_long_mirrors_short(self):
        a = fixture.setup()
        a[1][:] = 200-a[1]
        for mode in model.MODES:
            r = result(a)[("15m", mode)]
            self.assertEqual(r["touch_edge"], "val")
            self.assertEqual(r["side"], 1)
            self.assertEqual(r["half_level"], 95.)
            self.assertEqual(r["entry"], 91.)
            self.assertEqual(r["sl"], 82.)

    def test_poc_reached_before_retest_is_allowed(self):
        a = fixture.setup()
        fixture.bar(a, "2026-01-02T14:45Z", 15, 112, 99)
        for mode in model.MODES:
            self.assertIn(result(a)[("15m", mode)]["status"], model.base.ACTIVE)

    def test_future_prices_cannot_change_entry(self):
        a = fixture.setup()
        before = result(a)[("15m", "close_half")]
        cutoff = model.ms(before["entry_timestamp_ny"])
        a[1][a[0] > cutoff] = 115.
        after = result(a)[("15m", "close_half")]
        for field in ("entry_timestamp_ny", "entry_agg_trade_id", "entry", "signal_timestamp_ny", "pullback_available_ny"):
            self.assertEqual(before[field], after[field])

    def test_same_entry_outcome_mismatch_is_rejected(self):
        a = fixture.setup()
        cached = source_rows(a)
        cached["15m"]["net_r"] += 1
        with self.assertRaises(AssertionError):
            result(a, cached)

    def test_invalid_first_fill_consumes_opportunity(self):
        a = fixture.setup()
        fixture.bar(a, "2026-01-02T15:15Z", 15, 99, 100)
        fixture.bar(a, "2026-01-02T15:30Z", 15, 105, 112)
        for mode in model.MODES:
            r = result(a)[("15m", mode)]
            self.assertNotIn(r["status"], model.base.ACTIVE)
            self.assertEqual(r["entry_timestamp_ny"], "2026-01-02T10:15:00-05:00")

    def test_one_hour_preserves_utc_open_before_ny(self):
        a = fixture.path()
        fixture.bar(a, "2026-01-02T14:00Z", 60, 100, 112)
        fixture.bar(a, "2026-01-02T15:00Z", 60, 112, 105)
        fixture.bar(a, "2026-01-02T16:00Z", 60, 105, 111)
        fixture.bar(a, "2026-01-02T17:00Z", 60, 109, 100)
        r = result(a)[("1h", "close_half")]
        self.assertEqual(r["first_push_start_ny"], "2026-01-02T09:00:00-05:00")
        self.assertEqual(r["entry_timestamp_ny"], "2026-01-02T12:00:00-05:00")

    def test_no_entry_at_profile_cutoff(self):
        a = fixture.path()
        fixture.bar(a, "2026-01-02T14:30Z", 15, 100, 112)
        fixture.bar(a, "2026-01-02T14:45Z", 15, 112, 105)
        fixture.bar(a, "2026-01-03T14:15Z", 15, 105, 111)
        for mode in model.MODES:
            self.assertEqual(result(a)[("15m", mode)]["status"], "no_fill_before_period_end")

    def test_epoch_precision_is_explicit(self):
        self.assertEqual(model.ms("2026-01-02T09:30:00.123000-05:00"), 1767364200123)

    def test_reader_matches_existing_raw_window_and_reuses_groups(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(Path.cwd().resolve()))
            path = Path(folder)/"raw.parquet"
            pd.DataFrame(dict(timestamp=np.arange(12), price=np.arange(12)+100., qty=1., agg_trade_id=np.arange(12))).to_parquet(path, row_group_size=3)
            reader = model.WindowReader(path)
            f, mins, maxs = model.base.metadata(path)
            for start, end in ((2, 8), (3, 7), (0, 1), (50, 60)):
                actual = reader.window(start, end)
                expected = model.base.load_window(f, mins, maxs, start, end)
                for a, b in zip(actual, expected):
                    np.testing.assert_array_equal(a, b)
            before = reader.groups_read
            reader.window(2, 8)
            self.assertEqual(reader.groups_read, before)
            reader.close()
            f.close()

    def test_cli_synthetic_run_resume_and_source_integrity(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(Path.cwd().resolve()))
            source = root/"source"
            source.mkdir()
            a = fixture.setup()
            cfg = json.loads(json.dumps(fixture.CFG))
            all_rows, sources = [], {}
            for symbol in cfg["assets"]:
                path = root/(symbol+".parquet")
                cfg["assets"][symbol] = path.name
                pd.DataFrame(dict(timestamp=a[0], price=a[1], qty=a[2], agg_trade_id=a[3])).to_parquet(path)
                stat = path.stat()
                sources[symbol] = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
                for row in fixture.model.analyse(fixture.DAY, a, cfg, fixture.fixed_profile):
                    all_rows.append(dict(row, symbol=symbol))
            frame = pd.DataFrame(all_rows)
            frame.to_csv(source/"sessions.csv", index=False)
            fixture.model.report(frame, cfg, source)
            snap = dict(rule_version="second_push_utc_v17_1", config=cfg, sources=sources,
                        code_sha256=model.base.sha(model.old.__file__), helpers={})
            (source/"config_snapshot.json").write_text(json.dumps(snap))
            source_audit = dict(status="passed", outputs={p.name:model.base.sha(p) for p in source.iterdir()})
            (source/"audit.json").write_text(json.dumps(source_audit))
            command = [sys.executable, model.__file__, "--root", str(root), "--source", str(source), "--output", str(root/"run")]
            for extra in ([], ["--resume"]):
                completed = subprocess.run(command+extra, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                audit = json.loads((root/"run/audit.json").read_text())
                self.assertEqual(audit["status"], "passed")
                self.assertEqual(audit["rows"], 24)
                self.assertEqual(audit["profiles_built"], 0)
            with (source/"sessions.csv").open("a") as handle:
                handle.write("\n")
            completed = subprocess.run(command+["--resume"], capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Changed v17 artifact", completed.stderr)


if __name__ == "__main__":
    unittest.main()
