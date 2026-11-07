"""Bounded train-only sizing on unchanged POC trades; no execution replay."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

KEYS = ["symbol", "profile", "session_date"]
SYMBOLS = ["BTCUSDC", "XRPUSDT"]
ACTIVE = ["tp_first", "sl_first", "time_exit"]
NY = "America/New_York"
BASE_RISK = .005
CAP = 5.
COSTS = [0., 1., 2., 5.]
TARGET_COSTS = [0., 2.]
FEATURES = {
    "adx_4h": "macro_4h_adx",
    "ema50_slope_4h": "macro_4h_ema_slope_signed",
    "poc_distance_volatility": "poc_distance_ewma20",
    "va_pre_ny": "value_pre_ny_outside_fraction",
    "va_previous": "value_previous_outside_fraction",
    "vwap_daily": "vwap_daily_z",
    "vwap_24h": "vwap_24h_z",
}
MODELS = {name: [name] for name in FEATURES}
MODELS.update(adx_plus_vwap=["adx_4h", "vwap_daily"],
              adx_plus_poc_distance=["adx_4h", "poc_distance_volatility"])


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, allow_nan=False), encoding="utf-8")


def labels(values, cuts):
    x = np.asarray(values, dtype=float)
    return np.where(~np.isfinite(x), -1, np.searchsorted(cuts, x, side="left"))


def fit_component(x, y):
    """Three coarse bins, fixed shrinkage, bounded scores; missing remains neutral."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if not np.isfinite(y).all():
        raise ValueError("Nonfinite training outcomes")
    finite = np.isfinite(x)
    artifact = dict(status="insufficient_history", available=int(finite.sum()),
                    cutpoints=[], groups=[], score_by_category=[0., 0., 0.])
    if finite.sum() < 90:
        return artifact
    cuts = np.quantile(x[finite], [1/3, 2/3])
    category = labels(x, cuts)
    overall = float(y[finite].mean())
    groups = []
    for c in range(3):
        values = y[category == c]
        n = len(values)
        mean = float(values.mean()) if n else None
        shrunk = float((values.sum() + 30 * overall) / (n + 30))
        groups.append(dict(category=c, n=n, mean=mean, shrunk_mean=shrunk, supported=n >= 30))
    supported = [g for g in groups if g["supported"]]
    artifact.update(cutpoints=cuts.tolist(), groups=groups, train_mean=overall)
    if len(supported) < 2:
        artifact["status"] = "insufficient_categories"
        return artifact
    center = sum(g["n"] * g["shrunk_mean"] for g in supported) / sum(g["n"] for g in supported)
    scale = max(.25, 2 * max(abs(g["shrunk_mean"] - center) for g in supported))
    scores = [float((g["shrunk_mean"] - center) / scale) if g["supported"] else 0. for g in groups]
    order = {g["category"]: rank for rank, g in enumerate(sorted(supported, key=lambda g: (g["shrunk_mean"], g["category"])), 1)}
    for g in groups:
        g["train_rank"] = order.get(g["category"])
    artifact.update(status="calibrated", center=float(center), scale=float(scale), score_by_category=scores)
    np.testing.assert_allclose(apply_component(x, artifact).mean(), 0., atol=1e-12)
    return artifact


def apply_component(x, artifact):
    if artifact["status"] != "calibrated":
        return np.zeros(len(x))
    category = labels(x, artifact["cutpoints"])
    return np.array([artifact["score_by_category"][c] if c >= 0 else 0. for c in category])


def effective_risk(multiplier, distance):
    return np.minimum(BASE_RISK * np.asarray(multiplier), CAP * np.asarray(distance))


def match_fixed_risk(distance, target):
    """Match TRAIN mean effective risk after the same notional cap."""
    d = np.asarray(distance, dtype=float)
    lo, hi = 0., 1.5 * BASE_RISK
    for _ in range(65):
        mid = (lo + hi) / 2
        if np.minimum(mid, CAP * d).mean() < target:
            lo = mid
        else:
            hi = mid
    fixed = (lo + hi) / 2
    np.testing.assert_allclose(np.minimum(fixed, CAP * d).mean(), target, atol=1e-12)
    return fixed


