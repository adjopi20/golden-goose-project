"""Independent ledger, chronology and performance reconciliation for v19."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ACTIVE = ["tp_first", "sl_first", "time_exit"]
KEY = ["symbol", "session_date", "timeframe"]
MODEL = ["symbol", "timeframe", "pullback_mode"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamps(values):
    return np.array([pd.Timestamp(v).value//1000000 for v in values], dtype=np.int64)


def metrics(part, cfg):
    p = part.sort_values("session_date")
    result = dict(trades=len(p))
    if p.empty:
        return result
    distance = p.risk_price.to_numpy()/p.entry.to_numpy()
    effective = np.minimum(cfg["account_risk_fraction"], cfg["max_entry_notional_equity"]*distance)
    for label in ("gross", "net"):
        r = p[label+"_r"].to_numpy()
        curve = np.r_[1., np.cumprod(1+effective*r)]
        result.update({label+"_wr_pct":100*np.mean(r > 0), label+"_mean_r":r.mean(), label+"_median_r":np.median(r),
            label+"_total_r":r.sum(), label+"_mean_position_pct":100*np.mean(r*distance),
            label+"_capital_base_r":np.sum(effective/cfg["account_risk_fraction"]*r),
            label+"_account_return_pct":100*(curve[-1]-1),
            label+"_account_max_dd_pct":100*np.max(1-curve/np.maximum.accumulate(curve))})
    return result


def compare(actual, expected):
    for name, value in expected.items():
        assert np.isclose(actual[name], value, atol=1e-9, rtol=1e-10), (name, actual[name], value)


def run(folder, source):
    audit = json.loads((folder/"audit.json").read_text())
    assert audit["status"] == "passed" and audit["profiles_built"] == 0
    for name, digest in audit["outputs"].items():
        assert sha(folder/name) == digest, name
    snapshot = json.loads((folder/"config_snapshot.json").read_text())
    assert sha(source/"audit.json") == snapshot["source_audit_sha256"]
    cfg = snapshot["config"]
    assert cfg["timeframes_minutes"] == [15, 30, 60] and cfg["entry_modes"] == ["body_close"]
    data = pd.read_csv(folder/"sessions.csv", float_precision="round_trip")
    original = pd.read_csv(source/"sessions.csv", float_precision="round_trip")
    original = original.loc[original.entry_mode.eq("body_close") & original.timeframe.isin(["15m", "30m", "1h"])]
    assert len(data) == 2*len(original) and not data.duplicated(MODEL+["session_date"]).any()
    joined = data.merge(original, on=KEY, suffixes=("", "_v17"), validate="many_to_one")
    assert len(joined) == len(data)
    for column in ("valid_session", "opening_eligible"):
        assert joined[column].equals(joined[column+"_v17"])
    for column in ("poc", "val", "vah", "opening_price", "first_push_close"):
        np.testing.assert_allclose(joined[column], joined[column+"_v17"], equal_nan=True, atol=1e-12)
    active = joined.loc[joined.status.isin(ACTIVE)].copy()
    assert active.valid_session.all() and active.opening_eligible.all()
    side = np.where(active.touch_edge.eq("val"), 1, -1)
    np.testing.assert_array_equal(active.side, side)
    np.testing.assert_allclose(active.tp, active.poc, atol=1e-12)
    np.testing.assert_allclose(active.sl, 2*active.entry-active.poc, atol=1e-12)
    np.testing.assert_allclose(active.risk_price, np.abs(active.entry-active.poc), atol=1e-12)
    assert ((active.poc-active.entry)*side > 0).all()
    level = np.where(active.touch_edge.eq("val"), active.val, active.vah)
    np.testing.assert_allclose(active.half_level, (level+active.poc)/2, atol=1e-12)
    depth = side*(active.pullback_depth_price-level)/np.abs(active.poc-level)
    np.testing.assert_allclose(depth, active.pullback_depth_fraction, atol=1e-12)
    assert (depth >= .5-1e-12).all()
    close_mode = active.pullback_mode.eq("close_half")
    np.testing.assert_allclose(active.loc[close_mode, "pullback_depth_price"], active.loc[close_mode, "pullback_close"], atol=1e-12)
    assert ((active.pullback_close > active.val) & (active.pullback_close < active.vah)).all()
    assert (np.abs(active.pullback_close-active.poc) < np.abs(active.first_push_close-active.poc)).all()
    width = active.timeframe.map({"15m":900000, "30m":1800000, "1h":3600000}).to_numpy()
    first_end = stamps(active.first_push_available_ny)
    pull_start, pull_end = stamps(active.pullback_start_ny), stamps(active.pullback_available_ny)
    second_start, signal = stamps(active.second_bar_start_ny), stamps(active.signal_timestamp_ny)
    entry, exit_time = stamps(active.entry_timestamp_ny), stamps(active.exit_timestamp_ny)
    period_end = stamps(active.period_end_ny)
    assert (first_end <= pull_start).all() and (pull_end == pull_start+width).all()
    assert (second_start >= pull_end).all() and (signal == second_start+width).all()
    assert (entry >= signal).all() and (entry < period_end).all()
    assert (exit_time >= entry).all() and (exit_time < period_end).all()
    assert (stamps(active.first_push_touch_ny) >= stamps(active.profile_end_ny)).all()
    assert ((np.minimum(active.second_bar_open, active.signal_body_price) <= level)
            & (np.maximum(active.second_bar_open, active.signal_body_price) >= level)).all()
    gross = side*(active.exit_price-active.entry)/active.risk_price
    fee = cfg["fee_bps_per_side"]*.0001*(active.entry+active.exit_price)/active.risk_price
    np.testing.assert_allclose(active.gross_r, gross, atol=1e-10)
    np.testing.assert_allclose(active.net_r, gross-fee, atol=1e-10)
    same = active.entry_agg_trade_id.eq(active.entry_agg_trade_id_v17) & active.entry_timestamp_ny.eq(active.entry_timestamp_ny_v17)
    assert same.equals(active.same_entry_as_v17.astype(bool))
    for column in ("gross_r", "net_r", "entry", "exit_price", "exit_agg_trade_id"):
        np.testing.assert_allclose(active.loc[same, column], active.loc[same, column+"_v17"], atol=1e-10)
    summary = pd.read_csv(folder/"summary.csv")
    folds = pd.read_csv(folder/"fold_metrics.csv")
    checked = 0
    test_populations = {}
    for key, rows in data.groupby(MODEL):
        valid = rows.loc[rows.valid_session]
        s = summary
        f = folds
        for column, value in zip(MODEL, key):
            s = s.loc[s[column].eq(value)]
            f = f.loc[f[column].eq(value)]
        tested = np.zeros(len(valid), dtype=bool)
        for meta in f.to_dict("records"):
            start = meta["train_start"] if meta["phase"] == "train" else meta["test_start"]
            finish = meta["test_start"] if meta["phase"] == "train" else meta["test_end_exclusive"]
            mask = valid.session_date.ge(start) & valid.session_date.lt(finish)
            trades = valid.loc[mask & valid.status.isin(ACTIVE)]
            if meta["phase"] == "train":
                if len(trades):
                    boundary = pd.Timestamp(meta["test_start"], tz="America/New_York").value//1000000
                    trades = trades.loc[stamps(trades.exit_timestamp_ny) < boundary]
            else:
                tested |= mask.to_numpy()
            assert int(mask.sum()) == meta["valid_sessions"]
            compare(meta, metrics(trades, cfg))
            checked += 1
        for sample, population in (("all_available", valid), ("walk_forward_test", valid.loc[tested])):
            actual = s.loc[s["sample"].eq(sample)].iloc[0]
            assert actual.valid_sessions == len(population)
            compare(actual, metrics(population.loc[population.status.isin(ACTIVE)], cfg))
            checked += 1
        test_populations[key] = valid.loc[tested]
    common_dates = set.intersection(*(set(pd.concat([p for key, p in test_populations.items() if key[0] == symbol]).session_date)
                                      for symbol in cfg["assets"]))
    for actual in pd.read_csv(folder/"common_test_summary.csv").to_dict("records"):
        key = tuple(actual[name] for name in MODEL)
        population = test_populations[key]
        population = population.loc[population.session_date.isin(common_dates)]
        assert actual["valid_sessions"] == len(population)
        compare(actual, metrics(population.loc[population.status.isin(ACTIVE)], cfg))
        checked += 1
    result = dict(status="passed", rows=len(data), model_variants=24, geometry_and_chronology_checked=len(active),
        unchanged_entry_outcomes_checked=int(same.sum()), changed_entries=int((~same).sum()),
        entries_without_active_v17_trade=int((~active.status_v17.isin(ACTIVE)).sum()),
        performance_rows_reconciled=checked, common_valid_test_dates=len(common_dates), source_population_unchanged=True,
        source_audit_sha256=sha(source/"audit.json"), run_audit_sha256=sha(folder/"audit.json"))
    (folder/"VALIDATION.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    run(args.run, args.source)
