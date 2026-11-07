import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

import observe_push_orderflow as m
import validate_push_orderflow as checks
import test_second_push as fixture
from test_half_pullback import source_rows


def tape(advance=0.):
    ts = np.arange(900, dtype=np.int64)*1000
    prices = np.r_[np.linspace(100.,110.,121),np.full(779,110.)]
    prices[150:240] = 115.  # Earlier advance must not count as progress after the big print.
    prices[301:] += np.linspace(0,advance,599)
    qty = np.ones(900)
    qty[300] = 1000.
    return [ts,prices,qty,np.arange(900,dtype=np.int64),np.zeros(900,dtype=bool)]


def absorption(a):
    b = m.flow_bars(a,0,900000,15)[0]
    return m.absorption_features(a,b,0,"vah",110.,100.)


def events(n=30):
    rows = []
    for i,day in enumerate(pd.date_range("2025-01-01",periods=n)):
        rows.append(dict(symbol="BTCUSDC",session_date=str(day.date()),timeframe="15m",setup="first_push",touch_edge="vah",
            signal_bar_start_ny=day.isoformat(),absorption_calibratable=True,abs_pressure_rate=float(i+1),
            abs_large_notional=float(1000+10*i),abs_progress=float(i/100)))
    return pd.DataFrame(rows)


