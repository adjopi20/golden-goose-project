"""v15: require NY opening inside yesterday's VA; fade its first outward touch.

Reuse v14 levels and outcomes. Replay only opening-gap candidates that v14
entered before waiting for an outward touch. No profile rebuild or fitting.
"""
import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import previous_value_area_reversal as base


OUTCOME_COLUMNS = ("entry", "side", "tp", "sl", "risk_price", "risk_bps", "trigger_trade_price",
                   "gross_r", "barrier_gross_r", "entry_timestamp_ny", "exit_timestamp_ny",
                   "entry_agg_trade_id", "exit_agg_trade_id", "exit_price", "cost_r_per_bps",
                   "net_r", "target_distance_pct", "hold_minutes", "touch_edge", "touch_approach",
                   "touch_boundary", "touch_previous_price", "touch_timestamp_ny", "touch_agg_trade_id",
                   "touch_direction")


def eligible(row):
    # Strict interior: a session already on a boundary has not opened inside.
    return bool(row["valid_session"] and row["val"] < row["opening_price"] < row["vah"])


def replay_outward(row, arrays, cfg):
    ts, prices, _, ids = arrays
    opening, end = base.bounds(date.fromisoformat(row["session_date"]))[1:]
    assert base.quality(*arrays, opening, end, cfg) == "valid"
    assert np.isclose(prices[0], row["opening_price"], rtol=0, atol=1e-12)
    clean = {k:v for k,v in row.items() if k not in OUTCOME_COLUMNS}
    # The first post-open print establishes the opening price. Ignore any
    # crossing from the PRE-open print; start searching at print number two.
    touch = base.first_touch(prices[1:], float(prices[0]), row["val"], row["vah"])
    if touch is None:
        return dict(clean, status="no_touch")
    i, side = touch["index"]+1, touch["side"]
    assert ((touch["edge"] == "val" and touch["approach"] == "from_above")
            or (touch["edge"] == "vah" and touch["approach"] == "from_below"))
    clean.update(touch_edge=touch["edge"], touch_approach=touch["approach"],
                 touch_boundary=touch["boundary"], touch_previous_price=touch["previous_price"],
                 touch_timestamp_ny=base.iso(ts[i]), touch_agg_trade_id=int(ids[i]), touch_direction="out_of_value")
    result = base.outcome(prices[i:], row["poc"], -side, "endpoint")
    j = result.pop("exit_index", None)
    clean.update(result)
    if j is None:
        return clean
    entry, risk = result["entry"], result["risk_price"]
    exit_price = float(prices[i+j])
    gross = side*(exit_price-entry)/risk
    fee = .0001*(entry+exit_price)/risk
    clean.update(barrier_gross_r=result["gross_r"], gross_r=gross, exit_price=exit_price,
                 cost_r_per_bps=fee, net_r=gross-cfg["fee_bps_per_side"]*fee,
                 entry_timestamp_ny=base.iso(ts[i]), exit_timestamp_ny=base.iso(ts[i+j]),
                 entry_agg_trade_id=int(ids[i]), exit_agg_trade_id=int(ids[i+j]),
                 target_distance_pct=100*risk/entry, hold_minutes=(ts[i+j]-ts[i])/60000)
    return clean


def filter_sessions(data, cfg, loader):
    rows, repaired = [], []
    for record in data.to_dict("records"):
        row = dict(record, source_status=record["status"])
        ok = eligible(row) if row["valid_session"] else False
        row["opening_eligible"] = ok
        if row["valid_session"] and not ok:
            row["status"] = "no_entry_open_not_inside_va"
            row.update({k:None for k in OUTCOME_COLUMNS})
        elif ok and row["status"] != "no_touch" and row.get("touch_direction") != "out_of_value":
            original = row.copy()
            row = replay_outward(row, loader(row), cfg)
            repaired.append(dict(symbol=row["symbol"], session_date=row["session_date"],
                                 old_entry_timestamp_ny=original.get("entry_timestamp_ny"),
                                 new_entry_timestamp_ny=row.get("entry_timestamp_ny"),
                                 old_gross_r=original.get("gross_r"), new_gross_r=row.get("gross_r"),
                                 old_status=original["status"], new_status=row["status"]))
        rows.append(row)
    result = pd.DataFrame(rows)
    trades = result.loc[result.status.isin(base.ACTIVE)]
    assert trades.opening_eligible.all() and trades.touch_direction.eq("out_of_value").all()
    assert not trades.duplicated(["symbol", "session_date"]).any()
    if len(trades):
        np.testing.assert_allclose(trades.net_r, trades.gross_r-.0001*cfg["fee_bps_per_side"]*(trades.entry+trades.exit_price)/trades.risk_price)
    return result, pd.DataFrame(repaired)


