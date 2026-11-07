"""Causal context annotations and retrospective 8+2-month comparisons of old trades."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
KEYS = ["symbol", "profile", "session_date"]
ACTIVE = ["tp_first", "sl_first", "time_exit"]
COLS = ["timestamp_ms", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume"]
FEATURES = {
    "delta_result": ("delta_signed", "progress_atr"),
    "volume_result": ("relative_volume", "progress_atr"),
    "participation_change": ("volume_last30_over_previous30",),
    "poc_migration_pre_ny": ("poc_migration_pre_ny_atr",),
    "poc_migration_previous": ("poc_migration_previous_atr",),
    "target_geometry": ("distance_poc_atr",),
}
for _tf in ["4h", "1d"]:
    for _feature in ["adx", "adx_change", "ema_gap_signed", "ema_slope_signed", "er", "alignment"]:
        FEATURES[f"macro_{_tf}_{_feature}"] = (f"macro_{_tf}_{_feature}",)
for _ref in ["pre_ny", "previous"]:
    for _feature in ["outside_fraction", "failed_attempts"]:
        FEATURES[f"value_{_ref}_{_feature}"] = (f"value_{_ref}_{_feature}",)


def fingerprint(path):
    p = Path(path)
    st = p.stat()
    return dict(path=str(p.resolve()), bytes=st.st_size, mtime_ns=st.st_mtime_ns)


def minute_grid(c):
    """Carry only earlier closes for <=5 missing minutes; never repair existing bad rows."""
    c = c.sort_values("timestamp_ms").copy()
    if c.timestamp_ms.duplicated().any() or (c.timestamp_ms % 60000).ne(0).any():
        raise ValueError("Duplicate or unaligned candle opens")
    if not np.isfinite(c[COLS].to_numpy()).all():
        raise ValueError("Nonfinite source candles")
    if (c[["open", "high", "low", "close"]].le(0).any().any()
            or c[["volume", "buy_volume", "sell_volume"]].lt(0).any().any()
            or c.high.lt(c[["open", "low", "close"]].max(axis=1)).any()
            or c.low.gt(c[["open", "high", "close"]].min(axis=1)).any()):
        raise ValueError("Invalid source OHLCV")
    if not np.allclose(c.volume, c.buy_volume + c.sell_volume, rtol=1e-7, atol=1e-7):
        raise ValueError("Buy/sell volume does not reconcile")
    c = c.set_index("timestamp_ms")
    c = c.reindex(np.arange(c.index.min(), c.index.max() + 60000, 60000))
    missing = c.volume.isna()
    earlier = c.close.ffill(limit=5)
    for col in ["open", "high", "low", "close"]:
        c.loc[missing, col] = earlier.loc[missing]
    c.loc[missing, ["volume", "buy_volume", "sell_volume"]] = 0.
    c["imputed"] = missing
    c["valid"] = c[["open", "high", "low", "close"]].notna().all(axis=1)
    c.index = pd.to_datetime(c.index, unit="ms", utc=True)
    return c


def completed_states(c, rule, root):
    """Reset history after an invalid bar and index every state by availability time."""
    from indicator.trend import adx, atr, ema
    minutes = {"1h": 60, "4h": 240, "1d": 1440}[rule]
    r = c.resample(f"{minutes}min", origin="epoch", closed="left", label="left")
    b = r.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
              close=("close", "last"), valid_count=("valid", "sum"))
    b["valid"] = b.valid_count.eq(minutes)
    b["segment"] = (~b.valid).cumsum()
    records = []
    for _, part in b.loc[b.valid].groupby("segment"):
        p = part.copy()
        p["atr"] = atr(p.high, p.low, p.close)
        p.loc[np.arange(len(p)) < 13, "atr"] = np.nan
        if rule != "1h":
            lag, er_n = (6, 30) if rule == "4h" else (5, 20)
            e50, e200 = ema(p.close, 50), ema(p.close, 200)
            slope = e50.diff(lag) / p.atr
            strength = adx(p.high, p.low, p.close)
            p["adx"] = strength
            p["adx_change"] = strength.diff(lag)
            p["ema_gap"] = (e50 - e200) / p.atr
            p["ema_slope"] = slope
            p["er"] = (p.close.diff(er_n).abs()
                       / p.close.diff().abs().rolling(er_n).sum().replace(0, np.nan))
            p["direction"] = np.select(
                [(p.close > e50) & (e50 > e200) & (slope > 0),
                 (p.close < e50) & (e50 < e200) & (slope < 0)], [1., -1.], default=0.)
            p.loc[np.arange(len(p)) < 199,
                  ["adx", "adx_change", "ema_gap", "ema_slope", "er", "direction"]] = np.nan
        records.append(p)
    if not records:
        return pd.DataFrame()
    out = pd.concat(records).sort_index()
    out.index += pd.Timedelta(minutes=minutes)
    return out


def state_before(states, pivot, rule):
    i = states.index.searchsorted(pivot, side="right") - 1
    duration = pd.Timedelta(minutes={"1h": 60, "4h": 240, "1d": 1440}[rule])
    if i < 0 or pivot - states.index[i] >= duration:
        return None
    return states.iloc[i]


def slice_minutes(c, start, end):
    part = c.iloc[c.index.searchsorted(start):c.index.searchsorted(end)]
    if len(part) != int((end - start).total_seconds() / 60) or not part.valid.all():
        return None
    return part


def acceptance(closes, val, vah, direction):
    outside = closes > vah if direction > 0 else closes < val
    return float(outside.mean()), int((outside[:-1] & ~outside[1:]).sum())


def build_features(args, observations):
    from observe_four_profiles import clocks
    cfg = json.loads(args.candles_config.read_text(encoding="utf-8-sig"))
    va = pd.read_csv(args.value_areas) if args.value_areas else pd.DataFrame()
    if len(va) and va.duplicated(KEYS).any():
        raise ValueError("Duplicate value-area keys")
    rows, sources = [], []
    for symbol, sessions in observations.groupby("symbol", sort=False):
        paths = [args.root / p for p in cfg[symbol]]
        sources.extend(fingerprint(p) for p in paths)
        c = minute_grid(pd.concat([pd.read_parquet(p, columns=COLS) for p in paths], ignore_index=True))
        states = {rule: completed_states(c, rule, args.root) for rule in ["1h", "4h", "1d"]}
        # Relative volume is referenced to earlier NY dates at exactly the same local hour.
        local = c.index.tz_convert(NY)
        mask = (local.hour == 12)
        hourly = c.loc[mask].assign(ny_day=local[mask].strftime("%Y-%m-%d"))
        history = hourly.groupby("ny_day").agg(volume=("volume", "sum"), n=("valid", "sum"))
        history = history.loc[history.n.eq(60)]
        history["reference"] = history.volume.shift().rolling(20, min_periods=10).median()
        for day, profiles in sessions.groupby("session_date", sort=True):
            _, _, opening_ms, pivot_ms, _ = clocks(date.fromisoformat(day))
            pivot, opening = pd.to_datetime([pivot_ms, opening_ms], unit="ms", utc=True)
            morning = slice_minutes(c, opening, pivot)
            hour = slice_minutes(c, pivot - pd.Timedelta(hours=1), pivot)
            row = dict(symbol=symbol, session_date=day, feature_status="valid")
            if morning is None or hour is None:
                rows.append(dict(row, feature_status="incomplete_pre1300_minutes"))
                continue
            ref = profiles.iloc[0]
            if not (np.isclose(morning.open.iloc[0], ref.price_0930, rtol=1e-10, atol=1e-8)
                    and np.isclose(morning.close.iloc[-1], ref.price_before_1300, rtol=1e-10, atol=1e-8)):
                rows.append(dict(row, feature_status="cache_price_mismatch"))
                continue
            direction = float(np.sign(morning.close.iloc[-1] - morning.open.iloc[0]))
            a = state_before(states["1h"], pivot, "1h")
            normalizer = float(a.atr) if a is not None and a.atr > 0 else np.nan
            volume = hour.volume.sum()
            row.update(morning_direction=direction, atr_1h=normalizer,
                       imputed_morning_minutes=int(morning.imputed.sum()),
                       delta_signed=direction * (hour.buy_volume.sum() - hour.sell_volume.sum()) / volume if volume else np.nan,
                       progress_atr=direction * (hour.close.iloc[-1] - hour.open.iloc[0]) / normalizer,
                       relative_volume=volume / history.loc[day, "reference"] if day in history.index and history.loc[day, "reference"] > 0 else np.nan,
                       volume_last30_over_previous30=hour.volume.iloc[30:].sum() / hour.volume.iloc[:30].sum() if hour.volume.iloc[:30].sum() else np.nan,
                       feature_asof_utc=pivot.isoformat())
            pocs = profiles.set_index("profile").poc
            for label, name in [("pre_ny", "pre_ny_0130_0930"), ("previous", "previous_day_0930_0930")]:
                row[f"poc_migration_{label}_atr"] = (direction * (pocs.get("ny_0930_1300", np.nan) - pocs.get(name, np.nan)) / normalizer)
                if len(va):
                    v = va.loc[va.symbol.eq(symbol) & va.session_date.eq(day) & va.profile.eq(name)]
                    if len(v) == 1 and v.iloc[0].status == "valid":
                        known = v.iloc[0]
                        if pd.Timestamp(known.available_utc) > opening:
                            raise ValueError("Value-area reference unavailable at NY open")
                        if not np.isclose(known.poc, pocs.get(name, np.nan), rtol=1e-10, atol=1e-8):
                            raise ValueError("Value area POC mismatch")
                        closes = morning.close.iloc[4::5].to_numpy()
                        fraction, failures = acceptance(closes, known.val, known.vah, direction)
                        row[f"value_{label}_outside_fraction"] = fraction
                        row[f"value_{label}_failed_attempts"] = failures
            for rule in ["4h", "1d"]:
                s = state_before(states[rule], pivot, rule)
                row[f"macro_{rule}_available"] = s is not None and pd.notna(s.get("direction"))
                if not row[f"macro_{rule}_available"]:
                    continue
                for f in ["adx", "adx_change", "er"]:
                    row[f"macro_{rule}_{f}"] = float(s[f])
                row[f"macro_{rule}_ema_gap_signed"] = direction * float(s.ema_gap)
                row[f"macro_{rule}_ema_slope_signed"] = direction * float(s.ema_slope)
                aligned = direction * float(s.direction)
                row[f"macro_{rule}_alignment"] = "with_morning" if aligned > 0 else "against_morning" if aligned < 0 else "mixed"
                row[f"macro_{rule}_asof_utc"] = states[rule].index[states[rule].index.searchsorted(pivot, side="right") - 1].isoformat()
            rows.append(row)
        print(f"{symbol}: cached context completed", flush=True)
        del c, states
    return pd.DataFrame(rows), sources


def calibrate(train, feature):
    columns = FEATURES[feature]
    if feature.endswith("alignment"):
        return {"kind": "categorical"}
    if len(columns) == 2:
        return {"kind": "pair", "cutpoints": [float(train[c].median()) for c in columns]}
    return {"kind": "numeric", "cutpoints": train[columns[0]].quantile([1 / 3, 2 / 3]).tolist()}


def classify(frame, feature, calibration):
    cols = FEATURES[feature]
    if calibration["kind"] == "categorical":
        return frame[cols[0]].astype(str)
    if calibration["kind"] == "pair":
        effort = frame[cols[0]].to_numpy() > calibration["cutpoints"][0]
        result = frame[cols[1]].to_numpy() > calibration["cutpoints"][1]
        labels = np.where(effort, "high_effort", "low_effort")
        return pd.Series([e + ("_strong_result" if r else "_weak_result") for e, r in zip(labels, result)], index=frame.index)
    low, high = calibration["cutpoints"]
    return pd.Series(np.select([frame[cols[0]].le(low), frame[cols[0]].le(high)], ["low", "middle"], default="high"), index=frame.index)


def fold_schedule(first, last):
    start = pd.Timestamp(first)
    if start.day != 1:
        start = start.to_period("M").to_timestamp() + pd.DateOffset(months=1)
    last_end = pd.Timestamp(last) + pd.Timedelta(days=1)
    while start + pd.DateOffset(months=10) <= last_end:
        boundary = start + pd.DateOffset(months=8)
        yield start, boundary, boundary + pd.DateOffset(months=2)
        start += pd.DateOffset(months=2)


def measures(frame):
    from performance_poc_rr import stats
    m = stats(frame.gross_r)
    tp, sl = frame.status.eq("tp_first").sum(), frame.status.eq("sl_first").sum()
    m.update(tp_first_pct=100 * tp / len(frame) if len(frame) else np.nan,
             tp_resolved_pct=100 * tp / (tp + sl) if tp + sl else np.nan)
    if "returned_to_poc" in frame:
        moved = frame.loc[frame.moved_away.eq(True) & frame.returned_to_poc.notna()]
        m.update(poc_touch_eligible=len(moved), poc_touch_pct=100 * moved.returned_to_poc.eq(True).mean() if len(moved) else np.nan)
    return m


def random_control(base, selected, calendar_start, calendar_end, rng, repeats=500):
    if selected.empty:
        return {}
    values = base.gross_r.to_numpy()
    # Independent uniform subsets without replacement, vectorized across repetitions.
    shuffled = rng.random((repeats, len(values))).argsort(axis=1)
    draws = values[shuffled[:, :len(selected)]].mean(axis=1)
    dates = pd.date_range(calendar_start, calendar_end - pd.Timedelta(days=1), freq="D")
    original = base.set_index(pd.to_datetime(base.session_date)).gross_r.reindex(dates, fill_value=0).to_numpy()
    kept = selected.set_index(pd.to_datetime(selected.session_date)).gross_r.reindex(dates, fill_value=0).to_numpy()
    counts = base.set_index(pd.to_datetime(base.session_date)).gross_r.reindex(dates).notna().to_numpy().astype(int)
    contribution, n = kept - original, len(dates)
    blocks = int(np.ceil(n / 7))
    starts = rng.integers(n, size=(repeats, blocks, 1))
    indices = ((starts + np.arange(7)) % n).reshape(repeats, -1)[:, :n]
    denominator = counts[indices].sum(axis=1)
    estimates = contribution[indices].sum(axis=1)[denominator > 0] / denominator[denominator > 0]
    return dict(random_mean_r_p05=np.quantile(draws, .05), random_mean_r_p95=np.quantile(draws, .95),
                random_tail_fraction=(1 + (draws >= selected.gross_r.mean()).sum()) / (repeats + 1),
                opportunity_delta_r_ci05=np.quantile(estimates, .05), opportunity_delta_r_ci95=np.quantile(estimates, .95))


def analyze(ledger, out, minimum_train=30):
    rng = np.random.default_rng(20261011)
    folds, categories, chosen_rows, predictions, calibrations = [], [], [], [], []
    for (symbol, profile), original in ledger.groupby(["symbol", "profile"], sort=True):
        valid = original.loc[original.profile_status.eq("valid")].copy()
        if valid.empty:
            continue
        for number, (start, boundary, end) in enumerate(fold_schedule(valid.session_date.min(), valid.session_date.max()), 1):
            train_dates = valid.session_date.ge(str(start.date())) & valid.session_date.lt(str(boundary.date()))
            test_dates = valid.session_date.ge(str(boundary.date())) & valid.session_date.lt(str(end.date()))
            cutoff = pd.Timestamp(boundary.date()).tz_localize(NY).tz_convert("UTC")
            available = pd.to_datetime(valid.exit_timestamp_ny, utc=True, format="ISO8601", errors="coerce") < cutoff
            train = valid.loc[train_dates & valid.status.isin(ACTIVE) & available]
            test = valid.loc[test_dates & valid.status.isin(ACTIVE)]
            key = dict(symbol=symbol, profile=profile, fold=number, train_start=str(start.date()),
                       test_start=str(boundary.date()), test_end_exclusive=str(end.date()))
            folds.append(dict(key, train_end_exclusive=str(boundary.date()),
                              valid_test_sessions=int(test_dates.sum()), all_baseline_test_entries=len(test),
                              purged_train_outcomes=int((train_dates & valid.status.isin(ACTIVE) & ~available).sum())))
            for feature, columns in FEATURES.items():
                k = dict(key, feature=feature)
                if any(c not in valid for c in columns):
                    chosen_rows.append(dict(k, evidence="feature_not_exported", selected_trades=0))
                    continue
                tr = train.dropna(subset=list(columns))
                te = test.dropna(subset=list(columns)).sort_values("session_date")
                if tr.empty:
                    chosen_rows.append(dict(k, evidence="no_training_feature_history", selected_trades=0,
                                            matched_baseline_test_entries=len(te), all_baseline_test_entries=len(test)))
                    continue
                calibration = calibrate(tr, feature)
                tr = tr.assign(category=classify(tr, feature, calibration))
                te = te.assign(category=classify(te, feature, calibration))
                candidates = tr.groupby("category").gross_r.agg(["size", "mean"])
                candidates = candidates.loc[candidates["size"].ge(minimum_train)].sort_values(["mean"], ascending=False, kind="stable")
                category = candidates.index[0] if len(candidates) else None
                calibrations.append(dict(k, calibration=calibration, selected_category=category,
                                         training_feature_entries=len(tr), eligible_train_categories=candidates.to_dict("index"),
                                         all_train_categories=tr.groupby("category").gross_r.agg(["size", "mean"]).to_dict("index")))
                for name, part in te.groupby("category"):
                    categories.append(dict(k, category=name, matched_baseline_entries=len(te), **measures(part)))
                if category is None:
                    chosen_rows.append(dict(k, evidence="insufficient_training_category", selected_trades=0,
                                            matched_baseline_test_entries=len(te), all_baseline_test_entries=len(test)))
                    continue
                selected = te.loc[te.category.eq(category)]
                m = measures(selected)
                bm = measures(te)
                delta = (selected.gross_r.sum() - te.gross_r.sum()) / len(te) if len(te) else np.nan
                chosen_rows.append(dict(k, selected_category=category, selected_trades=len(selected),
                    evidence="limited" if len(selected) < 20 else "descriptive",
                    valid_test_sessions=int(test_dates.sum()), all_baseline_test_entries=len(test),
                    matched_baseline_test_entries=len(te), retained_pct=100 * len(selected) / len(te) if len(te) else np.nan,
                    selected_entry_pct_valid_sessions=100 * len(selected) / test_dates.sum() if test_dates.sum() else np.nan,
                    feature_coverage_pct=100 * len(te) / len(test) if len(test) else np.nan,
                    baseline_mean_r=bm["mean_r"], baseline_win_rate_pct=bm["win_rate_pct"],
                    baseline_total_r=bm["total_r"], delta_r_per_baseline_opportunity=delta,
                    **m, **random_control(te, selected, boundary, end, rng)))
                if len(te):
                    pred = te[KEYS + ["gross_r", "status", "category"]].copy()
                    pred["selected"] = pred.category.eq(category)
                    pred["feature"], pred["fold"] = feature, number
                    predictions.append(pred)
    fold_df, chosen = pd.DataFrame(folds), pd.DataFrame(chosen_rows)
    pred = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    aggregate = []
    if len(pred):
        if pred.duplicated(KEYS + ["feature"]).any():
            raise ValueError("Overlapping test dates")
        for (symbol, profile, feature), base in pred.groupby(["symbol", "profile", "feature"]):
            base = base.sort_values("session_date")
            selected = base.loc[base.selected]
            relevant = chosen.loc[chosen.symbol.eq(symbol) & chosen.profile.eq(profile) & chosen.feature.eq(feature)]
            delta = (selected.gross_r.sum() - base.gross_r.sum()) / len(base)
            aggregate.append(dict(symbol=symbol, profile=profile, feature=feature,
                matched_baseline_entries=len(base), selected_entries=len(selected), retained_pct=100 * len(selected) / len(base),
                baseline_mean_r=base.gross_r.mean(), baseline_win_rate_pct=100 * base.gross_r.gt(1e-12).mean(),
                baseline_total_r=base.gross_r.sum(), delta_r_per_baseline_opportunity=delta,
                selected_folds=int(relevant.selected_category.notna().sum()) if "selected_category" in relevant else 0,
                limited_folds=int(relevant.evidence.eq("limited").sum()),
                positive_delta_folds=int(relevant.delta_r_per_baseline_opportunity.gt(0).sum()),
                negative_delta_folds=int(relevant.delta_r_per_baseline_opportunity.lt(0).sum()),
                **measures(selected)))
    agg = pd.DataFrame(aggregate)
    for name, data in [("folds", fold_df), ("test_categories", pd.DataFrame(categories)),
                       ("selected_folds", chosen), ("test_predictions", pred), ("aggregate", agg)]:
        data.to_csv(out / f"{name}.csv", index=False)
    (out / "calibration.json").write_text(json.dumps(calibrations, indent=2, allow_nan=False), encoding="utf-8")
    return agg, chosen


def export_value_areas(args, observations):
    from observe import metadata, load_window
    from observe_four_profiles import clocks, quality
    from indicator.volume_profile import build_basic_volume_profile
    cfg = json.loads((args.profiles_run / "config_snapshot.json").read_text())
    rows = []
    for symbol, sessions in observations.groupby("symbol", sort=False):
        f, mins, maxs = metadata(args.root / cfg["assets"][symbol])
        for day, profiles in sessions.groupby("session_date", sort=True):
            previous, pre_ny, opening, _, _ = clocks(date.fromisoformat(day))
            arrays = load_window(f, mins, maxs, previous, opening)
            for name, start in [("pre_ny_0130_0930", pre_ny), ("previous_day_0930_0930", previous)]:
                match = profiles.loc[profiles.profile.eq(name)]
                if match.empty:
                    continue
                part = slice(np.searchsorted(arrays[0], start), np.searchsorted(arrays[0], opening))
                q = quality(*(a[part] for a in arrays), start, opening, cfg)
                row = dict(symbol=symbol, session_date=day, profile=name, status=q,
                           available_utc=pd.to_datetime(opening, unit="ms", utc=True).isoformat())
                if q == "valid":
                    vp = build_basic_volume_profile(pd.DataFrame(dict(price=arrays[1][part], qty=arrays[2][part])), n_bins=cfg["profile_bins"])
                    if not np.isclose(vp["poc_price"], match.iloc[0].poc, rtol=1e-10, atol=1e-8):
                        raise ValueError(f"POC mismatch: {symbol} {day} {name}")
                    row.update(poc=vp["poc_price"], val=vp["val"], vah=vp["vah"])
                rows.append(row)
            if len(rows) % 100 == 0:
                pd.DataFrame(rows).to_csv(args.output / "value_areas.csv", index=False)
                print(f"{symbol} {day}: value-area export", flush=True)
    pd.DataFrame(rows).to_csv(args.output / "value_areas.csv", index=False)


def report(out, aggregate, chosen, ledger):
    lines = ["# Mean-reversion context v09", "",
        "Retrospective research on previously inspected history; not an untouched holdout or a live model.",
        "Same old POC reversal entries, symmetric 1R barriers and pre-17:00 time exit. Gross results; costs/fills not simulated.",
        "Train 8 calendar months, test 2, advance 2; thresholds and categories chosen only in train. All feature families are separate trials.",
        "Selected entries are compared with baseline entries having the same feature availability. Inspect coverage, missing history and limited folds before ranking.",
        "WR includes every positive close, including time exits. TP-first and POC-touch rates are separate metrics.", "",
        "## Data and pending annotations", ""]
    for symbol, g in ledger.loc[ledger.profile_status.eq("valid")].groupby("symbol"):
        d = g.drop_duplicates("session_date")
        lines.append(f"- {symbol}: {len(d)} source-valid dates; cached pre-13:00 features valid on {int(d.feature_status.eq('valid').sum())}. "
                     f"4h macro available: {int(d.get('macro_4h_available', pd.Series(False, index=d.index)).fillna(False).sum())}; "
                     f"daily macro available: {int(d.get('macro_1d_available', pd.Series(False, index=d.index)).fillna(False).sum())}.")
        last = pd.Timestamp(d.session_date.max())
        schedules = list(fold_schedule(d.session_date.min(), d.session_date.max()))
        next_start = schedules[-1][2] if schedules else None
        lines.append(f"  Source dates: {d.session_date.min()} through {d.session_date.max()}; complete folds: {len(schedules)}; "
                     f"trailing dates after {next_start.date() if next_start is not None else 'initial eight-month train'} are not scored as a shortened test.")
    if not any(c.startswith("value_pre_ny_") for c in ledger):
        lines.append("- VAH/VAL acceptance/rejection pending the explicit user-run raw-trade export. POC migration is available independently.")
    lines += ["", "## All train-selected feature results: today's 09:30-13:00 POC", "",
        "One row per feature/asset; no automatically chosen winning indicator. Test dates vary with feature availability. Missing folds are in selected_folds.csv.", "",
        "| Asset | Feature | Matched entries | Kept | WR baseline / kept | Mean R baseline / kept | Delta R / original opportunity | Folds + / - |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    if len(aggregate):
        for r in aggregate.loc[aggregate.profile.eq("ny_0930_1300")].sort_values(["symbol", "feature"]).itertuples():
            wr = f"{r.win_rate_pct:.1f}" if r.trades else "NA"
            mean = f"{r.mean_r:+.4f}" if r.trades else "NA"
            lines.append(f"| {r.symbol} | {r.feature} | {r.matched_baseline_entries} | {r.selected_entries} ({r.retained_pct:.1f}%) | "
                         f"{r.baseline_win_rate_pct:.1f} / {wr} | {r.baseline_mean_r:+.4f} / {mean} | {r.delta_r_per_baseline_opportunity:+.4f} | {r.positive_delta_folds} / {r.negative_delta_folds} |")
    lines += ["", "Per-fold random-control ranges/tail fractions are descriptive equal-frequency thinning diagnostics, not corrected significance tests. "
              "Seven-day block bootstrap intervals describe the R contribution after discarded trades. Fewer than 20 selected test trades = limited evidence, not a pass/fail rule.",
              "All four POC variants are in aggregate.csv; do not add them as an independent portfolio. "
              "Calibration, every test category, feature exclusions and train-selected test rows are preserved. "
              "This search examines many hypotheses and cannot establish significance by picking the best row.", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    p.add_argument("--stage", choices=["all", "features", "analyze", "value-areas"], default="all")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--profiles-run", type=Path)
    p.add_argument("--trades-run", type=Path)
    p.add_argument("--candles-config", type=Path)
    p.add_argument("--value-areas", type=Path)
    p.add_argument("--start")
    p.add_argument("--end")
    args = p.parse_args()
    args.root = args.root.resolve()
    model = args.root / "model_development/models/mean_reversion_by_volume_profile"
    args.profiles_run = args.profiles_run or model / "runs/four_profiles_1300_v02"
    args.trades_run = args.trades_run or model / "runs/poc_rr_aligned_v03"
    args.candles_config = args.candles_config or model / "vwap_candles_config.json"
    sys.path[:0] = [str(args.root), str(model)]
    args.output.mkdir(parents=True, exist_ok=True)
    observations = pd.read_csv(args.profiles_run / "daily.csv")
    observations = observations.loc[observations.status.eq("valid")]
    if args.start:
        observations = observations.loc[observations.session_date.ge(args.start)]
    if args.end:
        observations = observations.loc[observations.session_date.le(args.end)]
    if observations.empty:
        raise ValueError("No valid sessions in requested range")
    snapshot = dict(stage=args.stage, status="running", recorded="2026-10-11", train_months=8, test_months=2,
        step_months=2, minimum_train_category=30, limited_test_below=20, random_repeats=500, seed=20261011,
        requested_start=args.start, requested_end=args.end,
        features=FEATURES, prior_inspection=True, sources=[fingerprint(args.profiles_run / "daily.csv"),
        fingerprint(args.profiles_run / "config_snapshot.json"), fingerprint(args.trades_run / "trades.csv"),
        fingerprint(args.trades_run / "config_snapshot.json"), fingerprint(args.candles_config)],
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    config_path = args.output / "config_snapshot.json"
    if args.stage != "analyze":
        if config_path.exists():
            raise ValueError("Use a new output directory to preserve prior results")
        config_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    if args.stage == "value-areas":
        export_value_areas(args, observations)
    else:
        if args.stage in ["all", "features"]:
            features, sources = build_features(args, observations)
            features.to_csv(args.output / "daily_features.csv", index=False)
            trades = pd.read_csv(args.trades_run / "trades.csv")
            ledger = trades.merge(observations[KEYS + ["returned_to_poc"]], on=KEYS, how="inner", validate="one_to_one")
            ledger = ledger.merge(features, on=["symbol", "session_date"], how="left", validate="many_to_one")
            ledger["distance_poc_atr"] = (ledger.price_before_1300 - ledger.poc).abs() / ledger.atr_1h
            ledger.to_csv(args.output / "context_ledger.csv", index=False)
            ledger.groupby(["symbol", "feature_status"], dropna=False).size().rename("profile_sessions").to_csv(args.output / "quality.csv")
            snapshot["sources"] += sources
            if args.value_areas:
                snapshot["sources"].append(fingerprint(args.value_areas))
        else:
            snapshot = json.loads(config_path.read_text())
            ledger = pd.read_csv(args.output / "context_ledger.csv")
        if args.stage in ["all", "analyze"]:
            aggregate, chosen = analyze(ledger, args.output)
            report(args.output, aggregate, chosen, ledger)
    snapshot["status"] = "complete"
    config_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"Completed {args.stage}: {args.output}", flush=True)


if __name__ == "__main__":
    main()