def load_inputs(root, out):
    model = root / "model_development/models/mean_reversion_by_volume_profile"
    sys.path[:0] = [str(model), str(root)]
    from observe_rolling_vwap_bands import rolling_states
    names = {
        "original": model / "runs/poc_rr_aligned_v03/trades.csv",
        "context": model / "runs/edge_context_with_va_v09/context_ledger.csv",
        "volatility": model / "runs/volatility_context_v10/context_ledger.csv",
        "daily_vwap": model / "runs/vwap_daily_utc_v05/daily_features.csv",
        "folds": model / "runs/edge_context_with_va_v09/folds.csv",
        "candles_config": model / "vwap_candles_config.json",
        "rolling_builder": model / "observe_rolling_vwap_bands.py",
        "equity_metrics": model / "compare_adx_quality.py",
    }
    provenance = [dict(role=k, path=str(p), sha256=sha(p)) for k, p in names.items()]
    original = pd.read_csv(names["original"])
    original = original.loc[original.symbol.isin(SYMBOLS)].copy()
    if original.duplicated(KEYS).any():
        raise ValueError("Duplicate original sessions")
    base = original.loc[original.profile_status.eq("valid") & original.status.isin(ACTIVE)].copy()
    context_cols = KEYS + [c for c in FEATURES.values() if c.startswith(("macro_", "value_"))] + ["feature_asof_utc", "macro_4h_asof_utc"]
    context = pd.read_csv(names["context"])
    context = context.loc[context.symbol.isin(SYMBOLS)]
    check = base.merge(context[KEYS + ["entry", "gross_r", "status"]], on=KEYS, validate="one_to_one", suffixes=("", "_context"))
    if len(check) != len(base) or not check.status.eq(check.status_context).all():
        raise ValueError("Context population changed")
    np.testing.assert_allclose(check.entry, check.entry_context)
    np.testing.assert_allclose(check.gross_r, check.gross_r_context)
    base = base.merge(context[context_cols], on=KEYS, how="left", validate="one_to_one")
    vol = pd.read_csv(names["volatility"])
    base = base.merge(vol[KEYS + ["poc_distance_ewma20", "ewma_snapshot_ms"]], on=KEYS, how="left", validate="one_to_one")
    daily = pd.read_csv(names["daily_vwap"])
    daily = daily.loc[daily.symbol.isin(SYMBOLS) & daily.feature_status.eq("valid")]
    daily = daily[["symbol", "session_date", "vwap_1300", "sigma", "morning_close"]]
    base = base.merge(daily, on=["symbol", "session_date"], how="left", validate="many_to_one")
    good = base.vwap_1300.notna()
    np.testing.assert_allclose(base.loc[good, "price_before_1300"], base.loc[good, "morning_close"], rtol=1e-10, atol=1e-8)
    base["vwap_daily_z"] = base.morning_bias * (base.price_before_1300 - base.vwap_1300) / base.sigma
    snapshots = []
    cfg = json.loads(names["candles_config"].read_text())
    cols = ["timestamp_ms", "open", "high", "low", "close", "volume"]
    for symbol in SYMBOLS:
        paths = [root / rel for rel in cfg[symbol]]
        provenance.extend(dict(role="24h_candles", symbol=symbol, path=str(p), sha256=sha(p)) for p in paths)
        candles = pd.concat([pd.read_parquet(p, columns=cols) for p in paths], ignore_index=True).sort_values("timestamp_ms").reset_index(drop=True)
        states = rolling_states(candles)
        wanted = base.loc[base.symbol.eq(symbol), ["session_date", "price_before_1300"]].drop_duplicates()
        if wanted.session_date.duplicated().any():
            raise ValueError("Different pre-entry reference prices across POCs")
        for row in wanted.itertuples():
            pivot = int(pd.Timestamp(row.session_date + " 13:00", tz=NY).timestamp() * 1000)
            i = states.timestamp_ms.searchsorted(pivot)
            record = dict(symbol=symbol, session_date=row.session_date, snapshot_ms=pivot,
                          vwap_24h=np.nan, sigma_24h=np.nan, available=False)
            if i < len(states) and states.timestamp_ms.iloc[i] == pivot and bool(states.valid.iloc[i]):
                s = states.iloc[i]
                np.testing.assert_allclose(s.last_close, row.price_before_1300, rtol=1e-10, atol=1e-8)
                record.update(vwap_24h=float(s.vwap), sigma_24h=float(s.sigma), available=True)
            snapshots.append(record)
        print(f"{symbol}: causal 24h VWAP snapshots completed", flush=True)
        del candles, states
    anchors = pd.DataFrame(snapshots)
    anchors.to_csv(out / "vwap_24h_entry_snapshots.csv", index=False)
    base = base.merge(anchors, on=["symbol", "session_date"], how="left", validate="many_to_one")
    base["vwap_24h_z"] = base.morning_bias * (base.price_before_1300 - base.vwap_24h) / base.sigma_24h
    base["distance"] = base.risk_price / base.entry
    modeled_exit = np.select([base.status.eq("tp_first"), base.status.eq("sl_first")], [base.tp, base.sl], default=base.trigger_trade_price)
    np.testing.assert_allclose(base.gross_r, base.side * (modeled_exit - base.entry) / base.risk_price, atol=1e-9)
    base["cost_r_per_bps"] = .0001 * (base.entry + modeled_exit) / base.risk_price
    numeric = ["entry", "risk_price", "gross_r", "distance", "cost_r_per_bps"]
    if not np.isfinite(base[numeric].to_numpy()).all() or base.distance.le(0).any():
        raise ValueError("Invalid trade geometry")
    pivot = pd.to_datetime(base.session_date + " 13:00").dt.tz_localize(NY).dt.tz_convert("UTC")
    for col in ["feature_asof_utc", "macro_4h_asof_utc"]:
        stamps = pd.to_datetime(base[col], utc=True, format="ISO8601", errors="coerce")
        if (stamps.notna() & stamps.gt(pivot)).any():
            raise ValueError(f"Future feature timestamp: {col}")
    vol_stamps = pd.to_datetime(base.ewma_snapshot_ms, unit="ms", utc=True)
    if (vol_stamps.notna() & vol_stamps.gt(pivot)).any():
        raise ValueError("Future volatility snapshot")
    base["entry_utc"] = pd.to_datetime(base.entry_timestamp_ny, utc=True, format="ISO8601")
    base["exit_utc"] = pd.to_datetime(base.exit_timestamp_ny, utc=True, format="ISO8601")
    if not base.entry_utc.ge(pivot).all() or not base.exit_utc.ge(base.entry_utc).all():
        raise ValueError("Invalid entry/exit timing")
    folds = pd.read_csv(names["folds"])
    folds = folds.loc[folds.symbol.isin(SYMBOLS)]
    base.to_csv(out / "feature_trade_ledger.csv", index=False)
    return base, folds, provenance


