"""Deterministic execution, causality, accounting and synthetic end-to-end tests."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

import backtest as b


def profile(*args, **kwargs):
    return dict(poc_price=100., val=95., vah=105., total_volume=1000.)


def arrays(day, opening=110., steps=None):
    start, pivot, end = b.bounds(day)
    ts = np.arange(start, end, 60_000, dtype=np.int64)
    p = np.full(len(ts), 100.)
    at = int(np.searchsorted(ts, pivot))
    p[at:] = opening
    steps = steps if steps is not None else [110., 106., 104., 102., 99.]
    p[at:at+len(steps)] = steps
    return [ts, p, np.ones(len(ts)), np.arange(len(ts), dtype=np.int64)]


class BacktestTests(unittest.TestCase):
    def test_exact_duplicates_preserve_unique_prints_and_original_order(self):
        a = [np.array(x) for x in ([1000, 1000, 1000, 1000, 2000],
             [100., 101., 100., 101., 102.], [1., 2., 1., 2., 3.], [1, 2, 1, 2, 3])]
        cleaned, dropped = b.unique_trades(a)
        self.assertEqual(dropped, 2)
        np.testing.assert_array_equal(cleaned[3], [1, 2, 3])
        np.testing.assert_array_equal(cleaned[1], [100., 101., 102.])
        distinct = [np.array(x) for x in ([1000, 1000], [100., 100.], [1., 1.], [1, 2])]
        self.assertEqual(b.unique_trades(distinct)[1], 0)
        backwards = [np.array(x) for x in ([1000, 1000], [100., 100.], [1., 1.], [2, 1])]
        kept, _ = b.unique_trades(backwards)
        np.testing.assert_array_equal(kept[3], [2, 1])
        with self.assertRaises(ValueError):
            b.quality(kept, 0, 3000, boundaries=False)

    def test_conflicting_duplicate_ids_are_not_repaired_silently(self):
        for conflicting in (0, 1, 2):
            a = [np.array(x) for x in ([1000, 1000], [100., 100.], [1., 1.], [5, 5])]
            a[conflicting][1] += 1
            with self.assertRaisesRegex(ValueError, "Conflicting repeated aggregate ID 5"):
                b.unique_trades(a)

    def test_only_known_original_code_can_upgrade_identical_resume_config(self):
        current = dict(code_sha256="patched", data_preparation=b.DATA_PREPARATION,
                       rules=dict(b.RULES), sources={"pair": "unchanged"}, environment={"python": "same"})
        previous = {k: v for k, v in current.items() if k != "data_preparation"}
        previous["code_sha256"] = b.LEGACY_CODE_SHA256
        self.assertTrue(b.compatible_resume(previous, current))
        self.assertFalse(b.compatible_resume(current, current))
        for altered in (dict(previous, code_sha256="unknown"), dict(previous, sources={"pair": "changed"}),
                        dict(previous, rules=dict(b.RULES, fee_bps_per_side=0)),
                        dict(previous, environment={"python": "different"})):
            with self.assertRaises(ValueError):
                b.compatible_resume(altered, current)

    def test_entry_actual_print_target_fee_and_no_preopen_cross(self):
        day = pd.Timestamp("2025-01-05").date()
        result = b.analyse(day, arrays(day), profile)
        self.assertEqual(result["status"], "tp_first")
        self.assertEqual(result["entry"], 104.)
        self.assertEqual(result["exit_price"], 99.)
        self.assertEqual(result["sl"], 108.)
        self.assertAlmostEqual(result["gross_r"], 1.25)
        self.assertAlmostEqual(result["net_r"], 1.25-.0004*(104+99)/4)
        self.assertTrue(result["location"])
        a = arrays(day, opening=106., steps=[106., 106., 106.])
        self.assertEqual(b.analyse(day, a, profile)["status"], "no_selected_edge_touch")

    def test_long_stop_first_time_exit_and_invalid_first_candidate(self):
        day = pd.Timestamp("2025-01-05").date()
        long = b.analyse(day, arrays(day, 90., [90., 94., 96., 98., 101.]), profile)
        self.assertEqual((long["side"], long["entry"], long["status"]), (1, 96., "tp_first"))
        stopped = b.analyse(day, arrays(day, 110., [110., 104., 109., 99.]), profile)
        self.assertEqual(stopped["status"], "sl_first")
        invalid = b.analyse(day, arrays(day, 110., [110., 99., 104., 99.]), profile)
        self.assertEqual(invalid["status"], "invalid_first_touch_geometry")
        timed = b.analyse(day, arrays(day, 104., [104., 105., 104.]), profile)
        self.assertEqual(timed["status"], "time_exit")
        self.assertFalse(timed["location"])

    def test_boundary_location_and_dst(self):
        day = pd.Timestamp("2025-01-05").date()
        r = b.analyse(day, arrays(day, 105., [105., 104., 99.]), profile)
        self.assertFalse(r["location"])
        self.assertEqual(r["entry"], 105.)
        for text, hours in (("2025-03-08", 23), ("2025-11-01", 25)):
            _, start, end = b.bounds(pd.Timestamp(text).date())
            self.assertEqual((end-start)/3_600_000, hours)

    def test_data_quality_fails_duplicates_and_marks_gaps(self):
        a = arrays(pd.Timestamp("2025-01-05").date())
        a[3][30:] += 1
        self.assertEqual(b.quality(a, a[0][0], a[0][-1]+60_000), "aggregate_id_gaps")
        a[3][31] = a[3][30]
        with self.assertRaises(ValueError):
            b.quality(a, a[0][0], a[0][-1]+60_000)

    def test_invalid_daily_prices_remain_reportable(self):
        start = b.stamp(pd.Timestamp("2025-01-01", tz="UTC"))
        ts = np.arange(start, start+b.DAY, 60_000, dtype=np.int64)
        prices = np.full(len(ts), 100.)
        prices[10] = np.nan
        row = b.daily_bar([ts, prices, np.ones(len(ts)), np.arange(len(ts))],
                          start, start, start+b.DAY-1, None)
        self.assertFalse(row["valid"])
        self.assertEqual(row["quality"], "invalid_price_or_volume")
        json.dumps(row, allow_nan=False)

    def test_atr_requires_31_daily_bars_and_invalid_bar_breaks_window(self):
        from indicator.trend import true_range
        start = b.stamp(pd.Timestamp("2024-01-01", tz="UTC"))
        bars = pd.DataFrame(dict(start_ms=start+np.arange(70)*b.DAY,
            available_ms=start+(np.arange(70)+1)*b.DAY, valid=True,
            open=100., high=102., low=98., close=100.))
        s = b.atr_states(bars, true_range)
        self.assertTrue(s.atr30_pct.iloc[:30].isna().all())
        self.assertAlmostEqual(s.atr30_pct.iloc[30], 4.)
        bars.loc[35, "valid"] = False
        s = b.atr_states(bars, true_range)
        self.assertTrue(s.atr30_pct.iloc[35:66].isna().all())
        self.assertAlmostEqual(s.atr30_pct.iloc[66], 4.)

    def test_regime_causality_warmup_and_entry_timestamp_block(self):
        available = pd.date_range("2023-12-01", "2025-05-01", tz="UTC", freq="D")
        states = pd.DataFrame(dict(available_ms=[b.stamp(t) for t in available],
                                  atr30_pct=np.arange(len(available), dtype=float)))
        entries = pd.DataFrame(dict(entry_ms=[b.stamp(pd.Timestamp("2025-01-01T05:01:00Z")),
                                               b.stamp(pd.Timestamp("2025-03-01T05:01:00Z")),
                                               b.stamp(pd.Timestamp("2024-01-02T15:00:00Z"))],
                                    session_date=["2024-12-31", "2025-02-28", "2024-01-02"]))
        first = b.stamp(pd.Timestamp("2023-12-01", tz="UTC"))
        d, cuts = b.add_regimes(entries, states, first)
        self.assertEqual(d.atr_block_start.iloc[0], "2025-01-01")
        self.assertFalse(d.atr_ready.iloc[2])
        train = states.loc[states.available_ms.ge(b.ny_midnight("2024-05-01")) &
                           states.available_ms.lt(b.ny_midnight("2025-01-01")), "atr30_pct"]
        self.assertAlmostEqual(d.atr_high_cut.iloc[0], train.quantile(2/3))
        altered = states.copy()
        altered.loc[altered.available_ms.gt(entries.entry_ms.iloc[0]), "atr30_pct"] = 10_000.
        d2, _ = b.add_regimes(entries, altered, first)
        self.assertEqual(d.atr_high.iloc[0], d2.atr_high.iloc[0])
        self.assertEqual(d.atr_high_cut.iloc[0], d2.atr_high_cut.iloc[0])

    def test_sizing_accounting_cap_membership_and_case_duplicates(self):
        d = pd.DataFrame(dict(session_date=["2025-01-01", "2025-01-02", "2025-01-03"],
            distance=[True, False, True], location=[True, True, False], atr_high=[True, False, True],
            atr_ready=True, risk_price=[2., .01, 2.], entry=100., gross_r=[1., -1., 1.],
            fee_r=[.0404, 7.9996, .0404], net_r=[.9596, -8.9996, .9596],
            side=[1, 1, 1], exit_price=[102., 99.99, 102.], entry_ms=[100, 300, 500],
            exit_ms=[200, 400, 600], opening_ms=[90, 290, 490], end_ms=[250, 450, 650],
            status=["tp_first", "sl_first", "tp_first"], distance_pct=[2., .01, 2.]))
        for _, parent, condition, mult in b.scenarios():
            ledger = b.simulate(d, parent, condition, mult)
            b.validate_ledger(ledger)
            self.assertTrue(ledger.notional_equity.le(5+1e-12).all())
        x = b.simulate(d, "union", "location", 2.)
        self.assertEqual(len(x), 3)
        self.assertAlmostEqual(x.actual_risk_fraction.iloc[1], .0005)
        self.assertAlmostEqual(x.actual_size_multiple.iloc[1], 1.)
        a = b.simulate(d, "intersection", "atr_high", 2.)
        c = b.simulate(d, "intersection", "location_and_atr", 2.)
        pd.testing.assert_frame_equal(a, c)

    def test_multifile_archive_overlap_resume_and_full_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ts_parts, price_parts = [], []
            first_day = pd.Timestamp("2024-01-01").date()
            for n in range(340):
                _, start, end = b.bounds(first_day+pd.Timedelta(days=n))
                ts = np.arange(start, end, 60_000, dtype=np.int64)
                amplitude = 8+4*np.sin(n/13) if n < 290 else 18.
                ts_parts.append(ts)
                price_parts.append(100+amplitude*np.cos(2*np.pi*np.arange(len(ts))/len(ts)))
            ts, price = np.concatenate(ts_parts), np.concatenate(price_parts)
            for pair in ("ltcusdc", "ltcusdt"):
                folder = root/"storage"/pair
                folder.mkdir(parents=True)
                raw = pd.DataFrame(dict(timestamp=ts, price=price, qty=np.ones(len(ts)),
                                        agg_trade_id=np.arange(len(ts), dtype=np.int64)))
                if pair == "ltcusdt":
                    # Duplicate the final 40 synthetic sessions, including across
                    # row-group boundaries, without changing unique transactions.
                    raw = raw.iloc[np.repeat(np.arange(len(raw)), 1+(np.arange(len(raw)) >= 300*1440))]
                # Two disjoint files exercise folder-wide input handling.
                raw.iloc[:len(raw)//2].to_parquet(folder/"a.parquet", row_group_size=65536, index=False)
                raw.iloc[len(raw)//2:].to_parquet(folder/"b.parquet", row_group_size=65536, index=False)
                a = b.Archive(folder)
                try:
                    window = a.window(ts[80], ts[180])
                    np.testing.assert_array_equal(window[0], ts[80:180])
                finally:
                    a.close()
            out = root/"run"
            b.main(["--root", str(root), "--output", str(out)])
            summary = pd.read_csv(out/"summary.csv")
            self.assertEqual(len(summary), 28)
            self.assertTrue(summary.trades.gt(0).all())
            self.assertTrue(summary.atr_ready_trades.gt(0).all())
            self.assertTrue(summary.atr_unavailable_trades.gt(0).all())
            self.assertTrue(summary.loc[summary.scenario.eq("case3_x2"), "boost_condition_trades"].gt(0).all())
            self.assertEqual(json.loads((out/"audit.json").read_text())["status"], "passed")
            counts = json.loads((out/"audit.json").read_text())["source_counts"]
            self.assertEqual(counts["LTCUSDC"]["exact_duplicates_removed"], 0)
            self.assertEqual(counts["LTCUSDT"]["exact_duplicates_removed"], 57600)
            self.assertEqual(counts["LTCUSDT"]["raw_rows"], 547200)
            self.assertEqual(counts["LTCUSDT"]["used_rows"], 489600)
            pd.testing.assert_series_equal(
                summary.loc[summary.symbol.eq("LTCUSDC"), "net_account_return_pct"].reset_index(drop=True),
                summary.loc[summary.symbol.eq("LTCUSDT"), "net_account_return_pct"].reset_index(drop=True))
            hashes = {name: b.sha(out/name) for name in ("summary.csv", "period_metrics.csv", "REPORT.md")}
            # Recreate a legacy run interrupted before the first duplicate day.
            snapshot = json.loads((out/"config_snapshot.json").read_text())
            snapshot.pop("data_preparation")
            snapshot["code_sha256"] = b.LEGACY_CODE_SHA256
            (out/"config_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
            for pair in ("LTCUSDC", "LTCUSDT"):
                path = out/(pair+"_checkpoint.jsonl")
                records = [json.loads(line) for line in path.read_text().splitlines()]
                if pair == "LTCUSDT":
                    cutoff = b.stamp(pd.Timestamp(first_day+pd.Timedelta(days=300)).tz_localize("UTC"))
                    records = [r for r in records if r["utc_start_ms"] < cutoff]
                for r in records:
                    r["daily"].pop("raw_observations", None)
                    r["daily"].pop("exact_duplicates_removed", None)
                path.write_text("".join(json.dumps(r)+"\n" for r in records), encoding="utf-8")
            # Resume safely repairs only an unfinished last checkpoint line.
            with (out/"LTCUSDT_checkpoint.jsonl").open("ab") as f:
                f.write(b'{"partial":')
            b.main(["--root", str(root), "--output", str(out), "--resume"])
            self.assertEqual(hashes, {name: b.sha(out/name) for name in hashes})
            self.assertEqual(json.loads((out/"config_snapshot.original_v31.json").read_text()), snapshot)
            archive = root/"storage"/"ltcusdt"
            import shutil
            shutil.copyfile(archive/"a.parquet", archive/"duplicate.parquet")
            with self.assertRaises(ValueError):
                b.Archive(archive)
            with self.assertRaises(ValueError):
                b.main(["--root", str(root), "--output", str(out), "--resume"])

    def test_reports_preserve_both_pairs_and_zero_trade_scenarios(self):
        columns = ("symbol", "session_date", "location", "distance", "entry_ms", "exit_ms", "risk_price", "entry",
                   "gross_r", "net_r", "fee_r", "distance_pct", "atr_high", "atr_ready", "status", "entry_agg_trade_id")
        trades = pd.DataFrame({c: pd.Series(dtype=bool if c in ("location", "distance", "atr_high", "atr_ready") else float)
                               for c in columns})
        sessions = pd.DataFrame(dict(symbol=["LTCUSDC", "LTCUSDT"], session_date=["2025-01-01"]*2, valid_session=True, status="no_touch"))
        with tempfile.TemporaryDirectory() as tmp:
            summary, steps = b.reports(sessions, trades, Path(tmp))
            self.assertEqual(len(summary), 28)
            self.assertEqual(steps, 0)
            self.assertTrue(summary.trades.eq(0).all())


if __name__ == "__main__":
    # Tests use the actual repository indicator implementations, with synthetic raw data.
    import sys
    p = __import__("argparse").ArgumentParser()
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    args, remaining = p.parse_known_args()
    sys.path.insert(0, str(args.root.resolve()))
    unittest.main(argv=[__file__]+remaining)
