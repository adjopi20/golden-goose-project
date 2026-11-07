"""v20: post-execution absorption proxy / three-candle exhaustion at VA pushes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

import observe_half_pullback as half

old, base = half.old, half.base
TIMEFRAMES = (15, 30, 60)
SETUPS = ("first_push", "second_wick_half", "second_close_half")
MODEL = ["symbol", "timeframe", "setup"]
POLICIES = ("base_all", "absorption_matched", "absorption", "exhaustion_matched", "exhaustion")
EVENT_KEY = ["symbol", "session_date", "timeframe", "signal_bar_start_ny", "touch_edge"]
PARAMS = dict(zone_fraction=.10, response_fraction=.10, minimum_later_prints=3,
              pressure_quantile=.50, size_quantile=.75, progress_quantile=.50,
              progress_cap=.10, minimum_train_events=25, exhaustion_bars=3)


class FlowReader(half.WindowReader):
    def window(self, start, end):
        parts = []
        for number in np.flatnonzero((self.maxs >= start) & (self.mins < end)):
            number = int(number)
            if number not in self.cache:
                columns = ["timestamp", "price", "qty", "agg_trade_id", "is_buyer_maker"]
                table = self.file.read_row_group(number, columns=columns)
                arrays = [table[c].combine_chunks().to_numpy(zero_copy_only=False) for c in columns]
                if arrays[-1].dtype != np.bool_:
                    raise ValueError("Buyer-maker flags must be observed booleans")
                self.cache[number] = arrays
                self.groups_read += 1
                if len(self.cache) > 64:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(number)
            arrays = self.cache[number]
            keep = (arrays[0] >= start) & (arrays[0] < end)
            parts.append([a[keep] for a in arrays])
        return [np.concatenate([p[i] for p in parts]) if parts else np.array([], dtype=d)
                for i, d in enumerate((np.int64, float, float, np.int64, bool))]


def flow_bars(arrays, start, end, minutes):
    ts, prices, qty, _, maker = arrays
    bars = old.utc_bars(ts, prices, start, end, minutes)
    for b in bars:
        sl = slice(b["i"], b["j"])
        b.update(high=float(prices[sl].max()), low=float(prices[sl].min()),
            buy_volume=float(qty[sl][~maker[sl]].sum()), sell_volume=float(qty[sl][maker[sl]].sum()),
            buy_count=int((~maker[sl]).sum()), sell_count=int(maker[sl].sum()))
    return bars


def exhaustion_features(bars, index, edge):
    result = dict(exhaustion_observable=False, exhaustion_signal=False, exhaustion_status="missing_three_consecutive_bars")
    if index < 2:
        return result
    p = bars[index-2:index+1]
    width = p[-1]["end"]-p[-1]["start"]
    if not all(b["complete"] for b in p) or any(p[i]["start"]+width != p[i+1]["start"] for i in (0, 1)):
        return result
    outward = 1 if edge == "vah" else -1
    extremes = [b["high"] if edge == "vah" else b["low"] for b in p]
    volumes = [b["buy_volume"] if edge == "vah" else b["sell_volume"] for b in p]
    price_steps = [outward*(extremes[i+1]-extremes[i]) for i in (0, 1)]
    flag = all(step > 0 for step in price_steps) and volumes[0] > volumes[1] > volumes[2]
    return dict(exhaustion_observable=True, exhaustion_status="valid", exhaustion_signal=bool(flag),
        exhaustion_volume_0=volumes[0], exhaustion_volume_1=volumes[1], exhaustion_volume_2=volumes[2],
        exhaustion_extreme_0=extremes[0], exhaustion_extreme_1=extremes[1], exhaustion_extreme_2=extremes[2])


def absorption_features(arrays, bar, opening, edge, level, poc):
    ts, prices, qty, ids, maker = arrays
    result = dict(absorption_observable=False, absorption_calibratable=False, absorption_status="no_post_open_contact")
    i, j = max(bar["i"], int(np.searchsorted(ts, opening))), bar["j"]
    hit = old.first_cross(prices, level, i, j)
    if hit is None:
        return result
    distance = abs(level-poc)
    if distance <= 0:
        return dict(result, absorption_status="zero_edge_poc_distance")
    outward = 1 if edge == "vah" else -1
    indices = np.arange(hit, j)
    zone = np.abs(prices[hit:j]-level) <= PARAMS["zone_fraction"]*distance
    aggressive = ~maker[hit:j] if edge == "vah" else maker[hit:j]
    candidates = indices[zone & aggressive]
    observed_ms = int(ts[j-1]-ts[hit])
    volume = float(qty[candidates].sum())
    result.update(boundary_contact_ny=base.iso(ts[hit]), boundary_contact_id=int(ids[hit]),
        boundary_zone_halfwidth=PARAMS["zone_fraction"]*distance,
        outward_zone_volume=volume, outward_zone_prints=len(candidates),
        abs_pressure_rate=volume*60000/observed_ms if observed_ms > 0 else np.nan,
        observed_post_contact_ms=observed_ms)
    if not len(candidates):
        return dict(result, absorption_observable=True, absorption_status="observed_no_outward_zone_prints")
    required_ms = int(PARAMS["response_fraction"]*(bar["end"]-bar["start"]))
    eligible = candidates[(ts[j-1]-ts[candidates] >= required_ms)
        & (j-candidates-1 >= PARAMS["minimum_later_prints"])]
    if not len(eligible):
        return dict(result, absorption_status="insufficient_post_execution_observation")
    notionals = prices[eligible]*qty[eligible]
    big = int(eligible[int(notionals.argmax())])  # Size selection, never price-response selection.
    later = outward*(prices[big+1:j]-prices[big])
    progress = max(0., float(later.max()))/distance
    return dict(result, absorption_observable=True, absorption_calibratable=True, absorption_status="valid",
        abs_large_notional=float(prices[big]*qty[big]), abs_large_qty=float(qty[big]),
        abs_large_price=float(prices[big]), abs_large_id=int(ids[big]), abs_large_timestamp_ny=base.iso(ts[big]),
        abs_progress=progress, abs_response_last_timestamp_ny=base.iso(ts[j-1]),
        abs_response_prints=j-big-1, abs_response_observed_ms=int(ts[j-1]-ts[big]))


def canonical(row):
    r = {k:v for k,v in row.items() if pd.notna(v)}
    for old_name, new_name in (("second_bar_start_ny", "signal_bar_start_ny"),
            ("second_bar_end_ny", "signal_bar_end_ny"), ("second_bar_open", "signal_bar_open"),
            ("signal_body_price", "signal_body_close")):
        if old_name in r:
            r[new_name] = r.pop(old_name)
    return r


def daily(day, arrays, first_sources, second_sources, cfg):
    _, opening, end = base.bounds(day)
    ts, prices, qty, ids = arrays[:4]
    read_start = opening//3600000*3600000-2*3600000
    rows = []
    for minutes in TIMEFRAMES:
        tf = old.timeframe_name(minutes)
        source = first_sources[tf]
        bars = flow_bars(arrays, read_start, end, minutes) if len(ts) else []
        starts = {b["start"]:n for n,b in enumerate(bars)}
        r = {k:v for k,v in source.items() if k in half.COMMON+half.FIRST and pd.notna(v)}
        r.update(timeframe=tf, entry_mode="body_close", setup="first_push", push_type="first", profile_reused=bool(source["valid_session"]))
        if not source["valid_session"]:
            r["status"] = "source_"+source["status"]
        elif not source["opening_eligible"]:
            r["status"] = "open_not_inside_va"
        elif pd.isna(source.get("first_push_available_ny")):
            r["status"] = "no_first_body_push"
        else:
            first = half.first_from_cache(source, bars)
            b = bars[starts[first["start_ms"]]]
            candidate = dict(entry_index=int(np.searchsorted(ts, b["end"])), signal_ms=b["end"], signal_index=None,
                second_bar_start_ms=b["start"], second_bar_end_ms=b["end"], second_bar_open=b["open"], signal_body_price=b["close"])
            r = old.score(r, candidate, ts, prices, ids, source["poc"], first["side"], end, cfg)
        group = [canonical(r)]
        for mode in half.MODES:
            s = canonical(second_sources[(tf, mode)])
            s.update(setup="second_"+mode, push_type="second")
            if s["status"] in base.ACTIVE:
                for key, date_key, price_key in (("entry_agg_trade_id", "entry_timestamp_ny", "entry"),
                                                ("exit_agg_trade_id", "exit_timestamp_ny", "exit_price")):
                    at = int(np.searchsorted(ids, int(s[key])))
                    assert at < len(ids) and ids[at] == int(s[key]) and ts[at] == half.ms(s[date_key])
                    assert float(prices[at]) == s[price_key], "v19 raw fill changed"
            group.append(s)
        feature_cache = {}
        for row in group:
            row.update(absorption_observable=False, absorption_calibratable=False, absorption_status="no_candidate",
                exhaustion_observable=False, exhaustion_signal=False, exhaustion_status="no_candidate")
            if "signal_bar_start_ny" in row:
                start = half.ms(row["signal_bar_start_ny"])
                key = (start, row["touch_edge"])
                if key not in feature_cache:
                    index = starts[start]
                    b = bars[index]
                    assert b["complete"] and b["end"] == half.ms(row["signal_timestamp_ny"])
                    values = absorption_features(arrays, b, opening, row["touch_edge"], row[row["touch_edge"]], row["poc"])
                    values.update(exhaustion_features(bars, index, row["touch_edge"]))
                    values["feature_available_ms"] = b["end"]
                    feature_cache[key] = values
                row.update(feature_cache[key])
                if row["status"] in base.ACTIVE:
                    assert row["feature_available_ms"] <= half.ms(row["entry_timestamp_ny"])
            rows.append(row)
    return rows


def fit_absorption(events):
    p = events.loc[events.absorption_calibratable.eq(True)].drop_duplicates(EVENT_KEY)
    if len(p) < PARAMS["minimum_train_events"]:
        return dict(supported=False, train_distinct_events=len(p), pressure_cut=np.nan, size_cut=np.nan,
                    progress_cut=np.nan, progress_train_median=np.nan)
    pressure = float(p.abs_pressure_rate.quantile(PARAMS["pressure_quantile"]))
    size = float(p.abs_large_notional.quantile(PARAMS["size_quantile"]))
    progress = float(p.abs_progress.quantile(PARAMS["progress_quantile"]))
    return dict(supported=bool(pressure > 0 and size > 0 and np.isfinite([pressure,size,progress]).all()),
        train_distinct_events=len(p), pressure_cut=pressure, size_cut=size,
        progress_cut=min(progress, PARAMS["progress_cap"]), progress_train_median=progress)


def apply_absorption(frame, gate):
    if not gate["supported"]:
        return pd.Series(False, index=frame.index)
    return (frame.absorption_calibratable.eq(True) & frame.abs_pressure_rate.ge(gate["pressure_cut"])
        & frame.abs_large_notional.ge(gate["size_cut"]) & frame.abs_progress.le(gate["progress_cut"]))


def portions(p):
    return dict(base_all=p, absorption_matched=p.loc[p.absorption_observable.eq(True) & p.absorption_supported],
        absorption=p.loc[p.absorption_selected], exhaustion_matched=p.loc[p.exhaustion_observable.eq(True)],
        exhaustion=p.loc[p.exhaustion_selected])


def evaluate(ledger, calendar, cfg):
    cut_rows, fold_rows, tests = [], [], []
    for meta in calendar.to_dict("records"):
        asset = ledger.loc[ledger.symbol.eq(meta["symbol"])]
        cutoff = pd.Timestamp(meta["test_start"]).tz_localize(base.NY).tz_convert("UTC")
        train_events = asset.loc[asset.session_date.ge(meta["train_start"]) & asset.session_date.lt(meta["test_start"])
            & asset.feature_available_ms.lt(cutoff.value//1000000)]
        for minutes in TIMEFRAMES:
            tf = old.timeframe_name(minutes)
            gate = fit_absorption(train_events.loc[train_events.timeframe.eq(tf)])
            cut_rows.append(dict(**meta, timeframe=tf, **gate))
            for setup in SETUPS:
                parent = dict(symbol=meta["symbol"], timeframe=tf, setup=setup)
                g = asset.loc[asset.timeframe.eq(tf) & asset.setup.eq(setup)]
                for phase, start, finish in (("train", meta["train_start"], meta["test_start"]),
                                             ("test", meta["test_start"], meta["test_end_exclusive"])):
                    population = g.loc[g.valid_session & g.session_date.ge(start) & g.session_date.lt(finish)]
                    p = population.loc[population.status.isin(base.ACTIVE)].copy()
                    purged = 0
                    if phase == "train" and len(p):
                        known = pd.to_datetime(p.exit_timestamp_ny, utc=True, format="ISO8601").lt(cutoff)
                        purged = int((~known).sum())
                        p = p.loc[known].copy()
                    p["absorption_supported"] = gate["supported"]
                    p["absorption_selected"] = apply_absorption(p, gate)
                    p["exhaustion_selected"] = p.exhaustion_signal.eq(True)
                    p["fold"] = meta["fold"]
                    for name in ("pressure_cut", "size_cut", "progress_cut"):
                        p[name] = gate[name]
                    if phase == "test":
                        tests.append(p)
                    for policy, selected in portions(p).items():
                        fold_rows.append(dict(**parent, policy=policy, phase=phase, fold=meta["fold"],
                            train_start=meta["train_start"], test_start=meta["test_start"], test_end_exclusive=meta["test_end_exclusive"],
                            purged_train_outcomes=purged, absorption_supported=gate["supported"],
                            limited_sample=len(selected) < 20, **old.metrics(selected, population, cfg)))
    predictions = pd.concat(tests, ignore_index=True)
    assert not predictions.duplicated(MODEL+["session_date"]).any()
    folds = pd.DataFrame(fold_rows)
    summaries = []
    for key, g in ledger.groupby(MODEL):
        parent = dict(zip(MODEL, key))
        p = predictions
        fm = folds
        for name, value in parent.items():
            p = p.loc[p[name].eq(value)]
            fm = fm.loc[fm[name].eq(value)]
        owned = calendar.loc[calendar.symbol.eq(key[0])]
        mask = np.zeros(len(g), dtype=bool)
        for m in owned.to_dict("records"):
            mask |= (g.session_date.ge(m["test_start"]) & g.session_date.lt(m["test_end_exclusive"])).to_numpy()
        population = g.loc[mask & g.valid_session]
        groups = portions(p)
        for policy, selected in groups.items():
            f = fm.loc[fm.policy.eq(policy) & fm.phase.eq("test")]
            matched = groups["absorption_matched"] if policy.startswith("absorption") else groups["exhaustion_matched"] if policy.startswith("exhaustion") else p
            summaries.append(dict(**parent, policy=policy, sample="walk_forward_test",
                all_base_trades=len(p), matched_base_trades=len(matched),
                retained_pct_base=100*len(selected)/len(p) if len(p) else np.nan,
                retained_pct_matched=100*len(selected)/len(matched) if len(matched) else np.nan,
                total_test_folds=len(f), nonempty_test_folds=int(f.trades.gt(0).sum()),
                positive_net_test_folds=int(f.net_mean_r.gt(0).sum()),
                limited_evidence=bool(len(selected) < 20 or f.trades.gt(0).sum() < 5),
                **old.metrics(selected, population, cfg)))
    return pd.DataFrame(summaries), folds, predictions, pd.DataFrame(cut_rows)


def write_report(summary, cuts, output):
    lines = ["# Orderflow at VA first/second pushes — v20", "",
        "36 parent setups; absorption and exhaustion separately. UTC 15m/30m/1h, four exact pairs, final-body entry only. "
        "Second pushes retain both v19 half-pullback definitions. POC TP, SL 1:1, 4 bps fee EACH side. "
        "No new profile, ADX or sizing. 8-month train / 2-month test / step 2; old asset calendars.", "",
        "Absorption is a trade-tape PROXY: strong directional volume and a large execution near the edge, followed by little outward progress "
        "AFTER that execution. No book/iceberg/individual-order identification. Exhaustion: three consecutive new extremes with declining "
        "outward taker quantity. First/second models are separate accounts; counts across them are correlated.", "",
        "Each cell: test trades / NET WR / mean NET R. Matched baselines separate unavailable observations from signal rejection. "
        "Below 20 selected trades or five nonempty folds is limited evidence. See summary.csv and fold_metrics.csv.", ""]
    for symbol, g in summary.groupby("symbol"):
        lines += [f"## {symbol}", "", "| Candle | Setup | Full base | Abs matched | Absorption | Exh matched | Exhaustion |",
                  "|---|---|---|---|---|---|---|"]
        for minutes in TIMEFRAMES:
            for setup in SETUPS:
                p = g.loc[g.timeframe.eq(old.timeframe_name(minutes)) & g.setup.eq(setup)].set_index("policy")
                cells = []
                for policy in POLICIES:
                    r = p.loc[policy]
                    cells.append(f"{int(r.trades)} / {r.get('net_wr_pct', np.nan):.1f}% / {r.get('net_mean_r', np.nan):+.4f}" if r.trades else "0 / unavailable")
                lines.append(f"| {old.timeframe_name(minutes)} | {setup} | "+" | ".join(cells)+" |")
        lines.append("")
    lines += [f"Unsupported absorption calibrations: {int((~cuts.supported).sum())}/{len(cuts)} asset/timeframe/folds.", "",
        "Cutpoints use distinct already-completed train candidate events, shared across pushes; no profit optimization. "
        "Train windows overlap and cannot be pooled. Previously inspected history remains development data. "
        "Fees exclude additional spread/slippage/funding, order-book fills and intratrade equity. See DESIGN.md for exact definitions."]
    (output/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def checked_run(path):
    audit = json.loads((path/"audit.json").read_text())
    assert audit["status"] == "passed"
    for name, digest in audit["outputs"].items():
        assert base.sha(path/name) == digest, "Changed source artifact: "+name
    return json.loads((path/"config_snapshot.json").read_text())


def run(args):
    first_cfg, second_cfg = checked_run(args.first_source), checked_run(args.second_source)
    assert first_cfg["rule_version"] == "second_push_utc_v17_1" and second_cfg["rule_version"] == "half_pullback_body_close_v19_1"
    assert second_cfg["source_audit_sha256"] == base.sha(args.first_source/"audit.json")
    assert first_cfg["code_sha256"] == base.sha(old.__file__) and second_cfg["code_sha256"] == base.sha(half.__file__)
    for path, digest in {**first_cfg["helpers"], **second_cfg["execution_helpers"]}.items():
        assert base.sha(path) == digest, "Changed execution helper: "+path
    sys.path.insert(0, str(args.root.resolve()))
    from indicator.deep_trade import get_aggressive_side
    assert get_aggressive_side(True) == "sell" and get_aggressive_side(False) == "buy"
    cfg = second_cfg["config"]
    assert cfg["timeframes_minutes"] == list(TIMEFRAMES) and cfg["entry_modes"] == ["body_close"]
    assert set(cfg["assets"]) == {"BTCUSDC", "ETHUSDC", "XRPUSDT", "LTCUSDC"}
    source1 = pd.read_csv(args.first_source/"sessions.csv", float_precision="round_trip")
    source1 = source1.loc[source1.entry_mode.eq("body_close") & source1.timeframe.isin(["15m","30m","1h"])]
    source2 = pd.read_csv(args.second_source/"sessions.csv", float_precision="round_trip")
    expected = sorted(source1.session_date.unique())
    design = Path(__file__).with_name("PUSH_ORDERFLOW_DESIGN.md")
    snapshot = dict(rule_version="push_orderflow_v20_1", config=cfg, parameters=PARAMS,
        first_source=str(args.first_source.resolve()), second_source=str(args.second_source.resolve()),
        first_audit_sha256=base.sha(args.first_source/"audit.json"), second_audit_sha256=base.sha(args.second_source/"audit.json"),
        code_sha256=base.sha(__file__), design_sha256=base.sha(design), source_snapshot=first_cfg,
        role_helper_sha256=base.sha(args.root/"indicator/deep_trade.py"),
        execution_helper_sha256={str(Path(m.__file__).resolve()):base.sha(m.__file__) for m in (half,old,base)})
    output = args.output
    if args.resume:
        assert json.loads((output/"config_snapshot.json").read_text()) == snapshot, "Resume inputs changed"
    else:
        output.mkdir(parents=True, exist_ok=False)
        (output/"config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (output/"audit.json").write_text(json.dumps(dict(status="running")))
    frames, read_counts = [], {}
    for symbol, relative in cfg["assets"].items():
        path = args.root/relative
        original = first_cfg["sources"][symbol]
        before = path.stat()
        assert before.st_size == original["bytes"] and before.st_mtime_ns == original["mtime_ns"]
        first_days = {day:dict(zip(z.timeframe,z.to_dict("records"))) for day,z in source1.loc[source1.symbol.eq(symbol)].groupby("session_date")}
        second_days = {day:{(r["timeframe"],r["pullback_mode"]):r for r in z.to_dict("records")}
                       for day,z in source2.loc[source2.symbol.eq(symbol)].groupby("session_date")}
        assert sorted(first_days) == sorted(second_days) == expected
        checkpoint = output/f"{symbol}_daily.jsonl"
        saved = [json.loads(line) for line in checkpoint.read_text().splitlines() if line] if checkpoint.exists() else []
        assert [d["session_date"] for d in saved] == expected[:len(saved)]
        assert all(len(d["rows"]) == 9 and {(r["timeframe"],r["setup"]) for r in d["rows"]} ==
                   {(old.timeframe_name(m),s) for m in TIMEFRAMES for s in SETUPS} for d in saved)
        reader = FlowReader(path)
        with checkpoint.open("a", encoding="utf-8") as handle:
            for number in range(len(saved),len(expected)):
                day = expected[number]
                needs = any(r["valid_session"] and r["opening_eligible"] and pd.notna(r.get("first_push_available_ny")) for r in first_days[day].values())
                _, opening, end = base.bounds(pd.Timestamp(day).date())
                arrays = reader.window(opening//3600000*3600000-2*3600000,end) if needs else [np.array([],dtype=d) for d in (np.int64,float,float,np.int64,bool)]
                rows = daily(pd.Timestamp(day).date(), arrays, first_days[day], second_days[day], cfg)
                for row in rows:
                    row["symbol"] = symbol
                    for key,value in list(row.items()):
                        if isinstance(value,float) and not np.isfinite(value):
                            row.pop(key)
                item = dict(session_date=day, rows=rows)
                handle.write(json.dumps(item,allow_nan=False)+"\n")
                handle.flush()
                saved.append(item)
                if number % 150 == 0:
                    print(f"{symbol} {day}: {reader.groups_read} raw groups", flush=True)
        reader.close()
        after = path.stat()
        assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
        read_counts[symbol] = reader.groups_read
        frames.append(pd.DataFrame([r for d in saved for r in d["rows"]]))
        print(f"{symbol}: {len(saved)} periods, nine parent setups", flush=True)
    ledger = pd.concat(frames,ignore_index=True)
    assert len(ledger) == len(expected)*36 and not ledger.duplicated(MODEL+["session_date"]).any()
    calendar = pd.read_csv(args.first_source/"fold_metrics.csv")[["symbol","fold","train_start","test_start","test_end_exclusive"]].drop_duplicates()
    summary, folds, predictions, cuts = evaluate(ledger,calendar,cfg)
    ledger.to_parquet(output/"feature_ledger.parquet",index=False)
    predictions.to_parquet(output/"test_predictions.parquet",index=False)
    summary.to_csv(output/"summary.csv",index=False)
    folds.to_csv(output/"fold_metrics.csv",index=False)
    cuts.to_csv(output/"train_cutpoints.csv",index=False)
    ledger.groupby(MODEL+["status"],dropna=False).size().reset_index(name="periods").to_csv(output/"quality.csv",index=False)
    ledger.groupby(MODEL+["absorption_status","exhaustion_status"],dropna=False).size().reset_index(name="periods").to_csv(output/"feature_coverage.csv",index=False)
    (output/"DESIGN.md").write_bytes(design.read_bytes())
    write_report(summary,cuts,output)
    assert base.sha(args.first_source/"audit.json") == snapshot["first_audit_sha256"] and base.sha(args.second_source/"audit.json") == snapshot["second_audit_sha256"]
    audit = dict(status="passed",periods_per_asset=len(expected),parent_setups=36,gated_variants=72,rows=len(ledger),
        baseline_entries=int(ledger.status.isin(base.ACTIVE).sum()),profiles_built=0,raw_row_groups_read=read_counts,
        outputs={p.name:base.sha(p) for p in output.iterdir() if p.name != "audit.json"})
    (output/"audit.json").write_text(json.dumps(audit,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in audit.items() if k != "outputs"}),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[3])
    parser.add_argument("--first-source",type=Path,required=True)
    parser.add_argument("--second-source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--resume",action="store_true")
    run(parser.parse_args())
