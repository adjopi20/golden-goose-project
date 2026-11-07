import unittest
import pandas as pd

from monthly_sd_reversion import MonthlySDReversion, monthly_features, complete_month_window, observe_main
from utils.closed_candle_context import CandleNotReady


class MonthlySDTests(unittest.TestCase):
    def window(self, start, end, source_start="2024-04-11", source_end="2026-09-30T23:59:54Z"):
        def ms(value):
            t=pd.Timestamp(value)
            return int((t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")).timestamp()*1000)
        return complete_month_window(start,end,ms(source_start),ms(source_end))

    def test_incomplete_months_skip_and_record(self):
        result=self.window("2024-04-12","2026-09-30")
        self.assertEqual(result["effective_start_utc"],"2024-05-01T00:00:00+00:00")
        self.assertEqual(result["effective_end_exclusive_utc"],"2026-09-01T00:00:00+00:00")
        self.assertEqual(len(result["excluded_intervals"]),2)
        self.assertEqual(result["requested_start_utc"],"2024-04-12T00:00:00+00:00")

    def test_aligned_months_unchanged_and_timezone_normalized(self):
        result=self.window("2024-05-01T07:00:00+07:00","2026-09-01")
        self.assertEqual(result["effective_start_utc"],"2024-05-01T00:00:00+00:00")
        self.assertEqual(result["excluded_intervals"],[])

    def test_source_limits_and_empty_window(self):
        result=self.window("2024-04-01","2026-10-01")
        self.assertEqual(result["effective_start_utc"],"2024-05-01T00:00:00+00:00")
        self.assertEqual(result["effective_end_exclusive_utc"],"2026-09-01T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError,"No complete UTC month"):
            self.window("2024-04-12","2024-04-30")
        with self.assertRaisesRegex(ValueError,"after start"):
            self.window("2024-05-01","2024-04-01")

    def test_model_launcher_records_dates_without_extra_files(self):
        from pathlib import Path
        import tempfile
        import json
        import pyarrow as pa
        import pyarrow.parquet as pq
        core=Path(__import__('utils.closed_candle_context',fromlist=['x']).__file__).parents[1]
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            base=Path(directory)
            path=base/'LTCUSDC-aggTrades-synthetic.parquet'
            dates=pd.date_range('2025-01-12','2025-03-20',freq='1D',tz='UTC')
            pq.write_table(pa.table(dict(timestamp=dates.as_unit('ms').asi8,
                price=[100.]*len(dates),qty=[1.]*len(dates),agg_trade_id=list(range(len(dates))))),path)
            out=base/'run'
            metadata=observe_main(['--shared-core',str(core),'--input',str(path),
                '--start','2025-01-12','--end','2025-03-20','--risk','10',
                '--venue','synthetic','--inspection-status','development','--output',str(out)])
            self.assertEqual(metadata['status'],'complete')
            self.assertEqual(metadata['iteration_id'],'ITER-004')
            self.assertEqual(metadata['configuration']['observation_window']['effective_start_utc'],'2025-02-01T00:00:00+00:00')
            self.assertEqual(metadata['configuration']['observation_window']['effective_end_exclusive_utc'],'2025-03-01T00:00:00+00:00')
            self.assertEqual(metadata['entry_fee_bps'],4.)
            self.assertEqual(sorted(p.name for p in out.iterdir()),['run.json','trades.parquet'])
            self.assertEqual(pq.read_table(out/'trades.parquet').num_rows,0)

    def snapshot(self, close=70, boundary=86400000):
        return dict(feature_as_of_ms=boundary, feature_available_at_ms=boundary,
                    decision_timestamp_ms=boundary+50, entry_eligible_timestamp_ms=boundary+50,
                    bar={"close": close}, indicators={"monthly": dict(vwap=100, std=10,
                    lower_1=90, lower_2=80, lower_3=70, upper_1=110, upper_2=120, upper_3=130)})

    def test_independent_levels_and_actual_fill_rr(self):
        model = MonthlySDReversion()
        self.assertEqual(len(model._decide(self.snapshot())), 3)
        for level in (1, 2, 3):
            position = model.accept_fill(level, price=72, timestamp_ms=86400051)
            self.assertEqual(position["stop"], 44)
            self.assertEqual(position["target"], 100)
        self.assertEqual(model._decide(self.snapshot(boundary=172800000)), [])
        record = model.record_exit(2, price=100, timestamp_ms=172800001, reason="tp")
        self.assertEqual(record["gross_R"], 1)
        self.assertEqual([len(model.records[i]) for i in (1, 2, 3)], [0, 1, 0])
        signals = model._decide(self.snapshot(boundary=259200000))
        self.assertEqual([s["sd"] for s in signals], [2])

    def test_only_qualifying_position_blocks_are_recorded(self):
        model = MonthlySDReversion()
        model._decide(self.snapshot())
        p = model.accept_fill(1, price=72, timestamp_ms=86400051)
        model._decide(self.snapshot(close=95, boundary=172800000))
        self.assertEqual(model.position_blocks, [])
        signals = model._decide(self.snapshot(boundary=259200000))
        self.assertEqual([s["sd"] for s in signals], [2, 3])
        self.assertEqual(len(model.position_blocks), 1)
        block = model.position_blocks[0]
        self.assertEqual(block["blocking_setup_id"], p["setup_id"])
        self.assertEqual(block["blocking_entry_timestamp_ms"], 86400051)
        self.assertEqual(block["record_type"], "position_block")
        self.assertNotIn("gross_R", block)
        model._decide(self.snapshot(boundary=259200000))
        self.assertEqual(len(model.position_blocks), 1)

    def test_short_and_duplicate_boundary(self):
        model = MonthlySDReversion()
        self.assertEqual(len(model._decide(self.snapshot(close=130))), 3)
        self.assertEqual(model._decide(self.snapshot(close=130)), [])
        p = model.accept_fill(3, price=132, timestamp_ms=86400051)
        self.assertEqual((p["stop"], p["target"]), (164, 100))
        r = model.record_exit(3, price=164, timestamp_ms=86400052, reason="sl")
        self.assertEqual(r["gross_R"], -1)

    def test_fill_timing_and_gap_rejection(self):
        model = MonthlySDReversion()
        model._decide(self.snapshot())
        for stamp in (86400000, 172800000):
            with self.assertRaises(ValueError):
                model.accept_fill(1, price=70, timestamp_ms=stamp)
        self.assertIsNone(model.accept_fill(1, price=101, timestamp_ms=86400051))

    def test_close_only_and_zero_dispersion(self):
        model = MonthlySDReversion()
        self.assertEqual(model._decide(self.snapshot(close=95)), [])
        s = self.snapshot(boundary=172800000)
        s["indicators"]["monthly"]["std"] = 0
        self.assertEqual(model._decide(s), [])

    def bars(self):
        return pd.DataFrame(dict(open=[100, 100, 100], high=[100, 110, 100],
            low=[100, 90, 10], close=[100, 100, 10], volume=[1, 1, 1]),
            index=pd.date_range("2026-10-01", periods=3, freq="1D", tz="UTC"))

    def test_context_excludes_unfinished_candle_and_delays_entry(self):
        bars = self.bars()
        stamp = int(pd.Timestamp("2026-10-04", tz="UTC").timestamp()*1000)
        before = MonthlySDReversion().evaluate(bars, decision_timestamp_ms=stamp-1)
        self.assertEqual(before["bar"]["close"], 100)
        after = MonthlySDReversion().evaluate(bars, decision_timestamp_ms=stamp+50)
        self.assertEqual(after["bar"]["close"], 10)
        self.assertEqual(after["entry_eligible_timestamp_ms"], stamp+50)
        for s in after["decision"]:
            self.assertEqual(s["entry_eligible_timestamp_ms"], stamp+50)

    def test_partial_month_and_missing_coverage_block(self):
        bars = self.bars()
        with self.assertRaises(CandleNotReady):
            monthly_features(bars.iloc[1:])
        stamp = int(pd.Timestamp("2026-10-04", tz="UTC").timestamp()*1000)
        with self.assertRaises(CandleNotReady):
            MonthlySDReversion().evaluate(bars.iloc[[0, 2]], decision_timestamp_ms=stamp)

    def test_monthly_reset(self):
        bars = pd.DataFrame(dict(open=[10, 20, 30], high=[10, 20, 30],
            low=[10, 20, 30], close=[10, 20, 30], volume=[1, 1, 1]),
            index=pd.to_datetime(["2026-10-01", "2026-10-31", "2026-11-01"], utc=True))
        features = monthly_features(bars)
        self.assertEqual(features.vwap.tolist(), [10, 15, 30])
        self.assertEqual(features["std"].tolist(), [0, 5, 0])


if __name__ == "__main__":
    unittest.main()