class PushOrderflowTests(unittest.TestCase):
    def test_progress_starts_after_execution_not_candle_open(self):
        r = absorption(tape())
        self.assertTrue(r["absorption_calibratable"])
        self.assertEqual(r["abs_large_id"],300)
        self.assertEqual(r["abs_progress"],0.)
        self.assertEqual(r["abs_large_notional"],110000.)

    def test_subsequent_continuation_is_measured(self):
        r = absorption(tape(3.))
        self.assertAlmostEqual(r["abs_progress"],.3)

    def test_later_trade_ids_at_same_timestamp_remain_after_execution(self):
        a = tape()
        a[0][301:305] = a[0][300]
        a[1][301] = 112.
        r = absorption(a)
        self.assertEqual(r["abs_large_id"],300)
        self.assertAlmostEqual(r["abs_progress"],.2)

    def test_late_print_without_response_is_not_absorption(self):
        a = tape()
        a[-1][:] = True
        a[-1][-1] = False
        a[2][-1] = 100000.
        r = absorption(a)
        self.assertFalse(r["absorption_observable"])
        self.assertEqual(r["absorption_status"],"insufficient_post_execution_observation")

    def test_largest_eligible_print_is_selected_by_size(self):
        a = tape()
        a[2][500] = 2000.
        a[2][890] = 100000.  # Too late for the declared observation horizon.
        r = absorption(a)
        self.assertEqual(r["abs_large_id"],500)
        self.assertGreaterEqual(r["abs_response_observed_ms"],90000)

    def test_no_outward_pressure_is_observed_absence_not_missing(self):
        a = tape()
        a[-1][:] = True
        r = absorption(a)
        self.assertTrue(r["absorption_observable"])
        self.assertFalse(r["absorption_calibratable"])
        self.assertEqual(r["outward_zone_volume"],0.)
        self.assertNotIn("abs_progress",r)

    def test_val_direction_mirrors_vah(self):
        a = tape(1.)
        up = absorption(a)
        a[1] = 200-a[1]
        a[-1] = ~a[-1]
        b = m.flow_bars(a,0,900000,15)[0]
        down = m.absorption_features(a,b,0,"val",90.,100.)
        self.assertEqual(up["abs_large_id"],down["abs_large_id"])
        self.assertAlmostEqual(up["abs_progress"],down["abs_progress"])
        self.assertEqual(up["outward_zone_volume"],down["outward_zone_volume"])

    def test_pre_open_large_execution_is_excluded(self):
        a = tape()
        a[1][400:500] = 105.
        a[2][300] = 1000000.
        a[2][600] = 1000.
        b = m.flow_bars(a,0,900000,15)[0]
        r = m.absorption_features(a,b,450000,"vah",110.,100.)
        self.assertEqual(r["abs_large_id"],600)

    def test_future_prints_after_signal_cannot_change_feature(self):
        a = tape(1.)
        original = absorption(a)
        b = [np.r_[x, np.array([1000000,2000000]) if i == 0 else np.array([999999,999999],dtype=x.dtype)] for i,x in enumerate(a)]
        self.assertEqual(original,absorption(b))

    def test_exhaustion_requires_price_extremes_and_declining_side_volume(self):
        bars = [dict(start=i*900000,end=(i+1)*900000,complete=True,high=108+i,low=100+i,buy_volume=300-i*100,sell_volume=50.) for i in range(3)]
        r = m.exhaustion_features(bars,2,"vah")
        self.assertTrue(r["exhaustion_signal"])
        bars[2]["buy_volume"] = 250.
        self.assertFalse(m.exhaustion_features(bars,2,"vah")["exhaustion_signal"])

    def test_exhaustion_sell_direction_and_missing_bar(self):
        bars = [dict(start=i*900000,end=(i+1)*900000,complete=True,high=100-i,low=92-i,buy_volume=50.,sell_volume=300-i*100) for i in range(3)]
        self.assertTrue(m.exhaustion_features(bars,2,"val")["exhaustion_signal"])
        bars[2]["start"] += 900000
        self.assertFalse(m.exhaustion_features(bars,2,"val")["exhaustion_observable"])

    def test_calibration_deduplicates_shared_second_push_candles(self):
        a = events()
        repeated = a.assign(setup="second_wick_half")
        gate = m.fit_absorption(pd.concat([a,repeated],ignore_index=True))
        self.assertEqual(gate["train_distinct_events"],30)
        self.assertEqual(gate["pressure_cut"],15.5)
        self.assertEqual(gate["size_cut"],1217.5)
        self.assertEqual(gate["progress_cut"],.10)
        self.assertFalse(m.fit_absorption(a.iloc[:24])["supported"])

    def test_gate_requires_pressure_size_and_low_post_execution_progress(self):
        gate = dict(supported=True,pressure_cut=10.,size_cut=1000.,progress_cut=.05)
        p = pd.DataFrame(dict(absorption_calibratable=[True]*4,abs_pressure_rate=[11,9,11,11],
            abs_large_notional=[1100,1100,900,1100],abs_progress=[.01,.01,.01,.1]))
        self.assertEqual(list(m.apply_absorption(p,gate)),[True,False,False,False])

    def test_first_entry_and_exact_second_entry_outcome_reuse(self):
        a = fixture.setup()
        flow = list(a)+[np.zeros(len(a[0]),dtype=bool)]
        first = source_rows(a)
        second = {(r["timeframe"],r["pullback_mode"]):r for r in m.half.analyse(fixture.DAY,a,first,fixture.CFG)}
        rows = {(r["timeframe"],r["setup"]):r for r in m.daily(fixture.DAY,flow,first,second,fixture.CFG)}
        self.assertEqual(len(rows),9)
        entry = rows[("15m","first_push")]
        self.assertEqual(entry["entry_timestamp_ny"],"2026-01-02T09:45:00-05:00")
        self.assertEqual(entry["entry"],112.)
        self.assertEqual(entry["sl"],124.)
        for mode in m.half.MODES:
            r = rows[("15m","second_"+mode)]
            for column in ("entry","gross_r","net_r","entry_agg_trade_id","exit_agg_trade_id"):
                self.assertEqual(r[column],second[("15m",mode)][column])

    def test_flow_reader_preserves_boolean_side_and_raw_order(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(Path.cwd().resolve()))
            path = Path(folder)/"raw.parquet"
            frame = pd.DataFrame(dict(timestamp=np.arange(12),price=100.,qty=1.,agg_trade_id=np.arange(12),is_buyer_maker=[True,False]*6))
            frame.to_parquet(path,row_group_size=3)
            reader = m.FlowReader(path)
            arrays = reader.window(2,8)
            np.testing.assert_array_equal(arrays[0],np.arange(2,8))
            np.testing.assert_array_equal(arrays[4],[True,False]*3)
            before = reader.groups_read
            reader.window(2,8)
            self.assertEqual(reader.groups_read,before)
            reader.close()

    def test_reused_random_controls_constant_returns_have_zero_lift(self):
        p = pd.DataFrame(dict(symbol="BTCUSDC",timeframe="15m",setup="first_push",fold=1,
            session_date=pd.date_range("2025-01-01",periods=6).strftime("%Y-%m-%d"),
            gross_r=.3,net_r=.25,absorption_observable=True,absorption_supported=True,exhaustion_observable=True,
            absorption_selected=[True,False]*3,exhaustion_selected=[False,True]*3))
        f = pd.DataFrame([dict(symbol="BTCUSDC",timeframe="15m",setup="first_push",fold=1,phase="test",
            policy=policy,test_start="2025-01-01",test_end_exclusive="2025-02-01")
            for policy in ("absorption_matched","exhaustion_matched")])
        result = checks.statistical_controls(p,f,repeats=30)
        self.assertEqual(len(result),2)
        np.testing.assert_allclose(result[["net_mean_r_lift","net_lift_block_ci025","net_lift_block_ci975"]],0.,atol=1e-12)
        np.testing.assert_allclose(result.net_same_frequency_upper_tail_fraction,1.)

    def test_future_test_data_does_not_change_shared_train_cuts(self):
        frames = []
        for minutes in m.TIMEFRAMES:
            for setup in m.SETUPS:
                p = events().assign(timeframe=m.old.timeframe_name(minutes),setup=setup)
                test = p.iloc[:4].copy()
                test["session_date"] = pd.date_range("2025-09-01",periods=4).strftime("%Y-%m-%d")
                test["signal_bar_start_ny"] = test.session_date+"T14:30:00Z"
                p = pd.concat([p,test],ignore_index=True)
                p["feature_available_ms"] = [m.half.ms(day+"T15:00:00Z") for day in p.session_date]
                p["valid_session"] = p["opening_eligible"] = p["absorption_observable"] = p["exhaustion_observable"] = True
                p["exhaustion_signal"] = False
                p["entry"] = 100.
                p["risk_price"] = 10.
                p["gross_r"] = .2
                p["net_r"] = .19
                p["status"] = "time_exit"
                p["target_distance_pct"] = 10.
                p["hold_minutes"] = 60.
                p["exit_timestamp_ny"] = p.session_date+"T16:00:00Z"
                frames.append(p)
        ledger = pd.concat(frames,ignore_index=True)
        cal = pd.DataFrame([dict(symbol="BTCUSDC",fold=1,train_start="2025-01-01",test_start="2025-09-01",test_end_exclusive="2025-11-01")])
        before = m.evaluate(ledger,cal,fixture.CFG)[3]
        ledger.loc[ledger.session_date.ge("2025-09-01"),["abs_large_notional","abs_pressure_rate","net_r"]] = 10000000.
        after = m.evaluate(ledger,cal,fixture.CFG)[3]
        pd.testing.assert_frame_equal(before,after)

    def test_synthetic_cli_and_resume(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(Path.cwd().resolve()))
            first_dir,second_dir = root/"first",root/"second"
            first_dir.mkdir()
            second_dir.mkdir()
            a = fixture.setup()
            cfg = json.loads(json.dumps(fixture.CFG))
            source_info,first_all,second_all = {},[],[]
            for symbol in cfg["assets"]:
                path = root/(symbol+".parquet")
                cfg["assets"][symbol] = str(path)
                pd.DataFrame(dict(timestamp=a[0],price=a[1],qty=a[2],agg_trade_id=a[3],is_buyer_maker=False)).to_parquet(path)
                stat = path.stat()
                source_info[symbol] = dict(bytes=stat.st_size,mtime_ns=stat.st_mtime_ns)
                old_rows = fixture.model.analyse(fixture.DAY,a,cfg,fixture.fixed_profile)
                first_all += [dict(r,symbol=symbol) for r in old_rows]
                selected = {r["timeframe"]:r for r in old_rows if r["entry_mode"] == "body_close" and r["timeframe"] in ("15m","30m","1h")}
                second_all += [dict(r,symbol=symbol) for r in m.half.analyse(fixture.DAY,a,selected,cfg)]
            pd.DataFrame(first_all).to_csv(first_dir/"sessions.csv",index=False)
            pd.DataFrame(second_all).to_csv(second_dir/"sessions.csv",index=False)
            calendar = pd.DataFrame([dict(symbol=s,fold=1,train_start="2025-05-01",test_start="2026-01-01",test_end_exclusive="2026-03-01") for s in cfg["assets"]])
            calendar.to_csv(first_dir/"fold_metrics.csv",index=False)
            first_cfg = dict(rule_version="second_push_utc_v17_1",config=cfg,sources=source_info,code_sha256=m.base.sha(m.old.__file__),helpers={})
            (first_dir/"config_snapshot.json").write_text(json.dumps(first_cfg))
            (first_dir/"audit.json").write_text(json.dumps(dict(status="passed",outputs={p.name:m.base.sha(p) for p in first_dir.iterdir()})))
            cfg["timeframes_minutes"] = [15,30,60]
            cfg["entry_modes"] = ["body_close"]
            second_cfg = dict(rule_version="half_pullback_body_close_v19_1",config=cfg,code_sha256=m.base.sha(m.half.__file__),
                source_audit_sha256=m.base.sha(first_dir/"audit.json"),execution_helpers={})
            (second_dir/"config_snapshot.json").write_text(json.dumps(second_cfg))
            (second_dir/"audit.json").write_text(json.dumps(dict(status="passed",outputs={p.name:m.base.sha(p) for p in second_dir.iterdir()})))
            command = [sys.executable,m.__file__,"--root",str(fixture.ROOT),"--first-source",str(first_dir),"--second-source",str(second_dir),"--output",str(root/"run")]
            for extra in ([],["--resume"]):
                done = subprocess.run(command+extra,capture_output=True,text=True)
                self.assertEqual(done.returncode,0,done.stderr)
                audit = json.loads((root/"run/audit.json").read_text())
                self.assertEqual(audit["rows"],36)
                self.assertEqual(audit["profiles_built"],0)
                self.assertEqual(len(pd.read_csv(root/"run/summary.csv")),180)


if __name__ == "__main__":
    unittest.main()
