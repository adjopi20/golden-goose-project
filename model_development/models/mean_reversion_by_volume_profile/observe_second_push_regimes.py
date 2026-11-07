"""v18: causal macro-state annotation of immutable v17 trades; no trade selection."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

import previous_value_area_reversal as base

FEATURES = {
    "adx14_4h": "4h", "er30_4h": "4h", "ema50_slope_abs_4h": "4h", "atr_pct_4h": "4h",
    "adx14_1d": "1d", "er20_1d": "1d", "ema50_slope_abs_1d": "1d", "atr_pct_1d": "1d",
    "rv20_1d": "1d",
}
PRIMARY = ("adx14_4h", "er30_4h", "rv20_1d")
FOCUS = {"BTCUSDC": ("2h", "body_close"), "ETHUSDC": ("4h", "intrabar"),
         "XRPUSDT": ("4h", "intrabar"), "LTCUSDC": ("2h", "body_close")}
MODEL = ["symbol", "timeframe", "entry_mode"]
KEY = MODEL + ["session_date"]
STRATA = MODEL + ["snapshot", "feature"]
CATEGORIES = ("low", "middle", "high", "missing_value", "unsupported_train")
WIDTH = {"4h": 4*3600000, "1d": 24*3600000}


def fingerprint(path):
    p = Path(path)
    s = p.stat()
    return dict(path=str(p.resolve()), bytes=s.st_size, mtime_ns=s.st_mtime_ns)


def aggregate_minutes(candles, rule, coverage):
    c = candles.sort_values("timestamp_ms")
    ts = c.timestamp_ms.to_numpy(dtype=np.int64)
    if len(ts) == 0 or (np.diff(ts) <= 0).any() or (ts % 60000).any():
        raise ValueError("Empty, duplicated, unordered or unaligned minute cache")
    values = c[["open", "high", "low", "close"]].to_numpy(float)
    if (not np.isfinite(values).all() or (values <= 0).any()
            or (c.high < c[["open", "close"]].max(axis=1)).any()
            or (c.low > c[["open", "close"]].min(axis=1)).any()):
        raise ValueError("Invalid cached OHLC")
    c = c.assign(start_ms=ts//WIDTH[rule]*WIDTH[rule])
    bars = c.groupby("start_ms", sort=True).agg(open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), observations=("timestamp_ms", "size"),
        first_observation_ms=("timestamp_ms", "first"), last_observation_ms=("timestamp_ms", "last"))
    bars["observation_unit"] = "minute_candles"
    return regular_bars(bars, rule, coverage)


def regular_bars(bars, rule, coverage):
    width = WIDTH[rule]
    index = np.arange(bars.index.min(), bars.index.max()+width, width, dtype=np.int64)
    bars = bars.reindex(index).rename_axis("start_ms")
    bars["available_ms"] = index+width
    bars["valid"] = (bars.observations.fillna(0).gt(0)
        & (index >= coverage[0]) & (index+width <= coverage[1]+1))
    bars["span_minutes"] = (bars.last_observation_ms-bars.first_observation_ms)/60000
    return bars


def raw_bars(path, coverage):
    """Aggregate both clocks in one pass; check ID continuity even across batches."""
    pieces = {r: [] for r in WIDTH}
    last_ts = last_id = None
    count = 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=500000,
            columns=["timestamp", "price", "agg_trade_id"]):
        c = batch.to_pandas()
        ts = c.timestamp.to_numpy(dtype=np.int64)
        ids = c.agg_trade_id.to_numpy(dtype=np.int64)
        prices = c.price.to_numpy(float)
        if (not len(c) or not np.isfinite(prices).all() or (prices <= 0).any()
                or (np.diff(ts) < 0).any() or (np.diff(ids) != 1).any()
                or (last_ts is not None and (ts[0] < last_ts or ids[0] != last_id+1))):
            raise ValueError("Invalid raw prices, order or aggregate-trade-ID continuity")
        last_ts, last_id = ts[-1], ids[-1]
        count += len(c)
        for rule, width in WIDTH.items():
            z = c.assign(start_ms=ts//width*width)
            pieces[rule].append(z.groupby("start_ms", sort=True).agg(open=("price", "first"),
                high=("price", "max"), low=("price", "min"), close=("price", "last"),
                observations=("timestamp", "size"), first_observation_ms=("timestamp", "first"),
                last_observation_ms=("timestamp", "last")))
    result = {}
    for rule, parts in pieces.items():
        bars = pd.concat(parts).groupby(level=0, sort=True).agg(dict(open="first", high="max",
            low="min", close="last", observations="sum", first_observation_ms="first",
            last_observation_ms="last"))
        bars["observation_unit"] = "aggregate_trades"
        result[rule] = regular_bars(bars, rule, coverage)
    return result, count


def feature_states(bars, rule, trend):
    out = bars.copy()
    names = [name for name, clock in FEATURES.items() if clock == rule]
    signed = "ema50_slope_signed_"+rule
    out[names+[signed]] = np.nan
    segment = (~out.valid).cumsum()
    for _, p in out.loc[out.valid].groupby(segment[out.valid]):
        c, h, l = p.close, p.high, p.low
        atr = trend.atr(h, l, c, 14).replace(0, np.nan)
        n, lag = (30, 6) if rule == "4h" else (20, 5)
        values = {
            "adx14_"+rule: trend.adx(h, l, c, 14),
            "er"+str(n)+"_"+rule: c.diff(n).abs()/c.diff().abs().rolling(n).sum().replace(0, np.nan),
            signed: trend.ema(c, 50).diff(lag)/atr,
            "atr_pct_"+rule: 100*atr/c,
        }
        values["ema50_slope_abs_"+rule] = values[signed].abs()
        if rule == "1d":
            values["rv20_1d"] = 100*np.log(c).diff().rolling(20).std(ddof=1)
        for name, v in values.items():
            warm = 200 if "ema50" in name else 100 if name.startswith(("adx", "atr")) else 0
            v = v.replace([np.inf, -np.inf], np.nan).copy()
            if warm:
                v.iloc[:warm-1] = np.nan
            out.loc[p.index, name] = v
    return out.reset_index()


def values_at(states, timestamps, rule):
    names = [name for name, clock in FEATURES.items() if clock == rule]
    names += ["ema50_slope_signed_"+rule]
    times = np.asarray(timestamps, dtype=np.int64)
    available = states.available_ms.to_numpy(dtype=np.int64)
    i = np.searchsorted(available, times, side="right")-1
    safe = np.maximum(i, 0)
    fresh = (i >= 0) & (times-available[safe] >= 0) & (times-available[safe] < WIDTH[rule])
    data = states[names].to_numpy(float)[safe].copy()
    data[~fresh] = np.nan
    result = pd.DataFrame(data, columns=names)
    result["macro_available_ms_"+rule] = np.where(fresh, available[safe], np.nan)
    return result


def train_cutpoints(states, feature, start_ms, end_ms):
    values = states.loc[states.available_ms.ge(start_ms) & states.available_ms.lt(end_ms), feature]
    values = values.loc[np.isfinite(values)]
    minimum = 60 if FEATURES[feature] == "4h" else 30
    q = values.quantile([1/3, 2/3]).to_numpy() if len(values) else [np.nan, np.nan]
    return dict(train_state_count=len(values), q_low=float(q[0]), q_high=float(q[1]),
                supported=bool(len(values) >= minimum and np.isfinite(q).all() and q[0] < q[1]))


def categories(values, cuts):
    x = np.asarray(values, dtype=float)
    labels = np.full(len(x), "missing_value", dtype=object)
    good = np.isfinite(x)
    if not cuts["supported"]:
        labels[good] = "unsupported_train"
    else:
        labels[good] = np.where(x[good] <= cuts["q_low"], "low",
            np.where(x[good] <= cuts["q_high"], "middle", "high"))
    return labels


def midnight_ms(day):
    return int(pd.Timestamp(day).tz_localize(base.NY).tz_convert("UTC").value//1000000)


def utc_ms(values):
    # pandas can infer microseconds rather than nanoseconds from ISO timestamps.
    return pd.to_datetime(values, utc=True, format="ISO8601").dt.as_unit("ms").astype("int64")


def fold_stats(part):
    row = dict(trades=len(part))
    for label in ("gross", "net"):
        r = part[label+"_r"].to_numpy(float)
        row.update({label+"_mean_r": float(r.mean()) if len(r) else np.nan,
                    label+"_total_r": float(r.sum()),
                    label+"_wr_pct": float(100*(r > 0).mean()) if len(r) else np.nan})
    return row


def label_folds(ledger, states, source_folds):
    cuts_rows, records, predictions = [], [], []
    asset_folds = source_folds[["symbol", "fold", "train_start", "test_start", "test_end_exclusive"]].drop_duplicates()
    for meta in asset_folds.to_dict("records"):
        symbol, fold = meta["symbol"], int(meta["fold"])
        asset = ledger.loc[ledger.symbol.eq(symbol)]
        groups = list(asset.groupby(["timeframe", "entry_mode", "snapshot"], sort=True).groups)
        train = asset.loc[asset.session_date.ge(meta["train_start"]) & asset.session_date.lt(meta["test_start"])
            & asset.exit_ms.lt(midnight_ms(meta["test_start"]))]
        test = asset.loc[asset.session_date.ge(meta["test_start"]) & asset.session_date.lt(meta["test_end_exclusive"])]
        for feature, rule in FEATURES.items():
            cuts = train_cutpoints(states[symbol][rule], feature, midnight_ms(meta["train_start"]), midnight_ms(meta["test_start"]))
            cuts_rows.append(dict(**meta, feature=feature, **cuts))
            for phase, sample in (("train", train), ("test", test)):
                tagged = sample.copy()
                tagged["feature"] = feature
                tagged["value"] = tagged[feature]
                tagged["category"] = categories(tagged[feature], cuts)
                tagged["fold"] = fold
                if phase == "test":
                    predictions.append(tagged[KEY+["snapshot", "feature", "fold", "category", "value",
                        "gross_r", "net_r", "risk_price", "entry", "status", "target_distance_pct", "hold_minutes"]])
                parts = dict(tuple(tagged.groupby(["timeframe", "entry_mode", "snapshot"], sort=True)))
                for group in groups:
                    p = parts.get(group, tagged.iloc[:0])
                    parent = dict(**meta, timeframe=group[0], entry_mode=group[1], snapshot=group[2], feature=feature, phase=phase)
                    matched = p.loc[p.category.isin(("low", "middle", "high"))]
                    for category, z in [("all_base", p), ("matched_base", matched)]+list(p.groupby("category", sort=True)):
                        records.append(dict(**parent, category=category, **fold_stats(z)))
    labels = pd.concat(predictions, ignore_index=True)
    assert not labels.duplicated(KEY+["snapshot", "feature"]).any(), "Overlapping test folds"
    return pd.DataFrame(cuts_rows), pd.DataFrame(records), labels


def summarize(labels, fold_metrics, source_summary, cfg):
    records = []
    tests = fold_metrics.loc[fold_metrics.phase.eq("test")]
    training = fold_metrics.loc[fold_metrics.phase.eq("train")]
    for key, p in labels.groupby(STRATA, sort=True):
        parent = dict(zip(STRATA, key))
        source = source_summary.loc[(source_summary[MODEL] == pd.Series(dict(zip(MODEL, key[:3])))).all(axis=1)
                                    & source_summary["sample"].eq("walk_forward_test")].iloc[0]
        valid_n = int(source.valid_sessions)
        matched = p.loc[p.category.isin(("low", "middle", "high"))]
        folds = tests
        tr = training
        for name, value in parent.items():
            folds = folds.loc[folds[name].eq(value)]
            tr = tr.loc[tr[name].eq(value)]
        base_folds = folds.loc[folds.category.eq("matched_base")].set_index("fold")
        for category in ("all_base", "matched_base")+CATEGORIES:
            z = p if category == "all_base" else matched if category == "matched_base" else p.loc[p.category.eq(category)]
            fm = folds.loc[folds.category.eq(category)].set_index("fold")
            tm = tr.loc[tr.category.eq(category)].set_index("fold")
            active = fm.loc[fm.trades.gt(0)]
            both = active.join(tm[["trades", "net_mean_r"]].rename(columns={"trades": "train_trades", "net_mean_r": "train_mean_r"}))
            comparable = both.loc[both.train_trades.gt(0)]
            lift = active.net_mean_r-base_folds.net_mean_r
            metrics = base.stats(z, valid_n, cfg)
            row = dict(**parent, category=category, **metrics,
                all_base_trades=len(p), matched_base_trades=len(matched),
                coverage_pct=100*len(matched)/len(p),
                share_of_matched_pct=100*len(z)/len(matched) if len(matched) and category in ("low", "middle", "high") else np.nan,
                matched_base_net_mean_r=matched.net_r.mean(), matched_base_gross_mean_r=matched.gross_r.mean(),
                total_folds=int(folds.fold.nunique()), nonempty_test_folds=len(active),
                positive_net_test_folds=int(active.net_mean_r.gt(0).sum()),
                better_than_matched_test_folds=int(lift.gt(0).sum()),
                comparable_train_test_folds=len(comparable),
                positive_train_and_test_folds=int((comparable.train_mean_r.gt(0) & comparable.net_mean_r.gt(0)).sum()),
                train_test_sign_agreement_folds=int((np.sign(comparable.train_mean_r) == np.sign(comparable.net_mean_r)).sum()),
                limited_evidence=bool(len(z) < 20 or len(active) < 5),
                mean_fee_r=float((z.gross_r-z.net_r).mean()) if len(z) else np.nan)
            records.append(row)
    return pd.DataFrame(records)


def write_report(summary, cuts, quality, output):
    lines = ["# Second-push macro regime observation — v18", "",
        "Retrospective observation of unchanged v17 trades. Categories are train-state terciles; no category is selected as a filter. "
        "Train 8 months / test 2 / step 2, original asset calendars. Fees 4 bps EACH side; no added funding or slippage.", "",
        "Primary readout below uses four parent variants fixed before this analysis. All 40 variants, nine features, and both snapshots "
        "are in regime_summary.csv. NY-open and entry snapshots are alternatives; these results are not a combined rule.", "",
        "Each category cell is: trades / net mean R per trade / positive-net test folds. Fold count includes empty category folds "
        "in its denominator. Low ADX or ER is an indicator state, not a verified sideways label. "
        "Any cell with <20 trades or <5 nonempty folds is limited evidence.", ""]
    for symbol, (tf, mode) in FOCUS.items():
        s = summary.loc[summary.symbol.eq(symbol) & summary.timeframe.eq(tf) & summary.entry_mode.eq(mode)]
        b = s.loc[s.category.eq("all_base")].iloc[0]
        lines += [f"## {symbol} — {tf} {mode}", "",
            f"Unchanged full-test base: {int(b.trades)} trades; gross {b.gross_mean_r:+.4f}R/trade, "
            f"net {b.net_mean_r:+.4f}R/trade; net total {b.net_total_r:+.2f}R.", "",
            "| Snapshot | Feature | Matched base n / net R | Low | Middle | High |",
            "|---|---|---|---|---|---|"]
        for snapshot in ("ny_open", "pre_entry"):
            for feature in PRIMARY:
                q = s.loc[s.snapshot.eq(snapshot) & s.feature.eq(feature)].set_index("category")
                base_row = q.loc["matched_base"]
                cells = []
                for category in ("low", "middle", "high"):
                    r = q.loc[category]
                    cells.append(f"{int(r.trades)} / {r.net_mean_r:+.4f} / {int(r.positive_net_test_folds)}/{int(r.total_folds)}"
                                 if r.trades else "0 / unavailable")
                lines.append(f"| {snapshot} | {feature} | {int(base_row.trades)} / {base_row.net_mean_r:+.4f} | "+" | ".join(cells)+" |")
        lines.append("")
    lines += ["## Coverage and interpretation", "",
        f"Unsupported feature/folds: {int((~cuts.supported).sum())} of {len(cuts)}. See train_cutpoints.csv for every threshold and count.",
        "Complete macro bars with no observations reset warmup. Missing minutes are not interpolated. Cached archive provenance "
        "has not been independently exchange-verified; macro quality diagnostics are in source_quality.csv.", "",
        "A favorable conditional mean is exploratory: many correlated variants and categories have been inspected. "
        "Positive-fold counts, train/test agreement, trade counts and fee-adjusted results must be read together. "
        "Pooled training results are deliberately omitted because training windows overlap. "
        "No present finding establishes a new untouched out-of-sample edge or justifies increasing position size.", "",
        "## Files", "", "- regime_summary.csv: all category and matched/unfiltered base performance.",
        "- fold_metrics.csv: separate train/test readouts; do not pool train folds.",
        "- test_labels.parquet: every test trade's individual-feature labels and original outcomes.",
        "- feature_ledger.parquet: both snapshots for all original active trades.",
        "- macro_states.parquet: UTC completed bars and all indicator states.",
        "- train_cutpoints.csv, source_quality.csv, audit.json, config_snapshot.json and DESIGN.md: provenance and checks."]
    (output/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def run(root, source, output, candle_config):
    audit = json.loads((source/"audit.json").read_text())
    assert audit["status"] == "passed"
    for name, digest in audit["outputs"].items():
        assert base.sha(source/name) == digest, "Changed v17 output: "+name
    snapshot = json.loads((source/"config_snapshot.json").read_text())
    cfg = snapshot["config"]
    source_data = pd.read_csv(source/"sessions.csv")
    trades = source_data.loc[source_data.status.isin(base.ACTIVE)].copy().sort_values(KEY).reset_index(drop=True)
    assert not trades.duplicated(KEY).any()
    source_folds = pd.read_csv(source/"fold_metrics.csv")
    source_summary = pd.read_csv(source/"summary.csv")
    sys.path.insert(0, str(root.resolve()))
    from indicator import trend
    output.mkdir(parents=True, exist_ok=False)
    (output/"audit.json").write_text(json.dumps(dict(status="running")))
    design = Path(__file__).with_name("SECOND_PUSH_REGIMES_DESIGN.md")
    (output/"DESIGN.md").write_bytes(design.read_bytes())
    config = json.loads(candle_config.read_text(encoding="utf-8-sig"))
    states, ledgers, quality, fingerprints = {}, [], [], []
    for symbol, entries in config.items():
        print("Building macro states:", symbol, flush=True)
        raw = root/cfg["assets"][symbol]
        old = snapshot["sources"][symbol]
        actual = fingerprint(raw)
        assert all(actual[k] == old[k] for k in ("bytes", "mtime_ns")), "Raw source changed: "+symbol
        coverage = (old["min_timestamp_ms"], old["max_timestamp_ms"])
        fingerprints.append(actual)
        if entries:
            frames = []
            for entry in entries:
                path = root/entry["path"]
                fingerprints.append(fingerprint(path))
                c = pd.read_parquet(path, columns=["timestamp_ms", "open", "high", "low", "close"])
                if "from" in entry:
                    c = c.loc[c.timestamp_ms.ge(pd.Timestamp(entry["from"]).value//1000000)]
                if "before" in entry:
                    c = c.loc[c.timestamp_ms.lt(pd.Timestamp(entry["before"]).value//1000000)]
                frames.append(c)
            c = pd.concat(frames, ignore_index=True).sort_values("timestamp_ms")
            cache_coverage = (max(coverage[0], int(c.timestamp_ms.min())),
                              min(coverage[1], int(c.timestamp_ms.max())+59999))
            bars = {r: aggregate_minutes(c, r, cache_coverage) for r in WIDTH}
            source_rows = len(c)
        else:
            bars, source_rows = raw_bars(raw, coverage)
            assert source_rows == old["rows"]
        states[symbol] = {r: feature_states(bars[r], r, trend) for r in WIDTH}
        for rule, z in states[symbol].items():
            quality.append(dict(symbol=symbol, clock=rule, source_kind="minute_cache" if entries else "raw_trade_archive",
                source_rows=source_rows, macro_bars=len(z), valid_bars=int(z.valid.sum()),
                empty_bars=int(z.observations.fillna(0).eq(0).sum()),
                partial_boundary_bars=int((~z.valid & z.observations.gt(0)).sum()),
                median_observations=float(z.loc[z.valid, "observations"].median()),
                first_available_utc=str(pd.to_datetime(z.available_ms.min(), unit="ms", utc=True)),
                last_available_utc=str(pd.to_datetime(z.available_ms.max(), unit="ms", utc=True))))
        t = trades.loc[trades.symbol.eq(symbol)].copy()
        t["exit_ms"] = utc_ms(t.exit_timestamp_ny)
        assert t.exit_ms.between(*coverage).all(), "Exit timestamp unit / coverage mismatch"
        for name, col in (("ny_open", "profile_end_ny"), ("pre_entry", "entry_timestamp_ny")):
            part = t.reset_index(drop=True).copy()
            part["snapshot"] = name
            part["snapshot_ms"] = utc_ms(part[col])
            assert part.snapshot_ms.between(*coverage).all(), "Snapshot timestamp unit / coverage mismatch"
            for rule in WIDTH:
                part = pd.concat([part, values_at(states[symbol][rule], part.snapshot_ms, rule)], axis=1)
                available = part["macro_available_ms_"+rule]
                assert (available.dropna() <= part.loc[available.notna(), "snapshot_ms"]).all()
            ledgers.append(part)
    ledger = pd.concat(ledgers, ignore_index=True)
    cuts, folds, labels = label_folds(ledger, states, source_folds)
    print("Summarizing", len(labels), "test annotations", flush=True)
    summary = summarize(labels, folds, source_summary, cfg)
    # Every category partition must exactly reproduce the unmodified parent.
    for key, p in summary.groupby(STRATA):
        b = p.loc[p.category.eq("all_base")].iloc[0]
        cats = p.loc[p.category.isin(CATEGORIES)]
        assert int(cats.trades.sum()) == int(b.trades), key
        for col in ("gross_total_r", "net_total_r"):
            assert np.isclose(cats[col].sum(), b[col], atol=1e-9), (key, col)
        original = source_summary.loc[(source_summary[MODEL] == pd.Series(dict(zip(MODEL, key[:3])))).all(axis=1)
                                      & source_summary["sample"].eq("walk_forward_test")].iloc[0]
        for col in ("trades", "gross_mean_r", "net_mean_r", "gross_total_r", "net_total_r", "net_account_return_pct"):
            assert np.isclose(b[col], original[col], atol=1e-9), (key, col)
    for fp in fingerprints:
        assert fingerprint(fp["path"]) == fp, "Input changed while running"
    ledger.to_parquet(output/"feature_ledger.parquet", index=False)
    labels.to_parquet(output/"test_labels.parquet", index=False)
    pd.concat([z.assign(symbol=s, clock=r) for s, clocks in states.items() for r, z in clocks.items()], ignore_index=True).to_parquet(output/"macro_states.parquet", index=False)
    summary.to_csv(output/"regime_summary.csv", index=False)
    folds.to_csv(output/"fold_metrics.csv", index=False)
    cuts.to_csv(output/"train_cutpoints.csv", index=False)
    pd.DataFrame(quality).to_csv(output/"source_quality.csv", index=False)
    state_config = dict(version="second_push_macro_regimes_v18_1", source_run=str(source.resolve()),
        source_audit_sha256=base.sha(source/"audit.json"), script_sha256=base.sha(Path(__file__)),
        indicator_sha256=base.sha(root/"indicator/trend.py"), features=FEATURES,
        candle_config=config, sources=fingerprints, original_trade_config=cfg,
        primary_features=PRIMARY, focus_models=FOCUS)
    (output/"config_snapshot.json").write_text(json.dumps(state_config, indent=2), encoding="utf-8")
    write_report(summary, cuts, quality, output)
    passed = dict(status="passed", source_rows=len(source_data), source_trades=len(trades),
        snapshot_rows=len(ledger), test_annotation_rows=len(labels), combinations=len(trades[MODEL].drop_duplicates()),
        features=len(FEATURES), snapshots=2, source_ledger_unchanged=True,
        categories_reconcile_to_base=True, original_base_metrics_reconciled=True,
        no_future_macro_candles=True, no_test_cutpoint_calibration=True,
        profiles_built=0, outputs={p.name: base.sha(p) for p in output.iterdir() if p.name != "audit.json"})
    (output/"audit.json").write_text(json.dumps(passed, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in passed.items() if k != "outputs"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candle-config", type=Path, default=Path(__file__).with_name("second_push_regimes_config.json"))
    args = parser.parse_args()
    run(args.root, args.source, args.output, args.candle_config)
