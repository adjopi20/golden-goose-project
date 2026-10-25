import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("mr_paper_worker", Path(__file__).with_name("worker.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = w.StateStore(Path(self.tmp.name) / "paper.sqlite", "binance_usdm")
        self.model = w.Model(self.store)
        self.ts = 1792935000000
        day, start, opening, end = w.session_bounds(self.ts)
        self.ts = opening + 1000
        self.state = dict(day=day, profile_start_ms=start, opening_ms=opening, end_ms=end,
            opening_price=103, previous_price=103, consumed=False, quality="valid",
            profile_rows=100, poc=100, val=98, vah=102, side=-1, location=True)
        self.store.db.execute("INSERT INTO mr_sessions VALUES (?,?)", (day, w.encode(self.state)))
        self.store.db.execute("INSERT INTO mr_ticks VALUES (1,?,103,1,1)", (self.ts - 1,))
        self.model.manager.tick(w.ACCOUNT, self.tick(1, 103, -1))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tick(self, i, price, offset=0):
        return dict(symbol=w.SYMBOL, timestamp_ms=self.ts + offset, agg_trade_id=i,
                    price=float(price), quantity=1., buy=True)

    def process(self, i, price, offset=0, **kwargs):
        tick = self.tick(i, price, offset)
        self.model.process(tick, now_ms=tick["timestamp_ms"], **kwargs)

    def portfolio(self):
        return self.model.manager._load(w.ACCOUNT)[1]

    def test_next_print_bracket_risk_and_duplicate(self):
        self.process(2, 102)
        self.assertIsNone(self.portfolio().position)
        self.process(3, 102)
        self.assertIsNone(self.portfolio().position)
        self.process(4, 101.99, 1)
        p = self.portfolio().position
        self.assertAlmostEqual(p["stop"], 103.98)
        self.assertAlmostEqual(p["target_price"], 100)
        self.assertAlmostEqual(p["actual_initial_risk"], 250)
        self.assertAlmostEqual(p["quantity"] * p["entry_price"] * .0004, p["entry_fee"])
        self.process(4, 101.99, 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM journal WHERE kind='fill'").fetchone()[0], 1)

    def test_profit_fee_and_compounding(self):
        self.process(2, 102)
        self.process(3, 102, 1)
        qty = self.portfolio().position["quantity"]
        self.process(4, 100, 2)
        self.assertIsNotNone(self.portfolio().position)
        self.process(5, 99.99, 3)
        expected = 10000 + qty * (102 - 99.99) - qty * (102 + 99.99) * .0004
        self.assertAlmostEqual(self.portfolio().equity, expected)
        candidate = dict(sample_id="next", session_day="next", symbol=w.SYMBOL,
            direction="short", stop=104, entry_reference=102, frozen_poc=100,
            feature_as_of_ms=self.ts + 3, entry_eligible_timestamp_ms=self.ts + 4,
            entry_deadline_timestamp_ms=self.ts + 1000, force_exit_timestamp_ms=self.ts + 2000)
        self.model.manager.submit(w.ACCOUNT, candidate, snapshot={"feature_as_of_ms": self.ts + 3},
                                  risk_fraction=.025)
        self.process(6, 102, 4)
        self.assertAlmostEqual(self.portfolio().position["actual_initial_risk"], expected * .025)

    def test_bootstrap_does_not_enter_or_retry(self):
        self.process(2, 102, origin="bootstrap")
        self.process(3, 103, 1)
        self.process(4, 102, 2)
        self.process(5, 102, 3)
        self.assertIsNone(self.portfolio().position)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM intents").fetchone()[0], 1)

    def test_open_inside_va_does_not_trade(self):
        self.state.update(opening_price=101, previous_price=101, location=False)
        self.store.db.execute("UPDATE mr_sessions SET payload=?", (w.encode(self.state),))
        self.process(2, 102)
        self.process(3, 102, 1)
        self.assertIsNone(self.portfolio().position)
        self.assertFalse(self.portfolio().signals)

    def test_stale_stream_cannot_create_entry(self):
        tick = self.tick(2, 102)
        self.model.process(tick, now_ms=tick["timestamp_ms"] + 6000)
        self.process(3, 102, 1)
        self.assertIsNone(self.portfolio().position)
        self.assertFalse(self.portfolio().signals)

    def test_resumed_history_does_not_repeat_short_page(self):
        class Api:
            calls = 0
            def get(self, path, **params):
                self.calls += 1
                return [dict(a=2, T=10, p="100", q="1", m=False)]
        api = Api()
        ticks = list(w.history(api, 0, 7200000, {"agg_trade_id": 1}))
        self.assertEqual([t["agg_trade_id"] for t in ticks], [2])
        self.assertEqual(api.calls, 1)

    def test_actual_fill_distance_rechecked(self):
        self.process(2, 102)
        self.process(3, 100.5, 1)
        self.assertIsNone(self.portfolio().position)
        self.assertFalse(self.portfolio().signals)
        self.process(4, 102, 2)
        self.assertIsNone(self.portfolio().position)

    def test_recovery_cancels_pending_entry(self):
        self.process(2, 102)
        self.process(3, 102, 1, origin="repair")
        self.assertFalse(self.portfolio().signals)
        self.assertIsNone(self.portfolio().position)

    def test_gap_and_conflict_do_not_advance(self):
        with self.assertRaises(w.UnrecoverableGap):
            self.process(3, 102)
        self.assertEqual(self.model.cursor()["agg_trade_id"], 1)
        with self.assertRaises(ValueError):
            self.process(1, 101, -1)

    def test_transaction_rolls_back_cursor_and_candidate(self):
        with patch.object(self.model.manager, "submit", side_effect=RuntimeError("test rollback")):
            with self.assertRaises(RuntimeError):
                self.process(2, 102)
        self.assertEqual(self.model.cursor()["agg_trade_id"], 1)
        self.assertFalse(json.loads(self.store.db.execute("SELECT payload FROM mr_sessions").fetchone()[0])["consumed"])
        self.assertEqual(json.loads(self.store.db.execute("SELECT cursor FROM accounts").fetchone()[0])["agg_trade_id"], 1)

    def test_restart_preserves_position(self):
        self.process(2, 102)
        self.process(3, 102, 1)
        before = self.portfolio().position
        self.model = w.Model(self.store)
        self.assertEqual(self.portfolio().position, before)

    def test_time_exit_at_first_next_session_print(self):
        self.process(2, 102)
        self.process(3, 102, 1)
        tick = self.tick(4, 101)
        tick["timestamp_ms"] = self.state["end_ms"]
        self.model.manager.tick(w.ACCOUNT, tick)
        self.assertIsNone(self.portfolio().position)
        payload = json.loads(self.store.db.execute("SELECT payload FROM journal WHERE kind='trade'").fetchone()[0])
        self.assertEqual(payload["exit_reason"], "time_exit_next_print")

    def test_long_direction(self):
        self.state.update(opening_price=97, previous_price=97, side=1)
        self.store.db.execute("UPDATE mr_sessions SET payload=?", (w.encode(self.state),))
        self.process(2, 98)
        self.process(3, 98.01, 1)
        p = self.portfolio().position
        self.assertEqual(p["direction"], "long")
        self.assertAlmostEqual(p["stop"], 96.02)
        self.assertAlmostEqual(p["target_price"], 100)

    def test_profile_matches_shared_builder_and_excludes_future(self):
        self.store.db.execute("DELETE FROM mr_ticks")
        day, start, opening, end = w.session_bounds(self.ts)
        times = np.arange(start, opening, 60000, dtype=np.int64)
        prices = 95 + 10 * np.linspace(0, 1, len(times))
        qty = 1 + np.arange(len(times)) % 7
        self.store.db.executemany("INSERT INTO mr_ticks VALUES (?,?,?,?,1)",
            [(i, int(t), float(p), float(q)) for i, (t, p, q) in enumerate(zip(times, prices, qty))])
        self.store.db.execute("INSERT INTO mr_ticks VALUES (?,?,?,?,1)", (len(times), opening, 999, 99999))
        tick = self.tick(len(times), 110)
        tick["timestamp_ms"] = opening
        state = self.model.freeze(tick)
        vp = w.build_basic_volume_profile(pd.DataFrame(dict(price=prices, qty=qty)), n_bins=50, value_area_pct=.7)
        self.assertEqual(state["quality"], "valid")
        self.assertEqual(state["poc"], vp["poc_price"])
        self.assertEqual(state["val"], vp["val"])
        self.assertEqual(state["vah"], vp["vah"])

    def test_dst_bounds(self):
        for day, hours in (("2026-03-08", 23), ("2026-11-01", 25)):
            ts = int(pd.Timestamp(day + " 16:00", tz="UTC").timestamp() * 1000)
            _, start, opening, end = w.session_bounds(ts)
            self.assertEqual((opening - start) / 3600000, hours)
            self.assertGreater(end, opening)

    def test_history_preserves_same_ms_pagination(self):
        class Api:
            calls = 0
            def get(self, path, **params):
                self.calls += 1
                start = params.get("fromId", 0)
                return [dict(a=i, T=10 if i < 1001 else 20, p="100", q="1", m=False)
                        for i in range(start, min(start + 1000, 1002))]
        api = Api()
        ticks = list(w.history(api, 0, 20))
        self.assertEqual([t["agg_trade_id"] for t in ticks], list(range(1001)))
        self.assertEqual(api.calls, 2)


if __name__ == "__main__":
    unittest.main()
