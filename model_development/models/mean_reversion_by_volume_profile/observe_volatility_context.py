"""Observe sizing volatility on fixed POC reversals; cached candles only."""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
FEATURES = {
    "ewma20_level": ("sigma_ewma20",),
    "poc_distance_ewma20": ("poc_distance_ewma20",),
    "macro_expansion_20_60": ("macro_expansion_20_60",),
    "sensitivity_ewma10_level": ("sigma_ewma10",),
    "sensitivity_ewma40_level": ("sigma_ewma40",),
}


def configure(root):
    sys.path[:0] = [str(root), str(root / "model_development/models/mean_reversion_by_volume_profile")]
    from model_development.phase_3 import volatility_sizing as sizing
    import observe_edge_context as context
    return sizing, context


def macro_expansion(minutes):
    """Original macro 20/60 daily close-return ratio, available at NY09:30.

    Only the 09:29 minute close is required for each daily close. Reindex calendar
    dates so a missing boundary close cannot create a multi-day 'daily' return.
    Intraday gaps do not invent endpoint prices; no intraday OHLC fill is used.
    """
    m = minutes.sort_values("timestamp_ms").drop_duplicates("timestamp_ms")
    local = pd.to_datetime(m.timestamp_ms, unit="ms", utc=True).dt.tz_convert(NY)
    end = local.dt.hour.eq(9) & local.dt.minute.eq(29)
    closes = pd.Series(m.loc[end, "close"].to_numpy(),
                       index=pd.DatetimeIndex(local.loc[end].dt.tz_localize(None).dt.normalize()))
    first = local.iloc[0].tz_localize(None).normalize()
    last = local.iloc[-1].tz_localize(None).normalize()
    closes = closes.reindex(pd.date_range(first, last, freq="D"))
    returns = np.log(closes).diff()
    vol20 = returns.rolling(20, min_periods=20).std() * math.sqrt(365)
    vol60 = returns.rolling(60, min_periods=60).std() * math.sqrt(365)
    result = pd.DataFrame(dict(session_date=closes.index.strftime("%Y-%m-%d"),
        macro_vol20=vol20.to_numpy(), macro_vol60=vol60.to_numpy()))
    result["macro_expansion_20_60"] = np.log(result.macro_vol20 / result.macro_vol60).replace([np.inf, -np.inf], np.nan)
    result["macro_snapshot_ms"] = [int(pd.Timestamp(d + " 09:30", tz=NY).timestamp()*1000)
                                   for d in result.session_date]
    return result


def normalized_distance(price, poc, sigma):
    # Dimensionless distance / predicted 24h return volatility, not a hitting probability.
    return (price - poc).abs() / price / sigma.where(sigma.gt(0))


def features(root, candle_config, out, sizing):
    rows, sources = [], []
    for symbol, relative in candle_config.items():
        paths = [(root / p).resolve() for p in relative]
        records = [dict(path=str(p), sha256=sizing.sha256(p)) for p in paths]
        signature = dict(version=sizing.VERSION, symbol=symbol, sources=records,
                         timezone=NY, snapshot="09:00", interval_minutes=15)
        key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        existing = root / "tmp/cache/risk_research/volatility_15m_v01" / key
        # Reuse a verified sizing cache read-only; otherwise compute in this new run.
        marker = existing / "complete.json"
        frames = [pd.read_parquet(p, columns=["timestamp_ms", "close", "symbol"]) for p in paths]
        minutes = pd.concat(frames, ignore_index=True)
        if set(minutes.symbol) != {symbol} or not np.isfinite(minutes.close).all() or minutes.close.le(0).any():
            raise ValueError(f"Invalid symbol/prices: {symbol}")
        if minutes.timestamp_ms.isna().any() or minutes.timestamp_ms.mod(60000).ne(0).any():
            raise ValueError("Expected minute-open timestamps")
        duplicates = minutes.loc[minutes.duplicated("timestamp_ms", keep=False)]
        if len(duplicates) and duplicates.groupby("timestamp_ms").close.nunique().gt(1).any():
            raise ValueError("Conflicting overlapping candle caches")
        if marker.is_file():
            saved = json.loads(marker.read_text(encoding="utf-8"))
            data = existing / "daily_variance.csv"
            if saved["signature"] != signature or sizing.sha256(data) != saved["data_sha256"]:
                raise ValueError("Sizing derived-cache validation failed")
            daily = pd.read_csv(data)
            print(f"{symbol}: reused verified sizing variance cache", flush=True)
        else:
            daily = sizing.daily_variance(minutes, symbol)
            print(f"{symbol}: computed sizing variance from cached minutes", flush=True)
        daily.to_csv(out / f"daily_variance_{symbol}.csv", index=False)
        forecasts = {}
        for half in [10, 20, 40]:
            f = pd.DataFrame(sizing.forecasts(daily, half, 120).values())
            forecasts[half] = f
        f = forecasts[20].rename(columns={"session_day": "session_date", "sigma_24h": "sigma_ewma20",
                                          "snapshot_ms": "ewma_snapshot_ms"})
        for half in [10, 40]:
            f[f"sigma_ewma{half}"] = forecasts[half].sigma_24h
        f = f.merge(macro_expansion(minutes), on="session_date", how="outer", validate="one_to_one")
        f["symbol"] = symbol
        rows.append(f)
        sources.append(dict(**signature, reused_sizing_cache=str(existing) if marker.is_file() else None))
        del minutes, frames
    return pd.concat(rows, ignore_index=True), sources