def predictions(base, folds, out):
    rows, calibration, coverage, category_rows = [], [], [], []
    for meta in folds.to_dict("records"):
        symbol, profile = meta["symbol"], meta["profile"]
        source = base.loc[base.symbol.eq(symbol) & base.profile.eq(profile)]
        train_mask = source.session_date.ge(meta["train_start"]) & source.session_date.lt(meta["test_start"])
        cutoff = pd.Timestamp(meta["test_start"], tz=NY).tz_convert("UTC")
        train = source.loc[train_mask & source.exit_utc.lt(cutoff)]
        test = source.loc[source.session_date.ge(meta["test_start"]) & source.session_date.lt(meta["test_end_exclusive"])].sort_values("session_date")
        if len(test) != int(meta["all_baseline_test_entries"]):
            raise ValueError("Original baseline test population changed")
        for target_cost in TARGET_COSTS:
            y = train.gross_r.to_numpy() - target_cost * train.cost_r_per_bps.to_numpy()
            fits = {f: fit_component(train[col], y) for f, col in FEATURES.items()}
            for f, artifact in fits.items():
                calibration.append(dict(**meta, target_cost_bps=target_cost, feature=f, **artifact))
                if len(test) and artifact["cutpoints"]:
                    cats = labels(test[FEATURES[f]], artifact["cutpoints"])
                    for g in artifact["groups"]:
                        q = test.loc[cats == g["category"]]
                        category_rows.append(dict(symbol=symbol, profile=profile, fold=meta["fold"], target_cost_bps=target_cost,
                            feature=f, category=g["category"], train_n=g["n"], train_rank=g.get("train_rank"),
                            train_supported=g["supported"], test_n=len(q), test_mean_gross_r=q.gross_r.mean(),
                            test_mean_net_r=(q.gross_r - target_cost*q.cost_r_per_bps).mean(),
                            test_gross_wr_pct=100*q.gross_r.gt(1e-12).mean()))
            if train.empty or test.empty:
                continue
            for model, features in MODELS.items():
                tr_score = np.mean([apply_component(train[FEATURES[f]], fits[f]) for f in features], axis=0)
                te_score = np.mean([apply_component(test[FEATURES[f]], fits[f]) for f in features], axis=0)
                multiplier = 1 + te_score
                tr_risk = effective_risk(1 + tr_score, train.distance)
                fixed = match_fixed_risk(train.distance, tr_risk.mean())
                np.testing.assert_allclose((1 + tr_score).mean(), 1., atol=1e-12)
                if not np.all((multiplier >= .5 - 1e-12) & (multiplier <= 1.5 + 1e-12)):
                    raise ValueError("Sizing exceeds frozen bounds")
                available = test[[FEATURES[f] for f in features]].notna()
                coverage.append(dict(symbol=symbol, profile=profile, fold=meta["fold"], model=model,
                    target_cost_bps=target_cost, train_trades=len(train), test_trades=len(test),
                    test_start=meta["test_start"], test_end_exclusive=meta["test_end_exclusive"],
                    calibrated_components=sum(fits[f]["status"] == "calibrated" for f in features),
                    feature_available_all=int(available.all(axis=1).sum()), feature_available_any=int(available.any(axis=1).sum()),
                    neutral_entries=int(np.isclose(multiplier, 1, atol=1e-12, rtol=0).sum()),
                    purged_train=int((train_mask & ~source.exit_utc.lt(cutoff)).sum()),
                    training_effective_risk=float(tr_risk.mean()), fixed_matched_risk=float(fixed)))
                q = test[KEYS + ["gross_r", "status", "distance", "cost_r_per_bps", "entry", "risk_price", "side"]].copy()
                q["fold"], q["model"], q["target_cost_bps"] = meta["fold"], model, target_cost
                q["multiplier"] = multiplier
                q["base_risk"] = effective_risk(np.ones(len(q)), q.distance)
                q["sized_risk"] = effective_risk(multiplier, q.distance)
                q["matched_risk"] = np.minimum(fixed, CAP*q.distance)
                q["matched_requested_multiplier"] = fixed/BASE_RISK
                q["cohort"] = np.select([multiplier <= .9, multiplier >= 1.1], ["lower", "higher"], default="middle")
                rows.append(q)
        print(f"{symbol}/{profile} fold {meta['fold']}: sizing frozen and scored", flush=True)
    result = pd.concat(rows, ignore_index=True)
    if result.duplicated(KEYS + ["model", "target_cost_bps"]).any():
        raise ValueError("Overlapping test predictions")
    result.to_csv(out / "sizing_predictions.csv", index=False)
    pd.DataFrame(coverage).to_csv(out / "coverage.csv", index=False)
    pd.DataFrame(category_rows).to_csv(out / "ranked_category_test.csv", index=False)
    save_json(out / "calibration.json", calibration)
    return result


