"""v17: UTC candle second-push comparison; no orderflow or parameter fitting."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import previous_value_area_reversal as base


def timeframe_name(minutes):
    return f"{minutes//60}h" if minutes % 60 == 0 else f"{minutes}m"


def utc_bars(ts, prices, opening, end, minutes):
    """Original UTC opens/closes, including the candle already open at NY open."""
    width = minutes*60000
    starts = np.arange(opening//width*width, end, width, dtype=np.int64)
    left = np.searchsorted(ts, starts)
    right = np.searchsorted(ts, np.minimum(starts+width, end))
    bars = []
    for start, i, j in zip(starts, left, right):
        if i == j:
            continue
        bars.append(dict(start=int(start), end=int(start+width), i=int(i), j=int(j),
                         open=float(prices[i]), close=float(prices[j-1]), complete=bool(start+width <= end)))
    return bars


def first_cross(prices, level, i, j):
    if i >= j:
        return None
    p = prices[i:j]
    before = np.r_[p[0], p[:-1]]
    hits = np.flatnonzero((p == level) | ((p-level)*(before-level) < 0))
    return i+int(hits[0]) if len(hits) else None


def body_contains(bar, level):
    return min(bar["open"], bar["close"]) <= level <= max(bar["open"], bar["close"])


def pattern(ts, prices, opening, end, val, vah, poc, minutes):
    first = pullback = None
    candidates = {"intrabar": None, "body_close": None}
    ny_index = int(np.searchsorted(ts, opening))
    for number, bar in enumerate(utc_bars(ts, prices, opening, end, minutes)):
        if first is None:
            if not bar["complete"]:
                continue
            touches = []
            for edge, level, side in (("val", val, 1), ("vah", vah, -1)):
                if body_contains(bar, level):
                    i = max(bar["i"], ny_index)
                    hit = first_cross(prices, level, i, bar["j"])
                    if hit is not None:
                        before = float(prices[max(hit-1, i)])
                        touches.append((hit, abs(level-before), edge, side))
            if touches:
                hit, _, edge, side = min(touches)
                first = dict(bar=number, edge=edge, side=side, close=bar["close"],
                             available_ms=bar["end"], touch_ms=int(ts[hit]),
                             start_ms=bar["start"], distance=abs(bar["close"]-poc))
            continue
        if pullback is None:
            if (bar["complete"] and val < bar["close"] < vah
                    and abs(bar["close"]-poc) < first["distance"]):
                pullback = dict(bar=number, available_ms=bar["end"], close=bar["close"], start_ms=bar["start"])
            continue
        level = val if first["edge"] == "val" else vah
        if candidates["intrabar"] is None:
            # A LIVE body is [bar open, current print]. Do not inspect this
            # candle's eventual close to decide the intrabar entry.
            p = prices[bar["i"]:bar["j"]]
            hits = np.flatnonzero((np.minimum(bar["open"], p) <= level) & (np.maximum(bar["open"], p) >= level))
            if len(hits):
                hit = bar["i"]+int(hits[0])
                candidates["intrabar"] = dict(entry_index=hit+1, signal_ms=int(ts[hit]), signal_index=hit,
                    second_bar_start_ms=bar["start"], second_bar_end_ms=bar["end"], second_bar_open=bar["open"],
                    signal_body_price=float(prices[hit]))
        if candidates["body_close"] is None and bar["complete"] and body_contains(bar, level):
            candidates["body_close"] = dict(entry_index=int(np.searchsorted(ts, bar["end"])),
                signal_ms=bar["end"], signal_index=None, second_bar_start_ms=bar["start"],
                second_bar_end_ms=bar["end"], second_bar_open=bar["open"], signal_body_price=bar["close"])
        if all(v is not None for v in candidates.values()):
            break
    return first, pullback, candidates


def score(row, candidate, ts, prices, ids, poc, side, end, cfg):
    i = candidate["entry_index"]
    row.update(signal_timestamp_ny=base.iso(candidate["signal_ms"]),
               second_bar_start_ny=base.iso(candidate["second_bar_start_ms"]),
               second_bar_end_ny=base.iso(candidate["second_bar_end_ms"]),
               second_bar_open=candidate["second_bar_open"], signal_body_price=candidate["signal_body_price"])
    if i >= len(ts) or ts[i] >= end:
        return dict(row, status="no_fill_before_period_end")
    row.update(entry_timestamp_ny=base.iso(ts[i]), entry_agg_trade_id=int(ids[i]))
    if candidate["signal_index"] is not None:
        assert i > candidate["signal_index"]  # Same millisecond, next ID is allowed.
        row["signal_agg_trade_id"] = int(ids[candidate["signal_index"]])
    assert int(ts[i]) >= candidate["signal_ms"]
    result = base.outcome(prices[i:], poc, -side, "endpoint")
    j = result.pop("exit_index", None)
    row.update(result)
    if j is None:
        return row
    entry, risk, exit_price = result["entry"], result["risk_price"], float(prices[i+j])
    gross = side*(exit_price-entry)/risk
    cost = .0001*(entry+exit_price)/risk
    row.update(barrier_gross_r=result["gross_r"], gross_r=gross, exit_price=exit_price,
               exit_timestamp_ny=base.iso(ts[i+j]), exit_agg_trade_id=int(ids[i+j]),
               cost_r_per_bps=cost, net_r=gross-cfg["fee_bps_per_side"]*cost,
               target_distance_pct=100*risk/entry, hold_minutes=float((ts[i+j]-ts[i])/60000))
    assert ts[i+j] < end and np.isclose(row["sl"], 2*entry-poc)
    return row


def analyse(day, arrays, cfg, builder, cached=None):
    previous, opening, end = base.bounds(day)
    ts, prices, qty, ids = arrays
    common = dict(session_date=str(day), valid_session=False, opening_eligible=False,
                  profile_start_ny=base.iso(previous), profile_end_ny=base.iso(opening),
                  period_end_ny=base.iso(end), period_hours=(end-opening)/3600000)
    status = "valid"
    if len(ts) and (np.diff(ts) < 0).any():
        status = "unordered_timestamps"
    pivot = int(np.searchsorted(ts, opening)) if status == "valid" else 0
    if status == "valid" and cached is None:
        q = base.quality(*(a[:pivot] for a in arrays), previous, opening, cfg)
        if q != "valid":
            status = "profile_"+q
    if status == "valid":
        q = base.quality(*(a[pivot:] for a in arrays), opening, end, cfg)
        if q != "valid":
            status = "observation_"+q
    if status == "valid" and (pivot == 0 or ids[pivot] != ids[pivot-1]+1):
        status = "opening_aggregate_id_gap"
    if status == "valid":
        if cached is None:
            vp = builder(pd.DataFrame(dict(price=prices[:pivot], qty=qty[:pivot])),
                         n_bins=cfg["profile_bins"], value_area_pct=cfg["value_area_fraction"])
            poc, val, vah = (float(vp[k]) for k in ("poc_price", "val", "vah"))
            volume = float(vp["total_volume"])
        else:
            poc, val, vah = (float(cached[k]) for k in ("poc", "val", "vah"))
            volume = float(cached["profile_volume"])
        if not 0 < val < poc < vah:
            status = "degenerate_profile"
        else:
            op = float(prices[pivot])
            common.update(valid_session=True, opening_eligible=bool(val < op < vah), opening_price=op,
                          poc=poc, val=val, vah=vah, profile_volume=volume,
                          profile_reused=cached is not None)
            if not common["opening_eligible"]:
                status = "open_not_inside_va"
    rows = []
    for minutes in cfg["timeframes_minutes"]:
        first = pullback = None
        candidates = {}
        if status == "valid":
            first, pullback, candidates = pattern(ts, prices, opening, end, val, vah, poc, minutes)
        for mode in cfg["entry_modes"]:
            row = dict(common, timeframe=timeframe_name(minutes), entry_mode=mode, status=status)
            if status != "valid":
                rows.append(row)
                continue
            if first is None:
                row["status"] = "no_first_body_push"
            else:
                row.update(touch_edge=first["edge"], first_push_start_ny=base.iso(first["start_ms"]),
                           first_push_available_ny=base.iso(first["available_ms"]),
                           first_push_touch_ny=base.iso(first["touch_ms"]), first_push_close=first["close"])
                if pullback is None:
                    row["status"] = "no_completed_pullback"
                else:
                    row.update(pullback_start_ny=base.iso(pullback["start_ms"]),
                               pullback_available_ny=base.iso(pullback["available_ms"]), pullback_close=pullback["close"])
                    candidate = candidates[mode]
                    if candidate is None:
                        row["status"] = "no_second_push"
                    else:
                        assert pullback["bar"] > first["bar"]
                        assert candidate["second_bar_start_ms"] >= pullback["available_ms"]
                        row = score(row, candidate, ts, prices, ids, poc, first["side"], end, cfg)
            rows.append(row)
    return rows


def blank_rows(day, cfg, status):
    return [dict(session_date=str(day), timeframe=timeframe_name(minutes), entry_mode=mode,
                 valid_session=False, opening_eligible=False, status=status)
            for minutes in cfg["timeframes_minutes"] for mode in cfg["entry_modes"]]


def metrics(frame, population, cfg):
    result = base.stats(frame, len(population), cfg)
    n = int(population.opening_eligible.sum())
    result.update(opening_eligible_sessions=n, entry_pct_opening_eligible=100*len(frame)/n if n else None)
    if frame.empty:
        for label in ("gross", "net"):
            result.update({label+"_total_r":0., label+"_account_return_pct":0., label+"_capital_base_r":0.})
    return result


def report(data, cfg, out):
    keys = ["symbol", "timeframe", "entry_mode"]
    summaries, fold_rows, years, test_parts = [], [], [], []
    for values, group in data.groupby(keys, sort=True):
        identity = dict(zip(keys, values))
        valid = group.loc[group.valid_session]
        trades = valid.loc[valid.status.isin(base.ACTIVE)]
        summaries.append(dict(identity, sample="all_available", **metrics(trades, valid, cfg)))
        owned_tests = []
        if len(valid):
            for fold, (start, boundary, finish) in enumerate(base.fold_schedule(valid.session_date.min(), valid.session_date.max()), 1):
                tr = valid.loc[valid.session_date.ge(str(start.date())) & valid.session_date.lt(str(boundary.date()))]
                te = valid.loc[valid.session_date.ge(str(boundary.date())) & valid.session_date.lt(str(finish.date()))]
                train = tr.loc[tr.status.isin(base.ACTIVE)]
                cutoff = pd.Timestamp(boundary.date()).tz_localize(base.NY).tz_convert("UTC")
                available = pd.to_datetime(train.exit_timestamp_ny, utc=True, format="ISO8601").lt(cutoff) if len(train) else pd.Series(dtype=bool)
                for phase, population, entries in (("train", tr, train.loc[available]), ("test", te, te.loc[te.status.isin(base.ACTIVE)])):
                    fold_rows.append(dict(identity, fold=fold, phase=phase, train_start=str(start.date()),
                         test_start=str(boundary.date()), test_end_exclusive=str(finish.date()),
                         purged_train_outcomes=int((~available).sum()), limited_sample=len(entries)<20,
                         **metrics(entries, population, cfg)))
                owned_tests.append(te)
        tested = pd.concat(owned_tests, ignore_index=True) if owned_tests else valid.iloc[:0]
        assert not tested.session_date.duplicated().any()
        test_parts.append(tested)
        summaries.append(dict(identity, sample="walk_forward_test", **metrics(tested.loc[tested.status.isin(base.ACTIVE)], tested, cfg)))
        for sample, population in (("all_available", valid), ("walk_forward_test", tested)):
            for year, part in population.groupby(population.session_date.str[:4]):
                years.append(dict(identity, sample=sample, year=year,
                                  **metrics(part.loc[part.status.isin(base.ACTIVE)], part, cfg)))
    summary = pd.DataFrame(summaries)
    folds = pd.DataFrame(fold_rows, columns=None if fold_rows else keys+["fold", "phase", "trades"])
    summary.to_csv(out/"summary.csv", index=False)
    folds.to_csv(out/"fold_metrics.csv", index=False)
    pd.DataFrame(years, columns=None if years else keys+["sample", "year", "trades"]).to_csv(out/"yearly_metrics.csv", index=False)
    data.to_csv(out/"sessions.csv", index=False)
    data.groupby(keys+["status"]).size().rename("periods").reset_index().to_csv(out/"quality.csv", index=False)
    tests = pd.concat(test_parts, ignore_index=True)
    sets = [set(tests.loc[tests.symbol.eq(s), "session_date"]) for s in cfg["assets"]]
    common = set.intersection(*sets)
    common_rows = []
    for values, group in tests.groupby(keys):
        part = group.loc[group.session_date.isin(common)]
        common_rows.append(dict(zip(keys, values), **metrics(part.loc[part.status.isin(base.ACTIVE)], part, cfg)))
    pd.DataFrame(common_rows, columns=None if common_rows else keys+["trades"]).to_csv(out/"common_test_summary.csv", index=False)
    lines = ["# UTC second push: 40 fixed combinations — v17", "",
             "NY opening strictly inside yesterday's frozen 09:30–09:30 VA; first FINAL-body push, "
             "a separate completed inside/closer-to-POC pullback candle, then second push at the same boundary. "
             "UTC exchange candles 15m/30m/1h/2h/4h. First push contact must occur after NY opening; a straddling UTC candle may qualify. "
             "Intrabar uses a LIVE body and next-print entry; body_close uses a FINAL body and first print at/after candle end. "
             "No orderflow, fitting, extra filters or new sizing. TP is frozen POC, planned stop 1:1, one opportunity per NY profile period.", "",
             f"Fee {cfg['fee_bps_per_side']} bps EACH side; illustration risk {100*cfg['account_risk_fraction']}% current equity, "
             f"entry notional cap {cfg['max_entry_notional_equity']}x equity. Assets/combinations are separate accounts, not pooled. "
             "Returns cumulative, not annual. Raw R and capped capital-risk R differ.", "",
             "Chronological readout: 8 calendar months train / 2 test / step 2; no threshold selection. "
             "Training periods overlap and cannot be pooled. Only complete test blocks enter this table; all available dates and yearly results remain in CSVs. "
             "This is previously studied development history with 40 comparisons, not 40 independent confirmations or a new untouched holdout.", ""]
    for symbol, g in summary.loc[summary["sample"].eq("walk_forward_test")].groupby("symbol"):
        order = {timeframe_name(m):i for i,m in enumerate(cfg["timeframes_minutes"])}
        g = g.assign(_order=g.timeframe.map(order)).sort_values(["_order", "entry_mode"])
        lines += [f"## {symbol}", "", "| Candle | Entry | Valid / inside sessions | Trades | Gross WR | Gross mean R | Net mean R | Net account | Median TP |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for r in g.to_dict("records"):
            def fmt(k, suffix=""):
                return f"{r[k]:.3f}{suffix}" if pd.notna(r.get(k)) else "—"
            lines.append(f"| {r['timeframe']} | {r['entry_mode']} | {r['valid_sessions']} / {r['opening_eligible_sessions']} | {r['trades']} | "
                         +" | ".join(fmt(k, "%" if k.endswith("pct") else "") for k in
                                     ("gross_wr_pct", "gross_mean_r", "net_mean_r", "net_account_return_pct", "median_target_pct"))+" |")
        lines.append("")
    lines += [f"Common valid test dates across four assets: {len(common)}; range "
              f"{min(common) if common else 'none'}–{max(common) if common else 'none'}. See common_test_summary.csv; original fold calendars are unchanged.", "",
              "Read quality.csv and coverage in summary.csv: no-entry and unavailable dates remain visible. "
              "A smaller timeframe has more opportunities to complete the pattern within the same period; compare frequency and per-trade quality together. "
              "Use fold_metrics.csv for limited samples below 20 trades and variation; do not promote a best-looking combination from this table alone.", "",
              "Observed next-print entries and trigger-print exits are proxies. Funding, spread, latency, nonlinear impact, liquidation and intratrade drawdown are unmodeled. "
              "Time exit marks the last print before next 09:30. Closed-trade drawdown is not intratrade drawdown. See DESIGN.md for the exact state machine."]
    (out/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def profile_cache(path, sources, cfg):
    if path is None:
        return {}, None
    audit = json.loads((path/"audit.json").read_text())
    assert audit["status"] == "passed"
    for name, digest in audit["outputs"].items():
        assert base.sha(path/name) == digest, f"Changed profile cache: {name}"
    snapshot = json.loads((path/"config_snapshot.json").read_text())
    assert snapshot.get("rule_version") == "previous_value_area_touch_v14_1", "Use the original v14 profile cache"
    assert snapshot["code_sha256"] == base.sha(base.__file__), "Changed profile-cache implementation"
    for dependency, digest in snapshot["dependencies"].items():
        assert base.sha(dependency) == digest, f"Changed cached-profile dependency: {dependency}"
    for key in ("profile_bins", "value_area_fraction", "max_internal_gap_seconds", "max_boundary_gap_seconds"):
        assert snapshot["config"][key] == cfg[key], f"Cached profile definition changed: {key}"
    for symbol, old in snapshot["sources"].items():
        if symbol in sources:
            assert sources[symbol]["bytes"] == old["bytes"] and sources[symbol]["mtime_ns"] == old["mtime_ns"], "Raw archive changed since cached profiles"
    frame = pd.read_csv(path/"sessions.csv")
    assert not frame.duplicated(["symbol", "session_date"]).any()
    rows = {(r["symbol"], r["session_date"]):r for r in frame.to_dict("records")}
    return rows, dict(path=str(path.resolve()), audit_sha256=base.sha(path/"audit.json"),
                      sessions_sha256=base.sha(path/"sessions.csv"), config_sha256=base.sha(path/"config_snapshot.json"))


def run(args):
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if (cfg["timeframes_minutes"] != [15, 30, 60, 120, 240] or cfg["entry_modes"] != ["intrabar", "body_close"]
            or cfg["candle_anchor"] != "UTC_epoch" or len(cfg["assets"]) != 4):
        raise ValueError("This experiment requires four assets, five fixed UTC timeframes and both entry modes")
    if (not 0 < cfg["account_risk_fraction"] < 1 or not np.isfinite(cfg["fee_bps_per_side"]) or cfg["fee_bps_per_side"] < 0
            or not np.isfinite(cfg["max_entry_notional_equity"]) or cfg["max_entry_notional_equity"] <= 0
            or not isinstance(cfg["profile_bins"], int) or cfg["profile_bins"] <= 0
            or not 0 < cfg["value_area_fraction"] <= 1):
        raise ValueError("Invalid profile, risk or fee configuration")
    begin, finish = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if finish < begin:
        raise ValueError("End before start")
    sys.path.insert(0, str(args.root.resolve()))
    from indicator.volume_profile import build_basic_volume_profile
    sources = {}
    for symbol, relative in cfg["assets"].items():
        path = (args.root/relative).resolve()
        f, mins, maxs = base.metadata(path)
        stat = path.stat()
        sources[symbol] = dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
             rows=f.metadata.num_rows, min_timestamp_ms=int(mins.min()), max_timestamp_ms=int(maxs.max()))
    cached, cache_manifest = profile_cache(args.profiles_run, sources, cfg)
    snapshot = dict(rule_version="second_push_utc_v17_1", config=cfg, start=args.start, end=args.end,
                    sources=sources, cached_profiles=cache_manifest, code_sha256=base.sha(__file__),
                    design_sha256=base.sha(Path(__file__).with_name("SECOND_PUSH_DESIGN.md")),
                    helpers={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in
                        (base, sys.modules["observe"], sys.modules["observe_four_profiles"],
                         sys.modules["backtest_poc_rr"], sys.modules["indicator.volume_profile"])})
    out = args.output
    if args.resume:
        if json.loads((out/"config_snapshot.json").read_text()) != snapshot:
            raise ValueError("Resume inputs changed; use a new output directory")
    else:
        out.mkdir(parents=True, exist_ok=False)
        (out/"config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (out/"audit.json").write_text(json.dumps(dict(status="running")))
    frames, counts = [], dict(profiles_built=0, profiles_reused=0)
    expected = [str(begin+timedelta(days=i)) for i in range((finish-begin).days+1)]
    combination_set = {(timeframe_name(m), mode) for m in cfg["timeframes_minutes"] for mode in cfg["entry_modes"]}
    for symbol, source in sources.items():
        f, mins, maxs = base.metadata(Path(source["path"]))
        checkpoint = out/f"{symbol}_daily.jsonl"
        days = [json.loads(line) for line in checkpoint.read_text().splitlines() if line] if checkpoint.exists() else []
        if [d["session_date"] for d in days] != expected[:len(days)] or len(days) > len(expected):
            raise ValueError("Checkpoint dates duplicated or noncontiguous")
        for saved in days:
            assert len(saved["rows"]) == len(combination_set)
            assert {(r["timeframe"], r["entry_mode"]) for r in saved["rows"]} == combination_set
            assert all(r["symbol"] == symbol and r["session_date"] == saved["session_date"] for r in saved["rows"])
        with checkpoint.open("a", encoding="utf-8") as handle:
            for number in range(len(days), len(expected)):
                day = date.fromisoformat(expected[number])
                previous, opening, end = base.bounds(day)
                cache = cached.get((symbol, str(day)))
                built, reused = False, False
                if previous < source["min_timestamp_ms"] or end-60000 > source["max_timestamp_ms"]:
                    rows = blank_rows(day, cfg, "outside_full_archive")
                elif cache is not None and not cache["valid_session"]:
                    rows = blank_rows(day, cfg, "cached_"+str(cache["status"]))
                else:
                    # Cached profiles permit a shorter read, but preserve the
                    # full original UTC candle open for the 4h straddling bar.
                    read_start = opening//(240*60000)*(240*60000) if cache is not None else previous
                    arrays = base.load_window(f, mins, maxs, read_start, end)
                    rows = analyse(day, arrays, cfg, build_basic_volume_profile, cache)
                    valid = any(r["valid_session"] for r in rows)
                    built, reused = valid and cache is None, valid and cache is not None
                for row in rows:
                    row["symbol"] = symbol
                saved = dict(session_date=str(day), rows=rows, profile_built=bool(built), profile_reused=bool(reused))
                handle.write(json.dumps(saved, allow_nan=False)+"\n")
                handle.flush()
                days.append(saved)
                if number % 50 == 0:
                    print(f"{symbol} {day}: {rows[0]['status']}", flush=True)
        counts["profiles_built"] += sum(d["profile_built"] for d in days)
        counts["profiles_reused"] += sum(d["profile_reused"] for d in days)
        frames.append(pd.DataFrame([r for d in days for r in d["rows"]]))
        print(f"{symbol}: {len(days)} periods, 10 combinations each", flush=True)
    for source in sources.values():
        stat = Path(source["path"]).stat()
        if stat.st_size != source["bytes"] or stat.st_mtime_ns != source["mtime_ns"]:
            raise ValueError("Raw archive changed during run")
    if cache_manifest:
        for name, key in (("audit.json", "audit_sha256"), ("sessions.csv", "sessions_sha256"), ("config_snapshot.json", "config_sha256")):
            assert base.sha(args.profiles_run/name) == cache_manifest[key], "Cached profiles changed during run"
    data = pd.concat(frames, ignore_index=True)
    assert len(data) == len(expected)*40
    assert not data.duplicated(["symbol", "session_date", "timeframe", "entry_mode"]).any()
    report(data, cfg, out)
    design = Path(__file__).with_name("SECOND_PUSH_DESIGN.md")
    (out/"DESIGN.md").write_text(design.read_text(encoding="utf-8"), encoding="utf-8")
    outputs = {p.name:base.sha(p) for p in out.iterdir() if p.is_file() and p.name != "audit.json"}
    audit = dict(status="passed", periods_per_asset=len(expected), combinations=40, rows=len(data),
                 entries=int(data.status.isin(base.ACTIVE).sum()), **counts, outputs=outputs)
    (out/"audit.json").write_text(json.dumps(audit, indent=2))
    print(f"Complete: {out.resolve()}/REPORT.md", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("second_push_config.json"))
    parser.add_argument("--profiles-run", type=Path, help="Optional v14 cache; never reuse another profile definition")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())
