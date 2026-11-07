"""Equal-distance TP/SL order study, reusing previously built POCs."""
import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from observe import iso, load_window, metadata
from observe_four_profiles import clocks, quality


def aligned_bias(opening, before_13, poc):
    change = int(np.sign(before_13 - opening))
    position = int(np.sign(before_13 - poc))
    return change if change != 0 and change == position else 0


def outcome(prices, poc, morning_bias, mode):
    entry = float(prices[0])
    side = -int(morning_bias) if mode in ("endpoint", "aligned") else int(np.sign(poc - entry))
    if not side:
        return dict(status="flat_bias_or_at_poc")
    risk = side * (poc - entry)
    if risk <= 0:
        return dict(status="poc_wrong_side", entry=entry, side=side, tp=poc)
    stop = 2 * entry - poc
    if stop <= 0:
        return dict(status="nonpositive_stop", entry=entry, side=side, tp=poc)
    delta = side * (prices[1:] - entry)
    tp = np.flatnonzero(delta >= risk)
    sl = np.flatnonzero(delta <= -risk)
    itp = int(tp[0]) + 1 if len(tp) else len(prices)
    isl = int(sl[0]) + 1 if len(sl) else len(prices)
    if itp < isl:
        status, index, result_r = "tp_first", itp, 1.
    elif isl < itp:
        status, index, result_r = "sl_first", isl, -1.
    else:
        status, index = "time_exit", len(prices) - 1
        result_r = float(side * (prices[-1] - entry) / risk)
    return dict(status=status, entry=entry, side=side, tp=float(poc), sl=float(stop),
                risk_price=float(risk), risk_bps=10000 * risk / entry,
                exit_index=index, trigger_trade_price=float(prices[index]), gross_r=result_r)


