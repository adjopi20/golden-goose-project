"""Independent reconciliation and descriptive uncertainty for a completed v18 run."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(run, source):
    audit = json.loads((run/"audit.json").read_text())
    assert audit["status"] == "passed"
    for name, sha in audit["outputs"].items():
        assert digest(run/name) == sha, name
    original = pd.read_csv(source/"sessions.csv")
    original = original.loc[original.status.isin(["tp_first", "sl_first", "time_exit"])]
    keys = ["symbol", "timeframe", "entry_mode", "session_date"]
    ledger = pd.read_parquet(run/"feature_ledger.parquet")
    labels = pd.read_parquet(run/"test_labels.parquet")
    states = pd.read_parquet(run/"macro_states.parquet")
    cuts = pd.read_csv(run/"train_cutpoints.csv")
    folds = pd.read_csv(run/"fold_metrics.csv")
    original_folds = pd.read_csv(source/"fold_metrics.csv")
    fold_keys = ["symbol", "timeframe", "entry_mode", "fold", "phase"]
    original_measures = ["trades", "gross_mean_r", "gross_total_r", "net_mean_r", "net_total_r"]
    reconciled = folds.loc[folds.category.eq("all_base")].merge(
        original_folds[fold_keys+original_measures], on=fold_keys, suffixes=("", "_original"), validate="many_to_one")
    assert len(reconciled) == len(folds.loc[folds.category.eq("all_base")])
    for measure in original_measures:
        np.testing.assert_allclose(reconciled[measure], reconciled[measure+"_original"], equal_nan=True, atol=1e-10)
    summary = pd.read_csv(run/"regime_summary.csv")
    cache_min_trade_counts = {}
    for fp in json.loads((run/"config_snapshot.json").read_text())["sources"]:
        if Path(fp["path"]).name == "candles_1m.parquet":
            parquet = pq.ParquetFile(fp["path"])
            col = parquet.schema.names.index("trade_count")
            minimum = min(parquet.metadata.row_group(i).column(col).statistics.min for i in range(parquet.num_row_groups))
            assert minimum >= 1, "Cached candles include minutes with no observed trades"
            cache_min_trade_counts[Path(fp["path"]).parent.name] = minimum
    assert len(ledger) == 2*len(original)
    for snapshot, p in ledger.groupby("snapshot"):
        p = p.set_index(keys).sort_index()
        o = original.set_index(keys).sort_index()
        pd.testing.assert_frame_equal(p[o.columns], o, check_dtype=False)
        clock_column = "profile_end_ny" if snapshot == "ny_open" else "entry_timestamp_ny"
        expected_clock = np.array([stamp.value//1000000 for stamp in pd.to_datetime(o[clock_column], utc=True, format="ISO8601")])
        np.testing.assert_array_equal(p.snapshot_ms.to_numpy(), expected_clock)
        expected_exit = np.array([stamp.value//1000000 for stamp in pd.to_datetime(o.exit_timestamp_ny, utc=True, format="ISO8601")])
        np.testing.assert_array_equal(p.exit_ms.to_numpy(), expected_exit)
    checked = 0
    for row in cuts.to_dict("records"):
        clock = "4h" if row["feature"].endswith("4h") else "1d"
        z = states.loc[states.symbol.eq(row["symbol"]) & states.clock.eq(clock)]
        start = pd.Timestamp(row["train_start"], tz="America/New_York").tz_convert("UTC").value//1000000
        end = pd.Timestamp(row["test_start"], tz="America/New_York").tz_convert("UTC").value//1000000
        x = z.loc[z.available_ms.between(start, end-1), row["feature"]].dropna().to_numpy()
        assert len(x) == row["train_state_count"]
        if len(x):
            np.testing.assert_allclose(np.quantile(x, [1/3, 2/3]), [row["q_low"], row["q_high"]], atol=1e-12)
        checked += 1
    joined = labels.merge(cuts[["symbol", "fold", "feature", "q_low", "q_high", "supported"]],
                          on=["symbol", "fold", "feature"], validate="many_to_one")
    expected = np.where(~np.isfinite(joined.value), "missing_value", np.where(~joined.supported,
        "unsupported_train", np.where(joined.value <= joined.q_low, "low",
        np.where(joined.value <= joined.q_high, "middle", "high"))))
    assert (joined.category == expected).all()
    macro_comparisons = price_probes = price_failures = 0
    for (symbol, clock), z in states.groupby(["symbol", "clock"]):
        z = z.sort_values("available_ms")
        p = ledger.loc[ledger.symbol.eq(symbol)]
        i = np.searchsorted(z.available_ms.to_numpy(), p.snapshot_ms.to_numpy(), side="right")-1
        safe = np.maximum(i, 0)
        available = z.available_ms.to_numpy()[safe]
        width = 14400000 if clock == "4h" else 86400000
        fresh = (i >= 0) & (p.snapshot_ms.to_numpy()-available < width)
        for feature in [c for c in cuts.feature.unique() if c.endswith(clock)]:
            expected_value = z[feature].to_numpy()[safe].copy()
            expected_value[~fresh] = np.nan
            np.testing.assert_allclose(p[feature], expected_value, equal_nan=True, atol=1e-12)
            macro_comparisons += len(p)
        # A data-source alignment check only: the containing bar may be unfinished at entry.
        t = original.loc[original.symbol.eq(symbol)]
        entry_times = pd.to_datetime(t.entry_timestamp_ny, utc=True, format="ISO8601")
        entry_ms = np.array([stamp.value//1000000 for stamp in entry_times], dtype=np.int64)
        look = pd.DataFrame(dict(start_ms=entry_ms//width*width, entry=t.entry.to_numpy()))
        probe = look.merge(z[["start_ms", "high", "low"]], on="start_ms", how="left", validate="many_to_one")
        failure = probe.low.isna() | (probe.entry < probe.low-1e-9) | (probe.entry > probe.high+1e-9)
        price_probes += len(probe)
        price_failures += int(failure.sum())
    assert price_failures == 0, ("Cached / raw macro OHLC mismatch", price_failures)
    strata = keys[:3]+["snapshot", "feature"]
    for key, p in labels.groupby(strata):
        view = summary
        for name, value in zip(strata, key):
            view = view.loc[view[name].eq(value)]
        for category in ("all_base", "matched_base", "low", "middle", "high", "missing_value", "unsupported_train"):
            q = p if category == "all_base" else p.loc[p.category.isin(["low", "middle", "high"])] if category == "matched_base" else p.loc[p.category.eq(category)]
            row = view.loc[view.category.eq(category)].iloc[0]
            assert len(q) == row.trades
            if len(q):
                for prefix in ("gross", "net"):
                    x = q[prefix+"_r"].to_numpy()
                    np.testing.assert_allclose([x.sum(), x.mean(), np.median(x), 100*(x > 0).mean()],
                        [row[prefix+"_total_r"], row[prefix+"_mean_r"], row[prefix+"_median_r"], row[prefix+"_wr_pct"]], atol=1e-10)
    result = dict(status="passed", source_trade_rows=len(original), immutable_snapshot_rows=len(ledger),
        calibrated_cutpoints_checked=checked, test_categories_checked=len(labels),
        macro_snapshot_values_checked=macro_comparisons, entry_price_ohlc_probes=price_probes,
        entry_price_ohlc_failures=price_failures, summary_rows_reconciled=len(summary),
        original_train_test_fold_rows_reconciled=len(reconciled),
        cache_min_trade_counts=cache_min_trade_counts,
        source_audit_sha256=digest(source/"audit.json"), run_audit_sha256=digest(run/"audit.json"))
    (run/"VALIDATION.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    return labels, folds


def uncertainty(run, labels, folds, repeats=2000, feature_list=None, seed=20261025, output_name="primary_uncertainty.csv"):
    """Fixed narrative parents/features; paired 7-day circular block resampling within test folds.

    These exploratory intervals are unadjusted for multiple research comparisons.
    Cutpoints stay fixed: this describes sample uncertainty, not research-selection uncertainty.
    """
    focus = {"BTCUSDC": ("2h", "body_close"), "ETHUSDC": ("4h", "intrabar"),
             "XRPUSDT": ("4h", "intrabar"), "LTCUSDC": ("2h", "body_close")}
    primary = feature_list if feature_list is not None else ["adx14_4h", "er30_4h", "rv20_1d"]
    rng = np.random.default_rng(seed)
    records = []
    for symbol, (tf, mode) in focus.items():
        a = labels.loc[labels.symbol.eq(symbol) & labels.timeframe.eq(tf) & labels.entry_mode.eq(mode)
                       & labels.feature.isin(primary) & labels.category.isin(["low", "middle", "high"])]
        for (snapshot, feature), p in a.groupby(["snapshot", "feature"], sort=True):
            sampled = np.zeros((repeats, 8))
            for fold, z in p.groupby("fold"):
                meta = folds.loc[folds.symbol.eq(symbol) & folds.fold.eq(fold)].iloc[0]
                dates = pd.date_range(meta.test_start, pd.Timestamp(meta.test_end_exclusive)-pd.Timedelta(days=1))
                columns = [z.net_r.to_numpy(), np.ones(len(z))]
                for category in ("low", "middle", "high"):
                    flag = z.category.eq(category).to_numpy(float)
                    columns += [z.net_r.to_numpy()*flag, flag]
                daily = pd.DataFrame(np.column_stack(columns), index=pd.to_datetime(z.session_date)).reindex(dates, fill_value=0).to_numpy()
                days = len(dates)
                starts = rng.integers(days, size=(repeats, int(np.ceil(days/7)), 1))
                indices = ((starts+np.arange(7)) % days).reshape(repeats, -1)[:, :days]
                sampled += daily[indices].sum(axis=1)
            for j, category in enumerate(("low", "middle", "high")):
                total_col, count_col = 2+2*j, 3+2*j
                good = (sampled[:, count_col] > 0) & (sampled[:, 1] > 0)
                means = sampled[good, total_col]/sampled[good, count_col]
                lift = means-sampled[good, 0]/sampled[good, 1]
                z = p.loc[p.category.eq(category)]
                records.append(dict(symbol=symbol, timeframe=tf, entry_mode=mode, snapshot=snapshot,
                    feature=feature, category=category, trades=len(z), net_mean_r=z.net_r.mean(),
                    net_mean_ci025=float(np.quantile(means, .025)) if len(means) else np.nan,
                    net_mean_ci975=float(np.quantile(means, .975)) if len(means) else np.nan,
                    net_lift_vs_matched=z.net_r.mean()-p.net_r.mean(),
                    net_lift_ci025=float(np.quantile(lift, .025)) if len(lift) else np.nan,
                    net_lift_ci975=float(np.quantile(lift, .975)) if len(lift) else np.nan,
                    repeats=repeats, valid_repeats=int(good.sum()), block_days=7,
                    limited_evidence=len(z) < 20, multiple_comparison_adjusted=False))
    pd.DataFrame(records).to_csv(run/output_name, index=False)
    files = [name for name in ("VALIDATION.json", "primary_uncertainty.csv", "secondary_uncertainty.csv") if (run/name).exists()]
    result = dict(status="passed", descriptive_intervals=sum(len(pd.read_csv(run/name)) for name in files if name.endswith(".csv")),
        seeds={"primary": 20261025, "secondary": 20261026},
        repeats=repeats, block_days=7, category_selection=False, multiple_comparison_adjusted=False,
        outputs={name: digest(run/name) for name in files},
        diagnostics_script_sha256=digest(Path(__file__)))
    (run/"diagnostics_audit.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    labels, folds = check(args.run, args.source)
    uncertainty(args.run, labels, folds)
    secondary = ["adx14_1d", "ema50_slope_abs_4h", "ema50_slope_abs_1d", "atr_pct_4h", "atr_pct_1d", "er20_1d"]
    uncertainty(args.run, labels, folds, feature_list=secondary, seed=20261026, output_name="secondary_uncertainty.csv")