def metrics(g, risk, cost, multiplier=None):
    from compare_adx_quality import equity
    r = g.gross_r.to_numpy()
    risk = np.asarray(risk)
    net = r - cost * g.cost_r_per_bps.to_numpy()
    units = risk / BASE_RISK
    contribution = units * net
    positives, negatives = contribution[contribution > 1e-12], contribution[contribution < -1e-12]
    equity_stats = equity(risk * net)
    return dict(trades=len(g), original_gross_wr_pct=float(100*np.mean(r > 1e-12)),
        original_net_wr_pct=float(100*np.mean(net > 1e-12)), original_mean_gross_r=float(r.mean()),
        original_mean_net_r=float(net.mean()), mean_effective_risk_pct=float(100*risk.mean()),
        median_effective_risk_pct=float(100*np.median(risk)), mean_notional_x=float(np.mean(risk/g.distance)),
        cap_pct=float(100*np.mean(np.isclose(risk/g.distance, CAP))),
        capital_gross_base_r=float(np.sum(units*r)), capital_cost_base_r=float(np.sum(units*(r-net))),
        capital_net_base_r=float(contribution.sum()), mean_capital_net_base_r=float(contribution.mean()),
        risk_weighted_mean_net_r=float(np.sum(risk*net)/np.sum(risk)),
        median_capital_net_base_r=float(np.median(contribution)), std_capital_net_base_r=float(np.std(contribution, ddof=1)),
        uncapped_weighted_gross_r=float(np.sum(multiplier*r)) if multiplier is not None else np.nan,
        profit_factor=float(positives.sum()/-negatives.sum()) if len(negatives) else np.nan,
        break_even_cost_bps_each_side=float(np.sum(units*r)/np.sum(units*g.cost_r_per_bps)),
        **equity_stats)