def run(source, root, out):
    audit = json.loads((source/"audit.json").read_text())
    assert audit["status"] == "passed"
    for name, digest in audit["outputs"].items():
        assert base.sha(source/name) == digest, f"Source output changed: {name}"
    snapshot = json.loads((source/"config_snapshot.json").read_text())
    assert snapshot["code_sha256"] == base.sha(base.__file__)
    for path, digest in snapshot["dependencies"].items():
        assert base.sha(path) == digest, f"Source dependency changed: {path}"
    cfg = snapshot["config"]
    for symbol, record in snapshot["sources"].items():
        stat = (root/cfg["assets"][symbol]).stat()
        assert stat.st_size == record["bytes"] and stat.st_mtime_ns == record["mtime_ns"], "Raw archive changed"
    cached = {}
    def loader(row):
        symbol = row["symbol"]
        if symbol not in cached:
            cached[symbol] = base.metadata(root/cfg["assets"][symbol])
        f, mins, maxs = cached[symbol]
        opening, end = base.bounds(date.fromisoformat(row["session_date"]))[1:]
        return base.load_window(f, mins, maxs, opening, end)
    data = pd.read_csv(source/"sessions.csv")
    assert data.valid_session.notna().all()
    filtered, repairs = filter_sessions(data, cfg, loader)
    out.mkdir(parents=True, exist_ok=False)
    base.write_reports(filtered, cfg, out)
    repairs.to_csv(out/"opening_gap_replays.csv", index=False)
    old_summary = pd.read_csv(source/"summary.csv").assign(model="v14_base")
    new_summary = pd.read_csv(out/"summary.csv").assign(model="v15_open_inside")
    pd.concat([old_summary, new_summary], ignore_index=True).to_csv(out/"comparison.csv", index=False)
    old_folds, new_folds = pd.read_csv(source/"fold_metrics.csv"), pd.read_csv(out/"fold_metrics.csv")
    keys = ["symbol", "fold", "phase", "train_start", "test_start", "test_end_exclusive"]
    pd.testing.assert_frame_equal(old_folds[keys], new_folds[keys])
    fold_compare = old_folds.merge(new_folds, on=keys, suffixes=("_base", "_inside"), validate="one_to_one")
    fold_compare.to_csv(out/"fold_comparison.csv", index=False)
    coverage = []
    for symbol, group in filtered.groupby("symbol"):
        old = data.loc[data.symbol.eq(symbol)]
        valid = group.loc[group.valid_session]
        tests = new_folds.loc[new_folds.symbol.eq(symbol) & new_folds.phase.eq("test")]
        for sample in ("all_available", "walk_forward_test"):
            part = valid
            baseline = old.loc[old.valid_session]
            if sample == "walk_forward_test":
                mask = valid.session_date.isin([])
                for fold in tests.itertuples():
                    mask |= valid.session_date.ge(fold.test_start) & valid.session_date.lt(fold.test_end_exclusive)
                part = valid.loc[mask]
                baseline = baseline.loc[baseline.session_date.isin(part.session_date)]
            n, eligible_n = len(part), int(part.opening_eligible.sum())
            kept_n = int(part.status.isin(base.ACTIVE).sum())
            base_n = int(baseline.status.isin(base.ACTIVE).sum())
            coverage.append(dict(symbol=symbol, sample=sample, valid_sessions=n, eligible_open_inside=eligible_n,
                                 base_entries=base_n, inside_entries=kept_n,
                                 entry_pct_all_valid=100*kept_n/n if n else None,
                                 entry_pct_eligible=100*kept_n/eligible_n if eligible_n else None,
                                 removed_entries=base_n-kept_n))
    coverage = pd.DataFrame(coverage)
    coverage.to_csv(out/"coverage.csv", index=False)
    lines = ["# NY opening inside previous value area — v15", "",
             "Require VAL < first price at/after 09:30 NY < VAH. Skip the entire profile period if this fails. "
             "From that opening, wait for the first outward touch/crossing: VAL long, VAH short. "
             "Frozen previous 09:30–09:30 POC target, equal-distance stop, one trade maximum, time exit before next 09:30. "
             "Opening exactly at a boundary is excluded; no such opening occurred among v14 trades. "
             "No pre-open-to-open crossing can trigger an entry. Existing levels and all unchanged trade outcomes are reused.", "",
             f"Replayed {len(repairs)} opening-gap sessions from raw prints without rebuilding profiles. "
             "Source/date/quality population and fold calendars match v14. All noneligible sessions stay in the denominator of valid_sessions. "
             "coverage.csv separately reports the denominator of opening-eligible sessions.", "",
             "Nothing fitted or optimized. Calendar train 8 months/test 2 months, step 2. "
             "Previously observed history; this tweak is development evidence, not a fresh untouched holdout. "
             "Gross and 4 bps per-side fee net use identical trigger-print proxy and 0.5% equity risk/5× notional cap as v14. "
             "Funding, latency, spread, book impact and intratrade drawdown are not modeled.", "",
             "| Asset | Sample | Base entries → inside | Gross WR base → inside | Mean gross R base → inside | Mean net R base → inside | Net account base → inside |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for old in old_summary.to_dict("records"):
        new = new_summary.loc[new_summary.symbol.eq(old["symbol"]) & new_summary["sample"].eq(old["sample"])].iloc[0]
        def pair(key, digits=3, suffix=""):
            return f"{old[key]:.{digits}f}{suffix} → {new[key]:.{digits}f}{suffix}"
        lines.append(f"| {old['symbol']} | {old['sample']} | {int(old['trades'])} → {int(new.trades)} | "
                     +" | ".join((pair("gross_wr_pct", 2, "%"), pair("gross_mean_r"), pair("net_mean_r"), pair("net_account_return_pct", 2, "%")))+" |")
    lines += ["", "| Asset | Positive net-account test folds | Gross-expectancy improvement folds | Net-expectancy improvement folds |", "|---|---:|---:|---:|"]
    for symbol, group in fold_compare.loc[fold_compare.phase.eq("test")].groupby("symbol"):
        lines.append(f"| {symbol} | {int(group.net_account_return_pct_inside.gt(0).sum())}/{len(group)} | "
                     f"{int(group.gross_mean_r_inside.gt(group.gross_mean_r_base).sum())}/{len(group)} | "
                     f"{int(group.net_mean_r_inside.gt(group.net_mean_r_base).sum())}/{len(group)} |")
    (out/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    run_snapshot = dict(rule_version="previous_va_open_inside_v15_1", source=str(source.resolve()),
                        source_snapshot=snapshot, source_audit_sha256=base.sha(source/"audit.json"),
                        script_sha256=base.sha(__file__), reporting_helper_sha256=base.sha(base.__file__),
                        profile_rebuilds=0, raw_sessions_replayed=len(repairs))
    (out/"config_snapshot.json").write_text(json.dumps(run_snapshot, indent=2), encoding="utf-8")
    outputs = {p.name:base.sha(p) for p in out.iterdir() if p.is_file()}
    (out/"audit.json").write_text(json.dumps(dict(status="passed", periods=len(filtered),
                 entries=int(filtered.status.isin(base.ACTIVE).sum()), profile_rebuilds=0,
                 raw_sessions_replayed=len(repairs), outputs=outputs), indent=2), encoding="utf-8")
    print((out/"REPORT.md").read_text(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.root, args.output)


if __name__ == "__main__":
    main()