def summarize(df):
    rows = []
    for (symbol, profile), g in df.groupby(["symbol", "profile"], sort=False):
        counts = g.status.value_counts()
        tp, sl, timeout = [int(counts.get(k, 0)) for k in ["tp_first", "sl_first", "time_exit"]]
        entries = tp + sl + timeout
        trades = g.loc[g.status.isin(["tp_first", "sl_first", "time_exit"])]
        rows.append(dict(symbol=symbol, profile=profile, entries=entries, tp_wins=tp,
                         sl_losses=sl, time_exits=timeout,
                         tp_pct_all_entries=100 * tp / entries if entries else None,
                         tp_pct_resolved=100 * tp / (tp + sl) if tp + sl else None,
                         positive_gross_closes=int(trades.gross_r.gt(0).sum()) if len(trades) else 0,
                         mean_gross_r=float(trades.gross_r.mean()) if len(trades) else None,
                         median_risk_bps=float(trades.risk_bps.median()) if len(trades) else None,
                         skipped_wrong_side=int(counts.get("poc_wrong_side", 0)),
                         skipped_flat=int(counts.get("flat_bias_or_at_poc", 0)),
                         skipped_not_away=int(counts.get("not_moved_away", 0)),
                         skipped_bias_not_aligned=int(counts.get("bias_not_aligned", 0)),
                         other_excluded=len(g) - entries - int(counts.get("poc_wrong_side", 0))
                             - int(counts.get("flat_bias_or_at_poc", 0)) - int(counts.get("not_moved_away", 0))
                             - int(counts.get("bias_not_aligned", 0))))
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--profiles-run", type=Path, required=True)
    parser.add_argument("--bias-mode", required=True, choices=["endpoint", "poc", "aligned"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--all-valid", action="store_true", help="Include sessions not meeting previous moved-away filter")
    args = parser.parse_args()
    source = args.profiles_run
    cfg = json.loads((source / "config_snapshot.json").read_text())
    manifest = json.loads((source / "source_manifest.json").read_text())
    original = pd.read_csv(source / "daily.csv")
    if args.start:
        original = original.loc[original.session_date >= args.start]
    if args.end:
        original = original.loc[original.session_date <= args.end]
    if original.empty:
        raise ValueError("No source sessions in selected period")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    snapshot = dict(profiles_source=str(source.resolve()), bias_mode=args.bias_mode,
                    cohort="all_valid" if args.all_valid else "previous_moved_away",
                    entry="first aggregate trade at or after 13:00 NY",
                    exit="first TP/SL barrier crossing, otherwise last trade before 17:00 NY",
                    risk_reward="1:1 gross, barrier scoring; fees/slippage/latency not simulated",
                    source_profile_config=cfg, selected_start=original.session_date.min(),
                    selected_end=original.session_date.max())
    (out / "config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    frames = []
    for symbol, source_group in original.groupby("symbol", sort=False):
        path = args.root / cfg["assets"][symbol]
        recorded = manifest[symbol]
        if path.stat().st_size != recorded["bytes"] or path.stat().st_mtime_ns != recorded["mtime_ns"]:
            raise ValueError(f"Archive changed since profile build: {symbol}")
        f, mins, maxs = metadata(path)
        rows = []
        for count, (day, group) in enumerate(source_group.groupby("session_date", sort=True)):
            candidates = group.loc[group.status.eq("valid")]
            if not args.all_valid:
                candidates = candidates.loc[candidates.moved_away.eq(True)]
            arrays, raw_quality = None, None
            if len(candidates):
                _, _, _, pivot, end = clocks(date.fromisoformat(day))
                arrays = load_window(f, mins, maxs, pivot, end)
                raw_quality = quality(*arrays, pivot, end, cfg)
            for record in group.itertuples():
                row = dict(symbol=symbol, profile=record.profile, session_date=day,
                           profile_status=record.status, moved_away=record.moved_away)
                if record.status != "valid":
                    row["status"] = "source_" + record.status
                elif not args.all_valid and record.moved_away != True:
                    row["status"] = "not_moved_away"
                elif args.bias_mode == "aligned" and not aligned_bias(record.price_0930, record.price_before_1300, record.poc):
                    row["status"] = "bias_not_aligned"
                elif raw_quality != "valid":
                    row["status"] = "execution_" + str(raw_quality)
                else:
                    ts, prices, _, _ = arrays
                    bias = int(np.sign(record.price_before_1300 - record.price_0930))
                    row.update(morning_bias=bias, price_0930=record.price_0930,
                               price_before_1300=record.price_before_1300, poc=record.poc,
                               entry_timestamp_ny=iso(ts[0]))
                    result = outcome(prices, record.poc, bias, args.bias_mode)
                    index = result.pop("exit_index", None)
                    row.update(result)
                    if index is not None:
                        row["exit_timestamp_ny"] = iso(ts[index])
                rows.append(row)
            if count % 200 == 0:
                print(f"{symbol}: {day}", flush=True)
        df = pd.DataFrame(rows)
        df.to_csv(out / f"{symbol}_trades.csv", index=False)
        frames.append(df)
        summarize(pd.concat(frames, ignore_index=True)).to_csv(out / "summary.csv", index=False)
        print(f"{symbol} completed", flush=True)
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(out / "trades.csv", index=False)
    combined.groupby(["symbol", "profile", "status"]).size().rename("sessions").reset_index().to_csv(out / "statuses.csv", index=False)
    summary = summarize(combined)
    summary.to_csv(out / "summary.csv", index=False)
    yearly = [summarize(g).assign(year=y) for y, g in combined.groupby(combined.session_date.str[:4])]
    pd.concat(yearly, ignore_index=True).to_csv(out / "yearly_summary.csv", index=False)
    expected = len(cfg["assets"]) * len(cfg["profiles"])
    valid_counts = original.loc[original.status.eq("valid")].groupby("session_date").size()
    common = set(valid_counts.loc[valid_counts.eq(expected)].index)
    summarize(combined.loc[combined.session_date.isin(common)]).to_csv(out / "common_dates_summary.csv", index=False)
    (out / "DEFINITIONS.md").write_text(
        "Bias endpoint: sign(price before 13:00 - first price >=09:30). Entry uses opposite direction. Flat bias is skipped.\n"
        "Bias aligned: long requires price before 13:00 > price at 09:30 AND > POC; short requires < both. Equality or disagreement means no entry. Enter opposite direction.\n"
        "Bias poc: entry points toward the selected POC, regardless of morning endpoint direction.\n"
        "TP is the previously computed frozen POC. SL = 2*entry - POC. Skip if POC lies on the wrong side of the required trade or risk is zero.\n"
        "Entry at first trade >=13:00 NY; levels are evaluated on subsequent aggregate trades until strictly before 17:00. Same-timestamp trades follow archive ID order.\n"
        "TP-first = +1R, SL-first = -1R by barrier scoring. A price gap can overshoot a level; reported R is not an actual achievable execution fill.\n"
        "Time exits use last observed price before 17:00. TP win/all entries counts time exits in denominator; TP win/resolved excludes them. Positive gross closes includes positive time exits.\n"
        "POCs are reused; none are rebuilt. No fee, spread, slippage, latency or funding model. Results do not establish net profitability.\n"
        "Default cohort preserves the previous moved-away filter; --all-valid includes every valid profile session.\n",
        encoding="utf-8")
    print(summary.to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