def uncertainty(g, schedule, rng, repeats):
    """Paired daily-block contrasts and within-fold shuffled weight diagnostics."""
    n = len(g)
    sr = g.sized_risk.to_numpy()/BASE_RISK
    mr = g.matched_risk.to_numpy()/BASE_RISK
    r, fee = g.gross_r.to_numpy(), g.cost_r_per_bps.to_numpy()
    risk_equalizer = float(sr.sum()/mr.sum())
    boot = np.zeros((repeats, 9))
    shuffled_gross, shuffled_fee = np.zeros(repeats), np.zeros(repeats)
    for fold, part in g.groupby("fold"):
        indexes = g.index.get_indexer(part.index)
        meta = schedule.loc[schedule.fold.eq(fold)].iloc[0]
        dates = pd.date_range(meta.test_start, pd.Timestamp(meta.test_end_exclusive)-pd.Timedelta(days=1))
        components = pd.DataFrame(dict(delta_gross=(sr[indexes]-mr[indexes])*r[indexes],
            delta_fee=(sr[indexes]-mr[indexes])*fee[indexes],
            sized_gross=sr[indexes]*r[indexes], sized_fee=sr[indexes]*fee[indexes],
            matched_gross=mr[indexes]*r[indexes], matched_fee=mr[indexes]*fee[indexes],
            sized_risk=sr[indexes], matched_risk=mr[indexes], count=np.ones(len(part))), index=pd.to_datetime(part.session_date))
        daily = components.reindex(dates, fill_value=0).to_numpy()
        count = len(dates)
        starts = rng.integers(count, size=(repeats, int(np.ceil(count/7)), 1))
        ix = ((starts + np.arange(7)) % count).reshape(repeats, -1)[:, :count]
        boot += daily[ix].sum(axis=1)
        order = rng.random((repeats, len(part))).argsort(axis=1)
        requested = part.multiplier.to_numpy()[order]
        weights = effective_risk(requested, part.distance.to_numpy()[None, :])/BASE_RISK
        shuffled_gross += (weights*r[indexes]).sum(axis=1)
        shuffled_fee += (weights*fee[indexes]).sum(axis=1)
    rows = []
    for cost in COSTS:
        valid = (boot[:, 8] > 0) & (boot[:, 7] > 0)
        b = boot[valid]
        delta = (b[:, 0]-cost*b[:, 1])/b[:, 8]
        normalized = (b[:, 2]-cost*b[:, 3]-(b[:, 6]/b[:, 7])*(b[:, 4]-cost*b[:, 5]))/b[:, 8]
        observed = float(np.sum(sr*(r-cost*fee)))
        draws = shuffled_gross-cost*shuffled_fee
        rows.append(dict(cost_bps=cost, bootstrap_delta_per_opportunity_ci025=float(np.quantile(delta, .025)),
            bootstrap_delta_per_opportunity_ci975=float(np.quantile(delta, .975)),
            retrospective_risk_equalizer=risk_equalizer,
            retrospective_equal_risk_delta_per_opportunity=float(np.sum((sr-risk_equalizer*mr)*(r-cost*fee))/n),
            bootstrap_equal_risk_delta_per_opportunity_ci025=float(np.quantile(normalized, .025)),
            bootstrap_equal_risk_delta_per_opportunity_ci975=float(np.quantile(normalized, .975)),
            random_weight_upper_tail_fraction=float((1 + np.sum(draws >= observed - 1e-10*max(1., abs(observed))))/(repeats+1)),
            random_capital_net_base_r_p025=float(np.quantile(draws, .025)),
            random_capital_net_base_r_p975=float(np.quantile(draws, .975)), repeats=repeats))
    return rows


