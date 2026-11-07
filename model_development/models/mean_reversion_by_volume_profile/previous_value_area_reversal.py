"""Frozen previous NY-open profile: first VA touch, POC target, equal stop.

Reuses the existing basic profile, raw-data quality checks and TP/SL scanner.
This is a trade-print observation, not an order-book execution backtest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from observe import iso, load_window, metadata
from observe_four_profiles import quality
from backtest_poc_rr import outcome

NY = ZoneInfo("America/New_York")
ACTIVE = ("tp_first", "sl_first", "time_exit")
RULE_VERSION = "previous_value_area_touch_v14_1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bounds(day):
    return tuple(int(datetime.combine(day + timedelta(days=offset), time(9, 30), NY).timestamp()*1000)
                 for offset in (-1, 0, 1))


def first_touch(prices, previous_price, val, vah):
    """First equality/crossing, either direction, ordered by observed trade ID.

    If one price jump crosses both edges, the nearer edge along that jump is
    the first candidate. Its actual observed entry can invalidate TP geometry.
    """
    previous = np.r_[previous_price, prices[:-1]]
    candidates = []
    for edge, level, side in (("val", val, 1), ("vah", vah, -1)):
        hit = np.flatnonzero((prices == level) | ((previous-level)*(prices-level) < 0))
        if len(hit):
            i = int(hit[0])
            candidates.append((i, abs(level-previous[i]), edge, side, float(previous[i])))
    if not candidates:
        return None
    i, _, edge, side, before = min(candidates)
    level = val if edge == "val" else vah
    approach = "from_below" if before < level else "from_above" if before > level else "on_level"
    return dict(index=i, edge=edge, side=side, approach=approach, previous_price=before,
                boundary=float(level))


def analyse(day, arrays, cfg, builder):
    start, opening, end = bounds(day)
    ts, prices, qty, ids = arrays
    row = dict(session_date=str(day), status="pending", valid_session=False,
               profile_start_ny=iso(start), profile_end_ny=iso(opening),
               period_end_ny=iso(end), period_hours=(end-opening)/3600000)
    if len(ts) and (np.diff(ts) < 0).any():
        return dict(row, status="unordered_timestamps")
    split = int(np.searchsorted(ts, opening))
    for label, part, left, right in (("profile", slice(0, split), start, opening),
                                    ("observation", slice(split, None), opening, end)):
        q = quality(*(a[part] for a in arrays), left, right, cfg)
        if q != "valid":
            return dict(row, status=label+"_"+q)
    if ids[split] != ids[split-1]+1:
        return dict(row, status="boundary_aggregate_id_gap")
    vp = builder(pd.DataFrame(dict(price=prices[:split], qty=qty[:split])),
                 n_bins=cfg["profile_bins"], value_area_pct=cfg["value_area_fraction"])
    poc, val, vah = (float(vp[k]) for k in ("poc_price", "val", "vah"))
    row.update(poc=poc, val=val, vah=vah, profile_last_price=float(prices[split-1]),
               profile_last_timestamp_ny=iso(ts[split-1]), profile_rows=split,
               observation_rows=len(ts)-split, profile_volume=float(vp["total_volume"]))
    if not 0 < val < poc < vah:
        return dict(row, status="degenerate_profile")
    future, future_ts = prices[split:], ts[split:]
    row.update(valid_session=True, opening_price=float(future[0]),
               opening_location="below" if future[0] < val else "above" if future[0] > vah else "inside")
    touch = first_touch(future, float(prices[split-1]), val, vah)
    if touch is None:
        return dict(row, status="no_touch")
    i, side = touch["index"], touch["side"]
    row.update(touch_edge=touch["edge"], touch_approach=touch["approach"],
               touch_boundary=touch["boundary"], touch_previous_price=touch["previous_price"],
               touch_timestamp_ny=iso(future_ts[i]), touch_agg_trade_id=int(ids[split+i]),
               touch_direction="out_of_value" if (touch["edge"] == "val" and touch["approach"] == "from_above")
                   or (touch["edge"] == "vah" and touch["approach"] == "from_below") else "toward_value")
    # Reuse the established barrier scanner; primary P&L uses observed trigger
    # prices instead of assuming every TP/SL can fill exactly at its barrier.
    result = outcome(future[i:], poc, -side, "endpoint")
    j = result.pop("exit_index", None)
    if j is None:
        row.update(result)
        return row  # First candidate consumed: no later replacement that day.
    row.update(result)
    entry, risk = result["entry"], result["risk_price"]
    exit_price = float(future[i+j])
    trigger_gross_r = side*(exit_price-entry)/risk
    fee_r_per_bps = .0001*(entry+exit_price)/risk
    row.update(barrier_gross_r=result["gross_r"], gross_r=float(trigger_gross_r),
               entry_timestamp_ny=iso(future_ts[i]), exit_timestamp_ny=iso(future_ts[i+j]),
               entry_agg_trade_id=int(ids[split+i]), exit_agg_trade_id=int(ids[split+i+j]),
               exit_price=exit_price, cost_r_per_bps=fee_r_per_bps,
               net_r=trigger_gross_r-cfg["fee_bps_per_side"]*fee_r_per_bps,
               target_distance_pct=100*risk/entry,
               hold_minutes=float((future_ts[i+j]-future_ts[i])/60000))
    assert future_ts[i] >= opening and future_ts[i+j] < end
    assert side*(poc-entry) > 0 and np.isclose(abs(result["sl"]-entry), risk)
    return row


def fold_schedule(first, last):
    # Same 8+2 month calendar as the older studies; no fitted parameter here.
    start = pd.Timestamp(first)
    if start.day != 1:
        start = start.to_period("M").to_timestamp()+pd.DateOffset(months=1)
    last_end = pd.Timestamp(last)+pd.Timedelta(days=1)
    while start+pd.DateOffset(months=10) <= last_end:
        test = start+pd.DateOffset(months=8)
        yield start, test, test+pd.DateOffset(months=2)
        start += pd.DateOffset(months=2)


def stats(trades, valid_sessions, cfg):
    row = dict(valid_sessions=valid_sessions, trades=len(trades),
               entry_pct_valid_sessions=100*len(trades)/valid_sessions if valid_sessions else None)
    if trades.empty:
        return row
    g = trades.sort_values("session_date")
    distance = g.risk_price/g.entry
    effective_risk = np.minimum(cfg["account_risk_fraction"], cfg["max_entry_notional_equity"]*distance)
    row.update(tp_first=int(g.status.eq("tp_first").sum()), sl_first=int(g.status.eq("sl_first").sum()),
               time_exit=int(g.status.eq("time_exit").sum()),
               median_target_pct=g.target_distance_pct.median(), mean_target_pct=g.target_distance_pct.mean(),
               median_hold_minutes=g.hold_minutes.median(), capped_entries=int((effective_risk < cfg["account_risk_fraction"]).sum()))
    for label, r in (("gross", g.gross_r), ("net", g.net_r)):
        positions = r*distance
        curve = np.r_[1., np.cumprod(1+effective_risk*r)]
        if (curve <= 0).any():
            raise ValueError("Account exhausted in print-proxy simulation; stop analysis")
        pos, neg = r[r > 0].sum(), -r[r < 0].sum()
        row.update({label+"_wr_pct":100*r.gt(0).mean(), label+"_mean_r":r.mean(),
                    label+"_median_r":r.median(), label+"_total_r":r.sum(),
                    label+"_profit_factor_r":pos/neg if neg else None,
                    label+"_mean_position_pct":100*positions.mean(),
                    label+"_account_return_pct":100*(curve[-1]-1),
                    label+"_account_max_dd_pct":100*(1-curve/np.maximum.accumulate(curve)).max(),
                    label+"_capital_base_r":(effective_risk/cfg["account_risk_fraction"]*r).sum()})
    return row


def write_reports(data, cfg, out):
    summaries, folds, years, cohorts = [], [], [], []
    for symbol, all_rows in data.groupby("symbol", sort=True):
        valid = all_rows.loc[all_rows.valid_session.eq(True)]
        trades = valid.loc[valid.status.isin(ACTIVE)]
        assert not trades.session_date.duplicated().any()
        summaries.append(dict(symbol=symbol, sample="all_available", **stats(trades, len(valid), cfg)))
        test_parts, test_valid = [], 0
        if len(valid):
            for fold, (train_start, test_start, test_end) in enumerate(fold_schedule(valid.session_date.min(), valid.session_date.max()), 1):
                te = valid.loc[valid.session_date.ge(str(test_start.date())) & valid.session_date.lt(str(test_end.date()))]
                tt = te.loc[te.status.isin(ACTIVE)]
                tr = trades.loc[trades.session_date.ge(str(train_start.date())) & trades.session_date.lt(str(test_start.date()))]
                # A session ending next day can cross a test boundary: purge it
                # from the train readout, even though there is no calibration.
                available = pd.to_datetime(tr.exit_timestamp_ny, utc=True, format="ISO8601") < pd.Timestamp(test_start.date()).tz_localize(NY).tz_convert("UTC") if len(tr) else pd.Series(dtype=bool)
                for phase, part, count in (("train", tr.loc[available], int((valid.session_date.ge(str(train_start.date())) & valid.session_date.lt(str(test_start.date()))).sum())),
                                           ("test", tt, len(te))):
                    folds.append(dict(symbol=symbol, fold=fold, phase=phase,
                                      train_start=str(train_start.date()), test_start=str(test_start.date()),
                                      test_end_exclusive=str(test_end.date()), train_outcomes_purged=int((~available).sum()),
                                      limited_sample=len(part) < 20, **stats(part, count, cfg)))
                test_parts.append(tt)
                test_valid += len(te)
        tested = pd.concat(test_parts, ignore_index=True) if test_parts else trades.iloc[:0]
        assert not tested.session_date.duplicated().any()
        summaries.append(dict(symbol=symbol, sample="walk_forward_test", **stats(tested, test_valid, cfg)))
        for year, part in valid.groupby(valid.session_date.str[:4]):
            years.append(dict(symbol=symbol, year=year, **stats(part.loc[part.status.isin(ACTIVE)], len(part), cfg)))
        for col in ("touch_edge", "touch_direction", "opening_location"):
            if col in trades:
                for value, part in trades.groupby(col):
                    cohorts.append(dict(symbol=symbol, dimension=col, category=value, **stats(part, len(part), cfg)))
    summary = pd.DataFrame(summaries)
    summary.to_csv(out/"summary.csv", index=False)
    pd.DataFrame(folds if folds else [], columns=None if folds else ["symbol", "fold", "phase", "trades"]).to_csv(out/"fold_metrics.csv", index=False)
    pd.DataFrame(years if years else [], columns=None if years else ["symbol", "year", "trades"]).to_csv(out/"yearly_metrics.csv", index=False)
    pd.DataFrame(cohorts if cohorts else [], columns=None if cohorts else ["symbol", "dimension", "category", "trades"]).to_csv(out/"cohort_metrics.csv", index=False)
    data.groupby(["symbol", "status"]).size().rename("periods").reset_index().to_csv(out/"quality.csv", index=False)
    data.to_csv(out/"sessions.csv", index=False)
    lines = ["# Previous value-area touch: fixed rule v14", "",
             "Previous 09:30–current 09:30 America/New_York profile; first VAL touch = long, first VAH touch = short, either direction. "
             "Frozen POC target; equal-distance stop; one trade per profile period. Time exit at last print strictly before next 09:30. "
             "Every calendar day including weekends. DST transition periods have 23/25 elapsed hours.", "",
             f"Profiles reuse indicator.volume_profile.build_basic_volume_profile: {cfg['profile_bins']} equal-width price bins, quantity weights, {100*cfg['value_area_fraction']}% value area. "
             "A touch includes equality or a crossing between prints, including last profile print to first session print. "
             "Entry uses the crossing print. If both edges are crossed in one jump, the first edge along the jump is used; "
             "invalid TP geometry consumes the candidate without a replacement.", "",
             f"Fee assumption: {cfg['fee_bps_per_side']} bps per side on actual modeled entry and exit notionals. "
             f"Account illustration: {100*cfg['account_risk_fraction']}% equity risk, at most {cfg['max_entry_notional_equity']}× entry notional/equity. Assets separate.", "",
             "Primary P&L uses observed TP/SL-trigger print prices; original ideal-barrier R is retained separately. "
             "This proxy can execute at the signal/trigger print and is optimistic about latency, spread and liquidity. "
             "Not a shared next-print execution replay; funding, impact, liquidation and intratrade drawdown are unmodeled. "
             "Closed-trade drawdown only; fee-only net profitability is not sufficient for deployment.", "",
             "No fitting or threshold selection. Rolling 8-month train/2-month test, step 2 months, is a chronological readout. "
             "Training intervals overlap; never pool them as independent evidence. Previously inspected assets/history remain development data. "
             "Incomplete full test folds are omitted from the test aggregate, but remain in all_available and yearly readouts. "
             "Trade ownership follows session date; a final test-day trade can exit the next morning. Train outcomes crossing midnight at the test start are purged conservatively.", "",
             "| Asset | Sample | Valid sessions | Trades | Gross WR | Net WR | Gross mean R | Net mean R | Gross account | Net account | Median target |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in summary.to_dict("records"):
        def f(key, suffix=""):
            return f"{r[key]:.3f}{suffix}" if pd.notna(r.get(key)) else "—"
        lines.append(f"| {r['symbol']} | {r['sample']} | {r['valid_sessions']} | {r['trades']} | "
                     +" | ".join(f(k, "%" if k.endswith("pct") else "") for k in
                                 ("gross_wr_pct", "net_wr_pct", "gross_mean_r", "net_mean_r", "gross_account_return_pct", "net_account_return_pct", "median_target_pct"))+" |")
    lines += ["", "Read quality.csv before interpreting percentages: missing, duplicate, gapped and partial sessions stay excluded and visible. "
              "cohort_metrics.csv is descriptive attribution, not a newly selected filter. "
              "Complete source manifest and configuration are in config_snapshot.json. Do not read an interrupted run as complete."]
    (out/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    p.add_argument("--config", type=Path, default=Path(__file__).with_name("previous_value_area_config.json"))
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2026-10-22", help="Last NY session start date, inclusive")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--resume", action="store_true", help="Continue an interrupted run with identical config/code/sources")
    args = p.parse_args()
    begin, finish = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if finish < begin:
        raise ValueError("End before start")
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if (not 0 < cfg["account_risk_fraction"] < 1 or not np.isfinite(cfg["fee_bps_per_side"]) or cfg["fee_bps_per_side"] < 0
            or not np.isfinite(cfg["max_entry_notional_equity"]) or cfg["max_entry_notional_equity"] <= 0
            or not isinstance(cfg["profile_bins"], int) or cfg["profile_bins"] <= 0
            or not 0 < cfg["value_area_fraction"] <= 1 or not cfg["assets"]):
        raise ValueError("Invalid risk or fee configuration")
    sys.path.insert(0, str(args.root.resolve()))
    from indicator.volume_profile import build_basic_volume_profile
    sources = {}
    for symbol, relative in cfg["assets"].items():
        path = (args.root/relative).resolve()
        sources[symbol] = dict(path=str(path), bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns)
    snapshot = dict(rule_version=RULE_VERSION, config=cfg, start=args.start, end=args.end, sources=sources,
                    code_sha256=sha(__file__), dependencies={str(Path(module.__file__).resolve()): sha(module.__file__)
                        for module in (sys.modules["observe"], sys.modules["observe_four_profiles"], sys.modules["backtest_poc_rr"], sys.modules["indicator.volume_profile"])})
    out = args.output
    if args.resume:
        if json.loads((out/"config_snapshot.json").read_text()) != snapshot:
            raise ValueError("Resume inputs changed; use a new output directory")
    else:
        out.mkdir(parents=True, exist_ok=False)
        (out/"config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (out/"audit.json").write_text(json.dumps(dict(status="running")), encoding="utf-8")
    frames = []
    for symbol, source in sources.items():
        f, mins, maxs = metadata(Path(source["path"]))
        checkpoint = out/f"{symbol}_sessions.jsonl"
        rows = [json.loads(line) for line in checkpoint.read_text().splitlines() if line] if checkpoint.exists() else []
        expected = [str(begin+timedelta(days=i)) for i in range((finish-begin).days+1)]
        if [r["session_date"] for r in rows] != expected[:len(rows)] or len(rows) > len(expected):
            raise ValueError("Checkpoint has noncontiguous/duplicate dates")
        with checkpoint.open("a", encoding="utf-8") as handle:
            for count in range(len(rows), len(expected)):
                day = date.fromisoformat(expected[count])
                start, opening, end = bounds(day)
                if start < mins.min() or end-60000 > maxs.max():
                    row = dict(symbol=symbol, session_date=str(day), valid_session=False, status="outside_full_archive")
                else:
                    arrays = load_window(f, mins, maxs, start, end)
                    row = dict(analyse(day, arrays, cfg, build_basic_volume_profile), symbol=symbol)
                handle.write(json.dumps(row, allow_nan=False)+"\n")
                handle.flush()
                rows.append(row)
                if count % 50 == 0:
                    print(f"{symbol} {day}: {row['status']}", flush=True)
        frames.append(pd.DataFrame(rows))
        print(f"{symbol}: {len(rows)} periods complete", flush=True)
    for source in sources.values():
        stat = Path(source["path"]).stat()
        if stat.st_size != source["bytes"] or stat.st_mtime_ns != source["mtime_ns"]:
            raise ValueError("Raw archive changed during run")
    data = pd.concat(frames, ignore_index=True)
    write_reports(data, cfg, out)
    audit = dict(status="passed", rule_version=RULE_VERSION, periods=len(data),
                 entries=int(data.status.isin(ACTIVE).sum()), outputs={name:sha(out/name) for name in
                     ("sessions.csv", "summary.csv", "fold_metrics.csv", "yearly_metrics.csv", "cohort_metrics.csv", "quality.csv", "REPORT.md")})
    (out/"audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(f"Complete: {out.resolve()}/REPORT.md", flush=True)


if __name__ == "__main__":
    main()