def audit(out, ledger, original, predictions, aggregate, sizing, script):
    keys = ["symbol", "profile", "session_date"]
    assert not ledger.duplicated(keys).any()
    checked = ledger.merge(original, on=keys, suffixes=("", "_original"), validate="one_to_one")
    assert len(checked) == len(ledger)
    for column in ["entry", "tp", "sl", "risk_price", "gross_r"]:
        np.testing.assert_allclose(checked[column], checked[column + "_original"], equal_nan=True)
    assert checked.status.eq(checked.status_original).all()
    for column, hour, minute in [("ewma_snapshot_ms", 9, 0), ("macro_snapshot_ms", 9, 30)]:
        valid = ledger[column].notna()
        local = pd.to_datetime(ledger.loc[valid, column], unit="ms", utc=True).dt.tz_convert(NY)
        assert local.dt.hour.eq(hour).all() and local.dt.minute.eq(minute).all()
        assert local.dt.strftime("%Y-%m-%d").eq(ledger.loc[valid, "session_date"]).all()
    assert ledger.loc[ledger.sigma_ewma20.notna(), "consecutive_valid_days"].ge(120).all()
    assert not predictions.duplicated(keys + ["feature"]).any()
    for r in aggregate.itertuples():
        g = predictions.loc[predictions.symbol.eq(r.symbol) & predictions.profile.eq(r.profile) & predictions.feature.eq(r.feature)]
        keep = g.loc[g.selected]
        assert len(g) == r.matched_baseline_entries and len(keep) == r.selected_entries
        np.testing.assert_allclose(keep.gross_r.sum(), r.total_r, atol=1e-10)
        if len(keep):
            np.testing.assert_allclose(keep.gross_r.mean(), r.mean_r, atol=1e-10)
            np.testing.assert_allclose(100 * keep.gross_r.gt(1e-12).mean(), r.win_rate_pct, atol=1e-10)
        np.testing.assert_allclose((keep.gross_r.sum()-g.gross_r.sum())/len(g), r.delta_r_per_baseline_opportunity)
    result = dict(status="passed", code_sha256=sizing.sha256(script), profile_dates=len(ledger),
                  prediction_rows=len(predictions), aggregate_rows=len(aggregate),
                  checks=["unchanged entries/barriers/risks/outcomes", "unique nonoverlapping test keys",
                          "NY09:00 and NY09:30 feature snapshots", "120 valid-day warmup", "WR/R arithmetic"])
    (out / "audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def report(out, aggregate, chosen, ledger, context):
    lines = ["# Volatility context v10", "", "Retrospective observation of previously inspected history; not a fresh holdout.",
             "Fixed original entries at 13:00 NY, symmetric 1R TP/SL, target POC, forced close before 17:00. No sizing changes. Gross R, before fees/slippage.",
             "WR counts all positive closes, including time exits. Each feature is an independent experiment; 10/40 are sensitivity checks only.",
             "8 calendar months train / next 2 test / step 2. Train-only terciles; highest train mean-R category with >=30 trades is frozen for test.",
             "Baseline comparisons use exactly the same available-feature test entries and scored folds. Missing feature dates are not credited as successful filtering.", "",
             "## Coverage", "", "| Asset | Valid dates | Base entries | EWMA120 warmup ready | Macro20/60 ready |",
             "|---|---:|---:|---:|---:|"]
    for symbol, g in ledger.groupby("symbol"):
        d = g.drop_duplicates("session_date")
        b = g.loc[g.profile.eq("ny_0930_1300") & g.status.isin(context.ACTIVE)]
        lines.append(f"| {symbol} | {len(d)} | {len(b)} | {d.sigma_ewma20.notna().sum()} | {d.macro_expansion_20_60.notna().sum()} |")
    lines += ["", "## Today's 09:30-13:00 POC: train-selected category", "",
              "| Asset | Feature | Base -> kept | Kept % | WR base -> kept | Mean R base -> kept | Total R base -> kept | Folds / limited |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in aggregate.loc[aggregate.profile.eq("ny_0930_1300")].itertuples():
        lines.append(f"| {r.symbol} | {r.feature} | {r.matched_baseline_entries} -> {r.selected_entries} | {r.retained_pct:.1f} | "
                     f"{r.baseline_win_rate_pct:.1f} -> {r.win_rate_pct:.1f} | {r.baseline_mean_r:+.4f} -> {r.mean_r:+.4f} | "
                     f"{r.baseline_total_r:+.2f} -> {r.total_r:+.2f} | {r.selected_folds} / {r.limited_folds} |")
    lines += ["", "All four profiles: aggregate.csv. Every train calibration and test category is retained. selected_folds.csv includes coverage and unscored folds.",
              "Low/middle/high are relative to each training window, not universal absolute volatility bands. Macro expansion is log(sigma20/sigma60), where zero means equal daily-close volatility.",
              "POC distance = abs(pre13 price - POC)/pre13 price / EWMA20 sigma24h. This is geometry relative to historical volatility, not a probability or 4h forecast.",
              "random_tail_fraction is an equal-frequency random-thinning diagnostic, not a multiple-testing-corrected p-value. Opportunity delta includes the R of discarded entries; block intervals use seven-day blocks.",
              "Fewer than 20 selected trades per fold = limited evidence. Missing whole 15m bars reset EWMA warmup; missing individual minutes are counted, not filled. Missing 09:29 daily closes invalidate macro rolling windows.",
              "Category choices can change between folds. Do not select a half-life, asset, profile or indicator using this test history and then call the same history unseen validation.", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    root, out = args.root.resolve(), args.output.resolve()
    sizing, context = configure(root)
    if out.exists():
        raise ValueError("Choose a new output directory; prior results are immutable")
    out.mkdir(parents=True)
    model = root / "model_development/models/mean_reversion_by_volume_profile"
    files = [model / "runs/four_profiles_1300_v02/daily.csv", model / "runs/poc_rr_aligned_v03/trades.csv",
             model / "vwap_candles_config.json"]
    dependencies = [Path(sizing.__file__), Path(context.__file__), model / "performance_poc_rr.py",
                    root / "model_development/models/statistical_jump_model/macro_regime.py"]
    cfg = dict(recorded="2026-10-11", status="running", prior_inspection=True,
               train_months=8, test_months=2, step_months=2, minimum_train_category=30, limited_test_below=20,
               ewma_half_life_primary=20, sensitivity_half_lives=[10,40], warmup_valid_days=120,
               ewma_snapshot="09:00 NY", macro_snapshot="09:30 NY", seed=20261011, features=FEATURES,
               code_sha256=sizing.sha256(Path(__file__)),
               sources=[dict(path=str(x), sha256=sizing.sha256(x)) for x in files],
               dependencies=[dict(path=str(x), sha256=sizing.sha256(x)) for x in dependencies])
    config_path = out / "config_snapshot.json"
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    observations = pd.read_csv(files[0])
    observations = observations.loc[observations.status.eq("valid")]
    original = pd.read_csv(files[1])
    f, sources = features(root, json.loads(files[2].read_text()), out, sizing)
    f.to_csv(out / "daily_features.csv", index=False)
    ledger = original.merge(observations[context.KEYS + ["returned_to_poc"]], on=context.KEYS, how="inner", validate="one_to_one")
    ledger = ledger.merge(f, on=["symbol", "session_date"], how="left", validate="many_to_one")
    ledger["poc_distance_ewma20"] = normalized_distance(ledger.price_before_1300, ledger.poc, ledger.sigma_ewma20)
    ledger.to_csv(out / "context_ledger.csv", index=False)
    coverage = []
    for (symbol, profile), g in ledger.groupby(["symbol", "profile"]):
        for feature, columns in FEATURES.items():
            b = g.loc[g.status.isin(context.ACTIVE)]
            coverage.append(dict(symbol=symbol, profile=profile, feature=feature, valid_sessions=len(g),
                                 all_base_entries=len(b), available_entries=len(b.dropna(subset=list(columns)))))
    pd.DataFrame(coverage).to_csv(out / "coverage.csv", index=False)
    previous = context.FEATURES
    try:
        context.FEATURES = FEATURES
        aggregate, chosen = context.analyze(ledger, out)
    finally:
        context.FEATURES = previous
    predictions = pd.read_csv(out / "test_predictions.csv")
    report(out, aggregate, chosen, ledger, context)
    audit(out, ledger, original, predictions, aggregate, sizing, Path(__file__))
    cfg.update(status="complete", candle_sources=sources)
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Completed and audited: {out}", flush=True)


if __name__ == "__main__":
    main()