def summarize(pred, folds, out, repeats):
    summaries, per_fold, cohorts, robustness = [], [], [], []
    group_keys = ["symbol", "profile", "model", "target_cost_bps"]
    for key, g in pred.groupby(group_keys, sort=True):
        tag = dict(zip(group_keys, key))
        g = g.sort_values("session_date")
        rng = np.random.default_rng(int(hashlib.sha256(json.dumps(key).encode()).hexdigest()[:8], 16))
        schedule = folds.loc[folds.symbol.eq(tag["symbol"]) & folds.profile.eq(tag["profile"])]
        robustness.extend(dict(**tag, **r) for r in uncertainty(g, schedule, rng, repeats))
        for cost in COSTS:
            for policy, risk_col in [("fixed_base", "base_risk"), ("scored_sizing", "sized_risk"), ("fixed_train_matched", "matched_risk")]:
                m = (g.multiplier.to_numpy() if policy == "scored_sizing" else
                     g.matched_requested_multiplier.to_numpy() if policy == "fixed_train_matched" else np.ones(len(g)))
                summaries.append(dict(**tag, cost_bps=cost, policy=policy, **metrics(g, g[risk_col], cost, m)))
                for fold, part in g.groupby("fold"):
                    per_fold.append(dict(**tag, cost_bps=cost, policy=policy, fold=fold,
                        **metrics(part, part[risk_col], cost, m[g.index.get_indexer(part.index)])))
            for cohort, part in g.groupby("cohort"):
                cohorts.append(dict(**tag, cost_bps=cost, cohort=cohort, mean_multiplier=float(part.multiplier.mean()),
                    **metrics(part, part.sized_risk, cost, part.multiplier)))
        print(f"{tag['symbol']}/{tag['profile']}/{tag['model']}/train cost {tag['target_cost_bps']:g}: diagnostics complete", flush=True)
    result, fold, cohort, robust = [pd.DataFrame(x) for x in [summaries, per_fold, cohorts, robustness]]
    stability = []
    for key, g in fold.groupby(group_keys + ["cost_bps"], sort=True):
        table = g.pivot(index="fold", columns="policy", values="capital_net_base_r").sort_index()
        delta = table.scored_sizing - table.fixed_train_matched
        half = len(delta)//2
        stability.append(dict(**dict(zip(group_keys + ["cost_bps"], key)), folds=len(delta),
            improved_folds=int(delta.gt(1e-12).sum()), worsened_folds=int(delta.lt(-1e-12).sum()),
            positive_sized_folds=int(table.scored_sizing.gt(0).sum()),
            delta_capital_net_base_r=float(delta.sum()), early_delta=float(delta.iloc[:half].sum()),
            late_delta=float(delta.iloc[half:].sum()), leave_one_fold_delta_min=float((delta.sum()-delta).min()),
            leave_one_fold_delta_max=float((delta.sum()-delta).max())))
    for name, frame in [("summary", result), ("fold_metrics", fold), ("sizing_cohorts", cohort), ("robustness", robust), ("stability", pd.DataFrame(stability))]:
        frame.to_csv(out / f"{name}.csv", index=False)
    return result, pd.DataFrame(stability), robust, cohort


