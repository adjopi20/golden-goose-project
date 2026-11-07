"""v19: 15m/30m/1h, half-way pullback, final-body retest; reuse v17 profiles."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
from pathlib import Path

import numpy as np
import pandas as pd

import observe_second_push as old

base = old.base
TIMEFRAMES = (15, 30, 60)
MODES = ("wick_half", "close_half")
MODEL = ["symbol", "timeframe", "pullback_mode"]
COMMON = ("session_date", "valid_session", "opening_eligible", "profile_start_ny", "profile_end_ny",
          "period_end_ny", "period_hours", "opening_price", "poc", "val", "vah", "profile_volume")
FIRST = ("touch_edge", "first_push_start_ny", "first_push_available_ny", "first_push_touch_ny", "first_push_close")


class WindowReader:
    """The existing raw-window definition, with bounded row-group reuse between days."""
    def __init__(self, path):
        self.file, self.mins, self.maxs = base.metadata(path)
        self.cache = OrderedDict()
        self.groups_read = 0

    def window(self, start, end):
        parts = []
        for number in np.flatnonzero((self.maxs >= start) & (self.mins < end)):
            number = int(number)
            if number not in self.cache:
                columns = ["timestamp", "price", "qty", "agg_trade_id"]
                table = self.file.read_row_group(number, columns=columns)
                self.cache[number] = [table[c].combine_chunks().to_numpy() for c in columns]
                self.groups_read += 1
                if len(self.cache) > 64:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(number)
            arrays = self.cache[number]
            keep = (arrays[0] >= start) & (arrays[0] < end)
            parts.append([a[keep] for a in arrays])
        return [np.concatenate([p[i] for p in parts]) if parts else np.array([], dtype=d)
                for i, d in enumerate((np.int64, float, float, np.int64))]

    def close(self):
        self.cache.clear()
        self.file.close()


def ms(value):
    return int(pd.Timestamp(value).value//1000000)


def first_from_cache(row, bars):
    if pd.isna(row.get("first_push_available_ny")):
        return None
    start, available = ms(row["first_push_start_ny"]), ms(row["first_push_available_ny"])
    matching = [b for b in bars if b["start"] == start]
    assert len(matching) == 1, "Cached first push missing from replay"
    bar = matching[0]
    edge = row["touch_edge"]
    assert bar["complete"] and bar["end"] == available
    assert np.isclose(bar["close"], row["first_push_close"], rtol=1e-12, atol=1e-12)
    assert old.body_contains(bar, row[edge]), "Cached body no longer touches VA"
    return dict(edge=edge, side=1 if edge == "val" else -1, close=float(row["first_push_close"]),
                available_ms=available, start_ms=start)


def candidates(bars, prices, ts, first, val, vah, poc):
    pullbacks = {mode: None for mode in MODES}
    entries = {mode: None for mode in MODES}
    if first is None:
        return pullbacks, entries
    level = val if first["edge"] == "val" else vah
    midpoint = (level+poc)/2
    for b in bars:
        if b["start"] < first["available_ms"] or not b["complete"]:
            continue
        inside_closer = val < b["close"] < vah and abs(b["close"]-poc) < abs(first["close"]-poc)
        for mode in MODES:
            if entries[mode] is not None:
                continue
            if pullbacks[mode] is None:
                value = b["close"] if mode == "close_half" else float(
                    prices[b["i"]:b["j"]].min() if first["edge"] == "vah" else prices[b["i"]:b["j"]].max())
                enough = value <= midpoint if first["edge"] == "vah" else value >= midpoint
                if inside_closer and enough:
                    pullbacks[mode] = dict(start_ms=b["start"], available_ms=b["end"], close=b["close"],
                        midpoint=midpoint, depth_price=value, depth_fraction=first["side"]*(value-level)/abs(poc-level))
                continue  # The pullback cannot also be the retest candle.
            if old.body_contains(b, level):
                entries[mode] = dict(entry_index=int(np.searchsorted(ts, b["end"])), signal_ms=b["end"],
                    signal_index=None, second_bar_start_ms=b["start"], second_bar_end_ms=b["end"],
                    second_bar_open=b["open"], signal_body_price=b["close"])
        if all(v is not None for v in entries.values()):
            break
    return pullbacks, entries


def analyse(day, arrays, cached_rows, cfg):
    _, opening, end = base.bounds(day)
    ts, prices, qty, ids = arrays
    pivot = int(np.searchsorted(ts, opening)) if len(ts) else 0
    rows = []
    for minutes in TIMEFRAMES:
        source = cached_rows[old.timeframe_name(minutes)]
        common = {k: v for k, v in source.items() if k in COMMON and pd.notna(v)}
        common.update(timeframe=old.timeframe_name(minutes), entry_mode="body_close",
                      profile_reused=bool(source["valid_session"]), source_v17_status=source["status"])
        valid, eligible = bool(source["valid_session"]), bool(source["opening_eligible"])
        first = None
        if valid and eligible and pd.notna(source.get("first_push_available_ny")):
            assert len(ts) and np.all(np.diff(ts) >= 0)
            assert base.quality(ts[pivot:], prices[pivot:], qty[pivot:], ids[pivot:], opening, end, cfg) == "valid"
            assert float(prices[pivot]) == source["opening_price"]
            bars = old.utc_bars(ts, prices, opening, end, minutes)
            first = first_from_cache(source, bars)
            pullbacks, entries = candidates(bars, prices, ts, first, source["val"], source["vah"], source["poc"])
        for mode in MODES:
            row = dict(common, pullback_mode=mode)
            if not valid:
                row["status"] = "source_"+source["status"]
            elif not eligible:
                row["status"] = "open_not_inside_va"
            elif first is None:
                row["status"] = "no_first_body_push"
            else:
                row.update({k: source[k] for k in FIRST})
                pb = pullbacks[mode]
                if pb is None:
                    row["status"] = "no_half_pullback"
                else:
                    row.update(pullback_start_ny=base.iso(pb["start_ms"]), pullback_available_ny=base.iso(pb["available_ms"]),
                        pullback_close=pb["close"], half_level=pb["midpoint"], pullback_depth_price=pb["depth_price"],
                        pullback_depth_fraction=pb["depth_fraction"])
                    entry = entries[mode]
                    if entry is None:
                        row["status"] = "no_second_body_push"
                    else:
                        assert entry["second_bar_start_ms"] >= pb["available_ms"]
                        row = old.score(row, entry, ts, prices, ids, source["poc"], first["side"], end, cfg)
                        same = (row.get("entry_agg_trade_id") == source.get("entry_agg_trade_id")
                            and row.get("entry_timestamp_ny") == source.get("entry_timestamp_ny"))
                        row["same_entry_as_v17"] = bool(same)
                        if same and row["status"] in base.ACTIVE:
                            assert row["status"] == source["status"]
                            for col in ("entry", "tp", "sl", "gross_r", "net_r", "exit_price", "risk_price"):
                                assert np.isclose(row[col], source[col], rtol=1e-10, atol=1e-10), (day, minutes, mode, col)
                            assert row["exit_agg_trade_id"] == source["exit_agg_trade_id"]
            rows.append(row)
    return rows


def report(data, source, cfg, output):
    source_folds = pd.read_csv(source/"fold_metrics.csv")
    summaries, fold_rows, years, test_groups = [], [], [], []
    for values, g in data.groupby(MODEL, sort=True):
        identity = dict(zip(MODEL, values))
        valid = g.loc[g.valid_session]
        entries = valid.loc[valid.status.isin(base.ACTIVE)]
        summaries.append(dict(**identity, sample="all_available", **old.metrics(entries, valid, cfg)))
        calendar = source_folds.loc[source_folds.symbol.eq(identity["symbol"]) & source_folds.timeframe.eq(identity["timeframe"])
            & source_folds.entry_mode.eq("body_close") & source_folds.phase.eq("test")]
        test_parts = []
        for meta in calendar.to_dict("records"):
            tr = valid.loc[valid.session_date.ge(meta["train_start"]) & valid.session_date.lt(meta["test_start"])]
            te = valid.loc[valid.session_date.ge(meta["test_start"]) & valid.session_date.lt(meta["test_end_exclusive"])]
            train = tr.loc[tr.status.isin(base.ACTIVE)]
            cutoff = pd.Timestamp(meta["test_start"]).tz_localize(base.NY).tz_convert("UTC")
            available = pd.to_datetime(train.exit_timestamp_ny, utc=True, format="ISO8601").lt(cutoff) if len(train) else pd.Series(dtype=bool)
            for phase, population, trades in (("train", tr, train.loc[available]), ("test", te, te.loc[te.status.isin(base.ACTIVE)])):
                fold_rows.append(dict(**identity, fold=meta["fold"], phase=phase, train_start=meta["train_start"],
                    test_start=meta["test_start"], test_end_exclusive=meta["test_end_exclusive"],
                    purged_train_outcomes=int((~available).sum()), limited_sample=len(trades) < 20,
                    **old.metrics(trades, population, cfg)))
            test_parts.append(te)
        tested = pd.concat(test_parts, ignore_index=True) if test_parts else valid.iloc[:0]
        assert not tested.session_date.duplicated().any()
        test_groups.append(tested)
        summaries.append(dict(**identity, sample="walk_forward_test", **old.metrics(tested.loc[tested.status.isin(base.ACTIVE)], tested, cfg)))
        for sample, p in (("all_available", valid), ("walk_forward_test", tested)):
            for year, z in p.groupby(p.session_date.str[:4]):
                years.append(dict(**identity, sample=sample, year=year, **old.metrics(z.loc[z.status.isin(base.ACTIVE)], z, cfg)))
    tests = pd.concat(test_groups, ignore_index=True)
    common = set.intersection(*(set(tests.loc[tests.symbol.eq(s), "session_date"]) for s in cfg["assets"]))
    common_rows = []
    for values, p in tests.groupby(MODEL):
        z = p.loc[p.session_date.isin(common)]
        common_rows.append(dict(zip(MODEL, values), sample="common_test", **old.metrics(z.loc[z.status.isin(base.ACTIVE)], z, cfg)))
    summary = pd.DataFrame(summaries)
    folds = pd.DataFrame(fold_rows)
    comparison = summary.copy()
    original = pd.read_csv(source/"summary.csv")
    original = original.loc[original.timeframe.isin([old.timeframe_name(m) for m in TIMEFRAMES]) & original.entry_mode.eq("body_close")].copy()
    original["pullback_mode"] = "v17_original"
    comparison = pd.concat([comparison, original], ignore_index=True)
    comparison.to_csv(output/"baseline_comparison.csv", index=False)
    summary.to_csv(output/"summary.csv", index=False)
    folds.to_csv(output/"fold_metrics.csv", index=False)
    pd.DataFrame(years).to_csv(output/"yearly_metrics.csv", index=False)
    pd.DataFrame(common_rows).to_csv(output/"common_test_summary.csv", index=False)
    data.groupby(MODEL+["status"], dropna=False).size().reset_index(name="periods").to_csv(output/"quality.csv", index=False)
    lines = ["# Half-way pullback, body-close retest — v19", "",
        "15m/30m/1h UTC; previous NY 09:30–09:30 profile; NY open inside VA. A separate completed inside/closer pullback candle "
        "must reach half-way by wick or by close, then a later final body retests the same VA edge. Entry first print after retest close. "
        "TP POC / SL 1:1; max one opportunity per profile/model. No ADX, other indicator filter or profile rebuild.", "",
        "Fee 4 bps EACH side. Mean R is raw trade R; account metrics use 0.5% price risk with 5x entry notional cap. "
        "WR includes time exits. No extra slippage/funding. Different model_development/models/assets are independent hypothetical accounts.", "",
        "Original v17 asset calendars: 8 months train / 2 test / step 2. No parameter fitting. Previously observed development history. "
        "Training windows overlap. Changes may delay the entry rather than merely remove old trades.", ""]
    for symbol, z in comparison.loc[comparison["sample"].eq("walk_forward_test")].groupby("symbol"):
        lines += [f"## {symbol}", "", "| Candle | Pullback | Trades | Gross WR | Net WR | Gross mean R | Net mean R | Total net R |",
                  "|---|---|---:|---:|---:|---:|---:|---:|"]
        for minutes in TIMEFRAMES:
            for mode in ("v17_original",)+MODES:
                r = z.loc[z.timeframe.eq(old.timeframe_name(minutes)) & z.pullback_mode.eq(mode)].iloc[0]
                lines.append(f"| {r.timeframe} | {mode} | {int(r.trades)} | {r.get('gross_wr_pct', np.nan):.1f}% | "
                    f"{r.get('net_wr_pct', np.nan):.1f}% | {r.get('gross_mean_r', np.nan):+.4f} | "
                    f"{r.get('net_mean_r', np.nan):+.4f} | {r.get('net_total_r', 0):+.2f} |")
        lines.append("")
    lines += [f"Common valid test dates across assets: {len(common)}; see common_test_summary.csv.", "",
        "Full all-available, yearly, fold, no-entry and quality denominators remain in CSVs. Test groups with fewer than 20 trades "
        "are limited evidence. Trigger-print fills and last-print time exits are observation proxies; account drawdown measures closed trades only."]
    (output/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def run(args):
    source = args.source.resolve()
    source_audit = json.loads((source/"audit.json").read_text())
    assert source_audit["status"] == "passed"
    for name, digest in source_audit["outputs"].items():
        assert base.sha(source/name) == digest, "Changed v17 artifact: "+name
    previous = json.loads((source/"config_snapshot.json").read_text())
    assert previous["rule_version"] == "second_push_utc_v17_1"
    assert previous["code_sha256"] == base.sha(old.__file__)
    for path, digest in previous["helpers"].items():
        assert base.sha(path) == digest, "Changed v17 helper: "+path
    cfg = dict(previous["config"], timeframes_minutes=list(TIMEFRAMES), entry_modes=["body_close"],
               pullback_modes=list(MODES), pullback_fraction=.5)
    assert set(cfg["assets"]) == {"BTCUSDC", "ETHUSDC", "XRPUSDT", "LTCUSDC"}
    cached = pd.read_csv(source/"sessions.csv", float_precision="round_trip")
    cached = cached.loc[cached.entry_mode.eq("body_close") & cached.timeframe.isin([old.timeframe_name(m) for m in TIMEFRAMES])]
    assert not cached.duplicated(["symbol", "session_date", "timeframe"]).any()
    expected = sorted(cached.session_date.unique())
    design = Path(__file__).with_name("HALF_PULLBACK_DESIGN.md")
    snapshot = dict(rule_version="half_pullback_body_close_v19_1", config=cfg, source_run=str(source),
        source_audit_sha256=base.sha(source/"audit.json"), code_sha256=base.sha(__file__),
        design_sha256=base.sha(design), source_snapshot=previous,
        execution_helpers={str(Path(module.__file__).resolve()):base.sha(module.__file__) for module in
            (old, base, __import__("observe"), __import__("observe_four_profiles"), __import__("backtest_poc_rr"))})
    output = args.output
    if args.resume:
        assert json.loads((output/"config_snapshot.json").read_text()) == snapshot, "Resume inputs changed"
    else:
        output.mkdir(parents=True, exist_ok=False)
        (output/"config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (output/"audit.json").write_text(json.dumps(dict(status="running")))
    frames, reads = [], {}
    for symbol in cfg["assets"]:
        info = previous["sources"][symbol]
        path = args.root/cfg["assets"][symbol]
        stat = path.stat()
        assert stat.st_size == info["bytes"] and stat.st_mtime_ns == info["mtime_ns"], "Raw source changed"
        reader = WindowReader(path)
        by_day = {day: dict(zip(z.timeframe, z.to_dict("records"))) for day, z in cached.loc[cached.symbol.eq(symbol)].groupby("session_date")}
        assert sorted(by_day) == expected
        checkpoint = output/f"{symbol}_daily.jsonl"
        saved = [json.loads(line) for line in checkpoint.read_text().splitlines() if line] if checkpoint.exists() else []
        assert [d["session_date"] for d in saved] == expected[:len(saved)]
        assert all(len(d["rows"]) == 6 and {(r["timeframe"], r["pullback_mode"]) for r in d["rows"]} ==
            {(old.timeframe_name(m), mode) for m in TIMEFRAMES for mode in MODES} for d in saved)
        with checkpoint.open("a", encoding="utf-8") as handle:
            for n in range(len(saved), len(expected)):
                day = expected[n]
                old_rows = by_day[day]
                needs_path = any(bool(r["valid_session"]) and bool(r["opening_eligible"])
                    and pd.notna(r.get("first_push_available_ny")) for r in old_rows.values())
                _, opening, end = base.bounds(pd.Timestamp(day).date())
                arrays = reader.window(opening//3600000*3600000, end) if needs_path else [np.array([], dtype=d) for d in (np.int64,float,float,np.int64)]
                rows = analyse(pd.Timestamp(day).date(), arrays, old_rows, cfg)
                for row in rows:
                    row["symbol"] = symbol
                d = dict(session_date=day, rows=rows)
                handle.write(json.dumps(d, allow_nan=False)+"\n")
                handle.flush()
                saved.append(d)
                if n % 100 == 0:
                    print(f"{symbol} {day}: {sum(r['status'] in base.ACTIVE for r in rows)} entries; {reader.groups_read} row groups read", flush=True)
        frames.append(pd.DataFrame([r for d in saved for r in d["rows"]]))
        reads[symbol] = reader.groups_read
        reader.close()
        stat = path.stat()
        assert stat.st_size == info["bytes"] and stat.st_mtime_ns == info["mtime_ns"]
        print(f"{symbol}: {len(saved)} periods, six variants", flush=True)
    data = pd.concat(frames, ignore_index=True)
    assert len(data) == len(expected)*24 and not data.duplicated(MODEL+["session_date"]).any()
    active = data.loc[data.status.isin(base.ACTIVE)]
    assert active.pullback_depth_fraction.ge(.5-1e-12).all()
    data.to_csv(output/"sessions.csv", index=False)
    report(data, source, cfg, output)
    (output/"DESIGN.md").write_bytes(design.read_bytes())
    assert base.sha(source/"audit.json") == snapshot["source_audit_sha256"]
    audit = dict(status="passed", periods_per_asset=len(expected), combinations=24, rows=len(data), entries=len(active),
        profiles_built=0, profiles_source="v17", raw_row_groups_read=reads,
        matching_v17_entries_checked=int(active.same_entry_as_v17.fillna(False).sum()),
        outputs={p.name:base.sha(p) for p in output.iterdir() if p.name != "audit.json"})
    (output/"audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in audit.items() if k != "outputs"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--source", type=Path, required=True, help="Completed immutable second_push_utc_v17 run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())
