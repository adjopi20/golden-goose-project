"""v16: train-calibrated pre-touch delta/weak-progress filter on v15 entries."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import previous_value_area_reversal as base
from observe_edge_context import COLS, minute_grid, completed_states, state_before, slice_minutes, fingerprint

FEATURES = ["outward_delta_imbalance", "outward_progress_atr"]


def feature_at(candles, hourly, entry_ms, side):
    cutoff = pd.Timestamp(int(entry_ms)//60000*60000, unit="ms", tz="UTC")
    part = slice_minutes(candles, cutoff-pd.Timedelta(minutes=5), cutoff)
    result = dict(feature_status="missing_five_minutes", feature_cutoff_utc=cutoff.isoformat())
    if part is None:
        return result
    volume = float(part.volume.sum())
    if volume <= 0:
        return dict(result, feature_status="zero_volume")
    state = state_before(hourly, cutoff, "1h") if len(hourly) else None
    if state is None or not np.isfinite(state.atr) or state.atr <= 0:
        return dict(result, feature_status="missing_hourly_atr")
    outward = -int(side)
    return dict(result, feature_status="valid", outward_delta_imbalance=outward*float(part.buy_volume.sum()-part.sell_volume.sum())/volume,
                outward_progress_atr=outward*float(part.close.iloc[-1]-part.open.iloc[0])/float(state.atr),
                pre_touch_volume=volume, atr_1h=float(state.atr), atr_available_utc=state.name.isoformat())


def fit_gate(train):
    available = train.loc[train.feature_status.eq("valid")]
    positive = available.loc[available.outward_delta_imbalance.gt(0)]
    if len(available) < 25 or len(positive) < 10:
        return None
    return dict(imbalance_median=float(positive.outward_delta_imbalance.median()),
                progress_median=float(positive.outward_progress_atr.median()),
                train_available=len(available), train_positive_imbalance=len(positive))


def apply_gate(frame, gate):
    if gate is None:
        return pd.Series(False, index=frame.index)
    return (frame.feature_status.eq("valid") & frame.outward_delta_imbalance.gt(0)
            & frame.outward_delta_imbalance.ge(gate["imbalance_median"])
            & frame.outward_progress_atr.le(gate["progress_median"]))


def extract_features(trades, root, candle_config):
    frames, sources = [], []
    for symbol, group in trades.groupby("symbol", sort=True):
        stamps = pd.to_datetime(group.entry_timestamp_ny, utc=True, format="ISO8601")
        left = int((stamps.min()-pd.Timedelta(days=7)).value//1_000_000)
        right = int((stamps.max()+pd.Timedelta(minutes=1)).value//1_000_000)
        chunks = []
        for relative in candle_config[symbol]:
            path = root/relative
            sources.append(fingerprint(path))
            chunks.append(pd.read_parquet(path, columns=COLS, filters=[("timestamp_ms", ">=", left), ("timestamp_ms", "<", right)]))
        c = minute_grid(pd.concat(chunks, ignore_index=True))
        # Keep the old resampling/ATR implementation but never treat imputed
        # flat candles as observed orderflow or valid ATR history.
        c["valid"] &= ~c.imputed
        hourly = completed_states(c, "1h", root)
        rows = []
        for r in group.to_dict("records"):
            entry = pd.Timestamp(r["entry_timestamp_ny"])
            values = feature_at(c, hourly, int(entry.value//1_000_000), r["side"])
            if values["feature_status"] == "valid":
                assert pd.Timestamp(values["feature_cutoff_utc"]) <= entry
                assert pd.Timestamp(values["atr_available_utc"]) <= entry
            rows.append(dict(r, **values))
        frames.append(pd.DataFrame(rows))
        print(f"{symbol}: {sum(r['feature_status']=='valid' for r in rows)}/{len(rows)} valid pre-touch features", flush=True)
    return pd.concat(frames, ignore_index=True), sources


def evaluate(ledger, source_folds, cfg):
    folds, predictions, calibrations = [], [], []
    test_folds = source_folds.loc[source_folds.phase.eq("test")]
    for f in test_folds.itertuples():
        g = ledger.loc[ledger.symbol.eq(f.symbol)]
        train = g.loc[g.session_date.ge(f.train_start) & g.session_date.lt(f.test_start)]
        cutoff = pd.Timestamp(f.test_start).tz_localize(base.NY).tz_convert("UTC")
        known = pd.to_datetime(train.exit_timestamp_ny, utc=True, format="ISO8601").lt(cutoff)
        train = train.loc[known]
        test = g.loc[g.session_date.ge(f.test_start) & g.session_date.lt(f.test_end_exclusive)].copy()
        gate = fit_gate(train)
        test["selected"] = apply_gate(test, gate)
        test["fold"] = int(f.fold)
        test["gate_supported"] = gate is not None
        test["imbalance_threshold"] = gate["imbalance_median"] if gate else np.nan
        test["progress_threshold"] = gate["progress_median"] if gate else np.nan
        predictions.append(test)
        calibrations.append(dict(symbol=f.symbol, fold=int(f.fold), train_start=f.train_start,
                                  test_start=f.test_start, test_end_exclusive=f.test_end_exclusive,
                                  train_outcomes_purged=int((~known).sum()), thresholds=gate))
        for policy, part in (("base_all", test), ("base_matched", test.loc[test.feature_status.eq("valid")]),
                             ("orderflow", test.loc[test.selected])):
            folds.append(dict(symbol=f.symbol, fold=int(f.fold), policy=policy, train_start=f.train_start,
                              test_start=f.test_start, test_end_exclusive=f.test_end_exclusive,
                              gate_supported=gate is not None, limited_sample=len(part)<20,
                              **base.stats(part, int(f.valid_sessions), cfg)))
    predictions = pd.concat(predictions, ignore_index=True)
    assert not predictions.duplicated(["symbol", "session_date"]).any()
    return predictions, pd.DataFrame(folds), calibrations


def controls(predictions, fold_metrics, repeats=2000, seed=20261023):
    rng = np.random.default_rng(seed)
    results = []
    for symbol, matched in predictions.loc[predictions.feature_status.eq("valid")].groupby("symbol"):
        kept = matched.loc[matched.selected]
        if kept.empty:
            results.append(dict(symbol=symbol, status="no_selected_trades"))
            continue
        random_sums = np.zeros((repeats, 2))
        sampled = np.zeros((repeats, 6))
        for fold, part in matched.groupby("fold"):
            n = int(part.selected.sum())
            r = part[["gross_r", "net_r"]].to_numpy()
            if n:
                picks = rng.random((repeats, len(part))).argsort(axis=1)[:, :n]
                random_sums += r[picks].sum(axis=1)
            meta = fold_metrics.loc[(fold_metrics.symbol == symbol) & (fold_metrics.fold == fold)].iloc[0]
            dates = pd.date_range(meta.test_start, pd.Timestamp(meta.test_end_exclusive)-pd.Timedelta(days=1))
            s = part.selected.to_numpy(dtype=float)
            daily = pd.DataFrame(dict(br=part.gross_r.to_numpy(), bn=part.net_r.to_numpy(),
                                      kr=part.gross_r.to_numpy()*s, kn=part.net_r.to_numpy()*s,
                                      bcount=np.ones(len(part)), kcount=s),
                                 index=pd.to_datetime(part.session_date)).reindex(dates, fill_value=0).to_numpy()
            days = len(dates)
            starts = rng.integers(days, size=(repeats, int(np.ceil(days/7)), 1))
            indices = ((starts+np.arange(7))%days).reshape(repeats, -1)[:, :days]
            sampled += daily[indices].sum(axis=1)
        good = (sampled[:, 4] > 0) & (sampled[:, 5] > 0)
        row = dict(symbol=symbol, repeats=repeats, status="passed", calendar_block_days=7)
        for i, label in enumerate(("gross", "net")):
            observed = kept[label+"_r"].mean()
            draws = random_sums[:, i]/len(kept)
            delta = sampled[good, i+2]/sampled[good, 5]-sampled[good, i]/sampled[good, 4]
            row.update({label+"_same_frequency_upper_tail_fraction":float((1+(draws>=observed).sum())/(repeats+1)),
                        label+"_mean_r_lift":float(observed-matched[label+"_r"].mean()),
                        label+"_lift_block_ci025":float(np.quantile(delta, .025)),
                        label+"_lift_block_ci975":float(np.quantile(delta, .975))})
        results.append(row)
    return pd.DataFrame(results)


def run(source, root, output, candle_config):
    audit = json.loads((source/"audit.json").read_text())
    assert audit["status"] == "passed"
    for name, digest in audit["outputs"].items():
        assert base.sha(source/name) == digest, name
    cfg = json.loads((source/"config_snapshot.json").read_text())["source_snapshot"]["config"]
    source_data = pd.read_csv(source/"sessions.csv")
    trades = source_data.loc[source_data.status.isin(base.ACTIVE)].copy()
    source_folds = pd.read_csv(source/"fold_metrics.csv")
    design = Path(__file__).with_name("VA_ORDERFLOW_DESIGN.md")
    output.mkdir(parents=True, exist_ok=False)
    (output/"audit.json").write_text(json.dumps(dict(status="running")))
    sys.path.insert(0, str(root.resolve()))
    candle_paths = json.loads(candle_config.read_text(encoding="utf-8-sig"))
    ledger, sources = extract_features(trades, root, candle_paths)
    for key in FEATURES:
        if key not in ledger:
            ledger[key] = np.nan
    predictions, fold_metrics, calibrations = evaluate(ledger, source_folds, cfg)
    selected = predictions.loc[predictions.selected]
    assert selected.feature_status.eq("valid").all()
    assert selected.gate_supported.all()
    # Trade geometry/outcomes are inherited, never recalculated or replaced.
    merged = selected.merge(trades, on=["symbol", "session_date"], suffixes=("_new", "_old"), validate="one_to_one")
    for col in ("entry", "exit_price", "poc", "sl", "gross_r", "net_r"):
        np.testing.assert_allclose(merged[col+"_new"], merged[col+"_old"])
    summary, coverage, yearly = [], [], []
    for symbol, part in predictions.groupby("symbol"):
        test_meta = source_folds.loc[source_folds.symbol.eq(symbol) & source_folds.phase.eq("test")]
        denominator = int(test_meta.valid_sessions.sum())
        expected = int(test_meta.trades.sum())
        assert len(part) == expected
        for policy, g in (("base_all", part), ("base_matched", part.loc[part.feature_status.eq("valid")]),
                          ("orderflow", part.loc[part.selected])):
            summary.append(dict(symbol=symbol, policy=policy, **base.stats(g, denominator, cfg)))
            for year, annual in g.groupby(g.session_date.str[:4]):
                yearly.append(dict(symbol=symbol, policy=policy, year=year, **base.stats(annual, len(annual), cfg)))
        coverage.append(dict(symbol=symbol, baseline_entries=len(part), feature_valid=int(part.feature_status.eq("valid").sum()),
                             missing_features=int(part.feature_status.ne("valid").sum()), selected=int(part.selected.sum()),
                             feature_valid_rejected=int((part.feature_status.eq("valid") & ~part.selected).sum()),
                             unsupported_folds=int(test_meta.fold.nunique()-part.loc[part.gate_supported, "fold"].nunique())))
    summary = pd.DataFrame(summary)
    robustness = controls(predictions, fold_metrics)
    for name, frame in (("feature_ledger", ledger), ("test_predictions", predictions), ("fold_metrics", fold_metrics),
                        ("summary", summary), ("coverage", pd.DataFrame(coverage)), ("yearly_metrics", pd.DataFrame(yearly)),
                        ("robustness", robustness)):
        frame.to_csv(output/f"{name}.csv", index=False)
    ledger.groupby(["symbol", "feature_status"]).size().rename("entries").reset_index().to_csv(output/"feature_quality.csv", index=False)
    (output/"calibration.json").write_text(json.dumps(calibrations, indent=2, allow_nan=False))
    for record in sources:
        assert fingerprint(record["path"]) == record, "Candle cache changed during run"
    snap = dict(rule_version="va_orderflow_v16_1", source=str(source.resolve()), source_audit_sha256=base.sha(source/"audit.json"),
                code_sha256=base.sha(__file__), design_sha256=base.sha(design), config=cfg, window_minutes=5,
                sources=sources, repeats=2000, seed=20261023, helpers={str(Path(m.__file__).resolve()):base.sha(m.__file__)
                    for m in (base, sys.modules["observe_edge_context"], sys.modules["indicator.trend"])})
    (output/"config_snapshot.json").write_text(json.dumps(snap, indent=2))
    (output/"DESIGN.md").write_text(design.read_text(encoding="utf-8"), encoding="utf-8")
    lines = ["# v16: pre-touch orderflow on v15", "",
             "One fixed gate: five completed minutes before the first outward VA touch; strong outward aggressive imbalance with weak price progress. "
             "Thresholds are medians of positive-imbalance TRAIN trades, frozen for the next test. 8-month train / 2-month test, step 2. "
             "No entry delay or replacement, no profile rebuild, unchanged POC TP, 1:1 planned stop and sizing. "
             "Fee 0.04% each side; account risk 0.5%, max entry notional/equity 5. Assets separate; return is cumulative, not annual.", "",
             "Base-all is the exact source v15 test population. Base-matched removes only unavailable orderflow features. "
             "Orderflow selects a subset; unsupported training gates cannot enter. Missing-feature counts are in coverage.csv. "
             "Positive close WR includes time exits. Raw R and capped capital-risk R differ.", "",
             "| Asset | Policy | Trades | Gross WR | Gross mean R | Net mean R | Gross total R | Net account |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in summary.to_dict("records"):
        def fmt(key, suffix=""):
            return f"{r[key]:.3f}{suffix}" if pd.notna(r.get(key)) else "—"
        lines.append(f"| {r['symbol']} | {r['policy']} | {r['trades']} | "+" | ".join(fmt(k, "%" if k.endswith("pct") else "") for k in
                     ("gross_wr_pct", "gross_mean_r", "net_mean_r", "gross_total_r", "net_account_return_pct"))+" |")
    lines += ["", "| Asset | Positive net-account selected folds | Gross mean-R improvement vs matched folds | Selected folds below 20 trades |", "|---|---:|---:|---:|"]
    for symbol, group in fold_metrics.groupby("symbol"):
        kept = group.loc[group.policy.eq("orderflow")].set_index("fold")
        matched = group.loc[group.policy.eq("base_matched")].set_index("fold")
        lines.append(f"| {symbol} | {int(kept.net_account_return_pct.gt(0).sum())}/{len(kept)} | "
                     f"{int(kept.gross_mean_r.gt(matched.gross_mean_r).sum())}/{len(kept)} | {int(kept.trades.lt(20).sum())}/{len(kept)} |")
    lines += ["", "The gate and five-minute horizon were specified before this run, not optimized against its test returns. "
              "This remains previously studied development history, not new untouched OOS evidence. "
              "robustness.csv reports uncorrected equal-frequency random control and paired seven-calendar-day block intervals. "
              "Small counts and resampled intervals do not establish stable regimes.", "",
              "Pre-touch aggression/price inefficiency is not proof of absorption at the VA boundary. "
              "The entry minute and post-entry flow are excluded. Trigger-print fills, last-pre-cutoff time exit and closed-trade drawdown "
              "retain v15's execution limits: no funding, spread, latency, market impact, liquidation or intratrade drawdown."]
    (output/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    files = {p.name:base.sha(p) for p in output.iterdir() if p.is_file() and p.name != "audit.json"}
    (output/"audit.json").write_text(json.dumps(dict(status="passed", source_entries=len(trades), test_entries=len(predictions),
         selected_test_entries=len(selected), profile_rebuilds=0, outputs=files), indent=2))
    print((output/"REPORT.md").read_text(), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candle-config", type=Path, default=Path(__file__).with_name("vwap_candles_config.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.root, args.output, args.candle_config)