def report(out, summary, stability, robust, cohorts):
    lines = ["# Edge-conditioned sizing v12", "", "BTC/XRP only. Previously inspected historical development evidence. No live rule or fresh holdout.",
        "All original entries are retained. Weights 0.5–1.5 learned in rolling 8-month train / 2-month test / 2-month step. Missing/unsupported features are neutral. Original R/WR is unchanged by sizing.",
        "One capital base-R unit = 0.5% pre-trade equity. Effective risk is capped at entry notional 5x equity. Account returns compound closed-trade returns; independent accounts, no funding, actual fills, or intratrade DD.",
        "Main table: target learned with hypothetical 2 bps/side cost and evaluated at the same cost. Zero-cost calibration and every cost scenario remain in summary.csv. None is selected as a winner.",
        "At entry 13:00, rolling 24h and frozen 24h share the same snapshot. The 24h feature uses only pre-entry candle quality; no future 17:00 anchor enters sizing.", "",
        "## Primary target: today's 09:30–13:00 POC", "",
        "Base control = fixed risk matched using TRAIN only. Its TEST risk can differ. All returns are scenario calculations, not executable forecasts.", "",
        "| Asset | Sizing feature | Entries | Gross WR | Capital net base-R: fixed / sized | Account return %: fixed / sized | Closed DD %: fixed / sized | Folds improved |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    main = summary.loc[summary.profile.eq("ny_0930_1300") & summary.target_cost_bps.eq(2) & summary.cost_bps.eq(2)]
    for (symbol, model), g in main.groupby(["symbol", "model"]):
        b, s = [g.loc[g.policy.eq(p)].iloc[0] for p in ["fixed_train_matched", "scored_sizing"]]
        st = stability.loc[stability.symbol.eq(symbol) & stability.profile.eq("ny_0930_1300") & stability.model.eq(model) & stability.target_cost_bps.eq(2) & stability.cost_bps.eq(2)].iloc[0]
        lines.append(f"| {symbol} | {model} | {int(s.trades)} | {s.original_gross_wr_pct:.1f}% | {b.capital_net_base_r:+.2f} / {s.capital_net_base_r:+.2f} | {b.compounded_return_pct:+.2f} / {s.compounded_return_pct:+.2f} | {b.max_drawdown_pct:.2f} / {s.max_drawdown_pct:.2f} | {int(st.improved_folds)}/{int(st.folds)} |")
    lines += ["", "## Gross calibration and gross evaluation", "",
        "Same primary POC, separate predefined diagnostic; this does not establish net profitability.", "",
        "| Asset | Sizing feature | Capital gross base-R: fixed / sized | Account return %: fixed / sized | Mean TEST risk %: fixed / sized |", "|---|---|---:|---:|---:|"]
    gross = summary.loc[summary.profile.eq("ny_0930_1300") & summary.target_cost_bps.eq(0) & summary.cost_bps.eq(0)]
    for (symbol, model), g in gross.groupby(["symbol", "model"]):
        b, s = [g.loc[g.policy.eq(p)].iloc[0] for p in ["fixed_train_matched", "scored_sizing"]]
        lines.append(f"| {symbol} | {model} | {b.capital_net_base_r:+.2f} / {s.capital_net_base_r:+.2f} | {b.compounded_return_pct:+.2f} / {s.compounded_return_pct:+.2f} | {b.mean_effective_risk_pct:.3f} / {s.mean_effective_risk_pct:.3f} |")
    lines += ["", "## Interpretation and complete evidence", "",
        "Calibration predicts category expectancy, not a calibrated win probability. Higher risk must earn a useful incremental net contribution; increasing weighted return or selecting a favorable row from many trials is insufficient.",
        "summary.csv includes fixed-base, train-matched fixed, and scored policies for every candidate/profile/calibration/cost. Mean/median/std capital contribution, PF, notional, cap incidence, risk, cost drag and break-even proportional cost are included.",
        "stability.csv reports fold signs, early/late deltas and leave-one-fold-out ranges. robustness.csv reports paired seven-calendar-day within-fold bootstrap intervals and random assignment of the same requested weights within each fold. These are descriptive and uncorrected for multiple testing. The equal-TEST-risk contrast is retrospective, never a sizing policy.",
        "sizing_cohorts.csv contains lower (<=0.9), middle, higher (>=1.1) multiplier groups, original gross/net WR and R. ranked_category_test.csv preserves train-ranked category outcomes including unsupported groups; coverage.csv preserves neutral fallbacks and feature availability. Check ordered outcomes and category counts rather than assuming more indicators create more evidence.",
        "The three other POC targets are supplementary, separate comparisons. Do not pool correlated profiles/assets as independent evidence or a portfolio. Risk caps can change results relative to old uncapped R totals; uncapped_weighted_gross_r is preserved separately.",
        "Design and full calibration are in EDGE_SIZING_DESIGN.md and calibration.json. A 2 bps per side scenario is not an exchange fee assertion. Narrow stops can make costs dominate; high cost sensitivity does not establish direction prediction. No auto-promotion follows this experiment.", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--repeats", type=int, default=2000)
    args = p.parse_args()
    if args.repeats < 100:
        p.error("At least 100 diagnostic draws required")
    if args.output.exists():
        raise ValueError("Preserve prior results; choose a new output directory")
    args.output.mkdir(parents=True)
    design = Path(__file__).with_name("EDGE_SIZING_DESIGN.md")
    shutil.copy2(design, args.output / design.name)
    snapshot = dict(status="running", recorded="2026-10-22", symbols=SYMBOLS, features=FEATURES, models=MODELS,
        base_risk=BASE_RISK, max_notional_x=CAP, calibration_cost_bps=TARGET_COSTS, evaluation_cost_bps=COSTS,
        minimum_train_features=90, minimum_category_trades=30, shrinkage_pseudo_trades=30,
        sensitivity_floor_r=.25, requested_multiplier_bounds=[.5, 1.5],
        train_months=8, test_months=2, step_months=2, repeats=args.repeats, prior_inspection=True,
        code_sha256=sha(__file__), design_sha256=sha(design))
    save_json(args.output / "config_snapshot.json", snapshot)
    base, folds, provenance = load_inputs(args.root.resolve(), args.output)
    pred = predictions(base, folds, args.output)
    summary, stability, robust, cohorts = summarize(pred, folds, args.output, args.repeats)
    report(args.output, summary, stability, robust, cohorts)
    # All policies retain identical original entries and signs; positive sizing cannot change WR.
    for _, g in summary.groupby(["symbol", "profile", "model", "target_cost_bps", "cost_bps"]):
        assert g.trades.nunique() == g.original_gross_wr_pct.nunique() == g.original_net_wr_pct.nunique() == 1
    assert pred.sized_risk.gt(0).all() and pred.multiplier.between(.5-1e-12, 1.5+1e-12).all()
    save_json(args.output / "audit.json", dict(status="passed", rows=len(pred), summary_rows=len(summary),
        checks=["original trade/outcome identity", "nonoverlapping test dates", "pre-entry features only",
                "positive bounded multipliers", "TRAIN-only effective-risk matching", "original WR invariant across policies"],
        sources=provenance))
    snapshot["status"] = "complete"
    save_json(args.output / "config_snapshot.json", snapshot)
    print(f"Complete: {args.output}", flush=True)


if __name__ == "__main__":
    main()
