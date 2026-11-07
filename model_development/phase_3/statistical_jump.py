"""Daily UTC statistical-jump diagnosis; no trading decisions or switch forecasts.

Numerical solver reused from model_development/models/statistical_jump_model/model.py. The research
clock comes from the explicitly selected executor checkout. All new evaluation
flow lives here; legacy feature preparation/backtests are deliberately not used.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

DAY = 86_400_000
VERSION = "daily_utc_jump_diagnostic_v1.3.0"
CHART = "calendar_template_b_v1"
FEATURES = ["downside_deviation_10", "sortino_20", "sortino_60"]
DEFAULT_ROOT = Path("C:/Users/adjop/OneDrive/Documents/golden-goose-project")
DEFAULT_CORE = Path("C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor")


@dataclass(frozen=True)
class Config:
    jump_penalty: float = 50.0
    train_days: int = 3000
    inference_days: int = 3000
    refit_months: int = 6
    warmup_days: int = 300
    restarts: int = 10
    max_iter: int = 100
    seed: int = 42
    mode: str = "causal"

    def validate(self):
        if not np.isfinite(self.jump_penalty) or self.jump_penalty < 0:
            raise ValueError("jump_penalty must be finite and nonnegative")
        for name in ("train_days", "inference_days", "refit_months", "warmup_days", "restarts", "max_iter"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.train_days < 2 or self.warmup_days < 60:
            raise ValueError("Require train_days >= 2 and warmup_days >= longest half-life (60)")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**32 - self.restarts:
            raise ValueError("Invalid deterministic seed/restarts")
        if self.mode not in ("causal", "retrospective"):
            raise ValueError("Unknown inference mode")


def load_dependencies(project_root=DEFAULT_ROOT, shared_core=DEFAULT_CORE):
    """Load the chosen files explicitly; avoid another checkout's package shadowing."""
    sys.path.append(str(Path(project_root).resolve()))
    from model_development.research_dependencies import bind_clock, clock_source
    paths = {
        "solver": Path(project_root) / "model_development/models/statistical_jump_model/model.py",
        "clock": clock_source(shared_core),
        "cache": Path(project_root) / "model_development/observation_cache.py",
        "observe": Path(project_root) / "model_development/phase_1/observe.py",
    }
    modules = {}
    for name, path in paths.items():
        if name == "clock":
            modules[name] = bind_clock(shared_core)
            continue
        spec = importlib.util.spec_from_file_location(f"_postmod_jump_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules, paths


def integer_ms(series, name):
    if series.dtype.kind not in "iu" or series.isna().any() or (series < 0).any():
        raise ValueError(f"{name} must be nonnegative integer UTC milliseconds")
    return series.astype("int64")


def utc_boundary(value):
    result = pd.Timestamp(value)
    if result.tz is None:
        result = result.tz_localize("UTC")
    result = result.tz_convert("UTC")
    if result != result.normalize() or result.value < 0:
        raise ValueError("Bounds must be UTC calendar-day midnights")
    return int(result.timestamp() * 1000)


def prepare_daily(raw, end_ms, clock, base_timeframe="1d", instrument=None):
    required = {"timestamp_ms", "open", "high", "low", "close", "volume"}
    if not required.issubset(raw):
        raise ValueError(f"Missing measured candle columns: {sorted(required - set(raw))}")
    raw = raw.copy()
    raw["timestamp_ms"] = integer_ms(raw.timestamp_ms, "timestamp_ms")
    # Data beyond the declared bound never enter features, fitting or reports.
    raw = raw.loc[raw.timestamp_ms < end_ms].copy()
    if raw.empty:
        raise ValueError("No history before end")
    for name in ("instrument", "symbol"):
        if name in raw and (raw[name].isna().any() or raw[name].astype(str).nunique() != 1
                            or (instrument is not None and str(raw[name].iloc[0]).upper() != instrument.upper())):
            raise ValueError(f"{name} mixes or contradicts the declared instrument")
    if (raw[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Prices must be positive")
    if "risk_free_return" in raw and base_timeframe != "1d":
        raise ValueError("Daily risk-free returns require daily input (do not sum/duplicate intraday)")
    columns = [*required]
    columns += [c for c in ("available_at_ms", "is_closed") if c in raw]
    context = clock.CandleContext(raw[columns], as_of_ms=end_ms, base_timeframe=base_timeframe)
    daily = context.candles("1d")
    if daily.empty or int(daily.end_ms.iloc[-1]) != end_ms:
        raise ValueError("Missing complete/available daily end-boundary coverage")
    daily["available_at_ms"] = daily.available_at_ms.cummax().astype("int64")
    if "risk_free_return" in raw:
        rf = raw.set_index(pd.to_datetime(raw.timestamp_ms, unit="ms", utc=True)).risk_free_return
        daily["risk_free_return"] = rf.reindex(daily.index)
    else:
        daily["risk_free_return"] = 0.0
    if (not np.isfinite(daily.risk_free_return).all() or (daily.risk_free_return <= -1).any()):
        raise ValueError("Invalid same-period decimal risk-free returns")
    daily.attrs["risk_free_assumption"] = "measured daily decimal input" if "risk_free_return" in raw else "explicit zero cash return"
    return daily


def daily_from_cache(folder, end_ms, instrument, dependencies, minutes=None):
    """Reuse measured sparse candles only with the shared raw-validation manifest."""
    folder = Path(folder)
    cache = dependencies["cache"]
    data_path, meta_path = folder/"candles_1m.parquet", folder/"cache.json"
    manifest_bytes = meta_path.read_bytes()
    saved = json.loads(manifest_bytes)
    identity = saved.get("identity", {})
    version, builder = identity.get("version"), identity.get("builder")
    current_builder = digest(cache.__file__)
    # Follow the global builder's explicit legacy allowlist without weakening continuity.
    legacy = version == 1 and (builder in getattr(cache, "LEGACY_BUILDERS", set())
        or (not hasattr(cache, "LEGACY_BUILDERS") and builder == current_builder))
    current = version == 2 and builder in (getattr(cache, "LEGACY_V2_BUILDERS", set()) | {current_builder})
    if not (legacy or current):
        raise ValueError("Unsupported/unvalidated candle cache or different builder; rebuild from raw")
    legacy_validation = "strict ordered contiguous aggregate IDs, positive finite price/qty"
    current_validation = {
        "ordered unique aggregate IDs; positive finite price/qty; explicit forward-ID-gap policy",
        "chronological timestamps; no adjacent duplicate IDs; positive finite price/qty; explicit source-ID-discontinuity policy",
    }
    validation_ok = (saved.get("validation") == legacy_validation if legacy
        else saved.get("validation") in current_validation)
    contiguous = (saved.get("source_continuity", "contiguous_ids_observed" if legacy else None)
                  == "contiguous_ids_observed" and not saved.get("discontinuities"))
    if (not validation_ok or not contiguous or identity.get("id_policy", "error") not in ("error", "record")
            or (legacy and identity.get("id_policy", "error") != "error")):
        raise ValueError("Cache source continuity is unverified; sparse-minute daily aggregation is blocked")
    identities = identity.get("sources", [])
    if not identities:
        raise ValueError("Cache has no raw-source provenance")
    sources = [Path(s["path"]) for s in identities]
    dependencies["observe"].infer_instrument(sources, instrument)
    if cache.source_identity(sources) != identities:
        raise ValueError("Raw sources changed since cache validation")
    if cache.digest(data_path) != saved.get("sha256"):
        raise ValueError("Candle cache checksum mismatch")
    low, high = saved.get("source_start_ms"), saved.get("source_end_ms")
    if type(low) is not int or type(high) is not int or not 0 <= low < high:
        raise ValueError("Invalid raw coverage bounds in cache")
    first_complete = ((low + DAY-1)//DAY)*DAY
    last_complete = (high//DAY)*DAY
    if end_ms > last_complete or end_ms <= first_complete:
        raise ValueError("Raw stream does not cover the requested complete UTC days")
    if minutes is None:
        minutes = pd.read_parquet(data_path)
    stamps = integer_ms(minutes.timestamp_ms, "cached timestamp_ms")
    if (stamps.empty or not stamps.is_monotonic_increasing or stamps.duplicated().any()
            or (stamps % 60000).any() or int(stamps.iloc[0]) != low//60000*60000
            or int(stamps.iloc[-1]) != high//60000*60000):
        raise ValueError("Cached timestamps disagree with validated source coverage")
    bars = cache.aggregate_candles(minutes, "1d", first_complete, end_ms)
    bars["timestamp_ms"] = bars.index.as_unit("ms").asi8
    # Sparse minute absence is backed by raw continuity; gate the measured daily bars.
    daily = prepare_daily(bars, end_ms, dependencies["clock"], "1d", instrument)
    receipt = dict(path=str(folder.resolve()), reused=True, manifest=saved,
        producer_version=version, builder_compatibility=("allowlisted_legacy_v1" if legacy
            else "current_v2" if builder == current_builder else "allowlisted_legacy_v2"),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        first_complete_day_ms=first_complete, requested_end_ms=end_ms,
        excluded_initial_partial_ms=[low, first_complete] if low < first_complete else None,
        source_last_complete_end_ms=last_complete, sparse_minutes="validated no-print intervals; no invented bars",
        daily_availability="historical close-time assumption; raw cache has no receipt timestamps")
    files = {"market_cache": data_path, "market_cache_manifest": meta_path}
    return daily, receipt, files


def load_market(args, end_ms, dependencies):
    if args.market is not None:
        raw = pd.read_parquet(args.market)
        daily = prepare_daily(raw, end_ms, dependencies["clock"], args.base_timeframe, args.instrument)
        return daily, None, {"market": args.market}
    if args.input:
        paths = [p.resolve() for p in args.input]
        dependencies["observe"].infer_instrument(paths, args.instrument)
        directory = args.cache_dir or args.project_root/"tmp/cache/observe"/args.instrument
        reused = None
        minutes = None
    else:
        directory = args.candle_cache
        reused = True
        minutes = None
    directory = directory.resolve()
    output = args.output.resolve()
    if directory.is_relative_to(output) or output.is_relative_to(directory):
        raise ValueError("Shared candle cache and run output must be separate directories")
    if args.input:
        # The builder's key is unchanged, so existing validated caches are reusable.
        minutes, built = dependencies["cache"].candle_cache(paths, directory)
        directory, reused = Path(built["path"]), built["reused"]
    daily, receipt, files = daily_from_cache(directory, end_ms, args.instrument, dependencies, minutes)
    receipt["reused"] = reused
    return daily, receipt, files


def features(daily, warmup_days):
    """Prefix-causal adjusted EWM; no invented first return or denominator floor."""
    out = daily.copy()
    out["market_return"] = daily.close.pct_change(fill_method=None)
    out["excess_return"] = out.market_return - daily.risk_free_return
    e = out.excess_return
    downside = e.clip(upper=0).pow(2)
    for h in (10, 20, 60):
        dd = downside.ewm(halflife=h, adjust=True, min_periods=warmup_days).mean().pow(0.5)
        if h == 10:
            out[FEATURES[0]] = dd
        else:
            mean = e.ewm(halflife=h, adjust=True, min_periods=warmup_days).mean()
            out[f"sortino_{h}"] = mean.div(dd.where(dd > 0))
    out[FEATURES] = out[FEATURES].replace([np.inf, -np.inf], np.nan)
    return out


def fit_checked(frame, config, solver):
    x = frame[FEATURES].to_numpy(dtype=float)
    if len(x) != config.train_days or not np.isfinite(x).all():
        raise ValueError("Incomplete fitting history or feature warmup")
    if (x.std(axis=0) == 0).any():
        raise ValueError("Degenerate constant fitting feature")
    model = solver.StatisticalJumpModel(config.jump_penalty, restarts=config.restarts,
        max_iter=config.max_iter, random_state=config.seed).fit(x, frame.excess_return.to_numpy())
    counts = np.bincount(model.states_, minlength=2)
    if not counts.all() or np.allclose(model.centers_[0], model.centers_[1], rtol=0, atol=1e-10):
        raise ValueError("Collapsed/indistinguishable fitted regimes; choose a declared sensitivity run")
    z = model._transform(x)
    means = np.array([z[model.states_ == k].mean(axis=0) for k in range(2)])
    if not np.allclose(means, model.centers_, rtol=1e-8, atol=1e-8):
        raise ValueError("Fitting did not converge within max_iter")
    sums = np.bincount(model.states_, weights=frame.excess_return, minlength=2)
    if np.isclose(sums[0], sums[1], rtol=0, atol=1e-12):
        raise ValueError("Ambiguous bull/bear training-return ranking")
    return model, {
        "objective": float(model.objective_), "bull_state": int(model.bull_state_),
        "counts": counts.tolist(), "training_cumulative_excess_returns": sums.tolist(),
        "mean": model.mean_.tolist(), "scale": model.scale_.tolist(),
        "centers_standardized": model.centers_.tolist(),
    }


def classify(daily, config, start_ms, end_ms, dependencies):
    config.validate()
    if start_ms >= end_ms or start_ms % DAY or end_ms % DAY:
        raise ValueError("Invalid start-inclusive/end-exclusive daily bounds")
    data = features(daily, config.warmup_days)
    out = data.copy()
    out["state"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["regime"] = "unknown"
    out["fit_id"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["label_available_at_ms"] = out.available_at_ms
    out["status"] = "warmup_or_insufficient_training"
    refits = []
    solver, clock = dependencies["solver"], dependencies["clock"]
    ready = np.isfinite(data[FEATURES].to_numpy()).all(axis=1)
    wanted = np.flatnonzero((out.end_ms >= start_ms) & (out.end_ms < end_ms))
    if not len(wanted):
        raise ValueError("No daily regime boundaries in requested window")

    def gate(index):
        # The prepared row is checked at its actual dependency availability.
        at = int(out.available_at_ms.iloc[index])
        context = clock.CandleContext(daily.iloc[:index+1], as_of_ms=at, base_timeframe="1d")
        spec = clock.IndicatorSpec("1d", lambda bars: data[FEATURES].reindex(bars.index))
        return context.evaluate(lambda snap: True, evaluation_timeframe="1d", indicators={"jump": spec})

    if config.mode == "retrospective":
        valid = np.flatnonzero(ready)
        if len(valid) < config.train_days or not np.all(ready[valid[-config.train_days]:]):
            raise ValueError("Insufficient contiguous retrospective fit history")
        fit = data.iloc[valid[-config.train_days:]]
        model, receipt = fit_checked(fit, config, solver)
        indices = np.flatnonzero(ready & (out.end_ms >= start_ms) & (out.end_ms < end_ms))
        # Decode permitted history, not just the report slice.
        first = valid[0]
        if not ready[first:].all():
            raise ValueError("Unavailable feature after warmup")
        states = model.predict_states(data.iloc[first:][FEATURES].to_numpy())
        for i in indices:
            out.iloc[i, out.columns.get_loc("state")] = int(states[i-first])
        out.loc[out.state.notna(), "label_available_at_ms"] = end_ms
        out.loc[out.state.notna(), "status"] = "retrospective_hindsight"
        out.loc[out.state.notna(), "fit_id"] = 0
        receipt.update(fit_id=0, fit_start_ms=int(fit.end_ms.iloc[0]), fit_end_ms=int(fit.end_ms.iloc[-1]))
        refits.append(receipt)
        out.loc[out.state.notna(), "regime"] = np.where(out.loc[out.state.notna(), "state"].astype(int) == model.bull_state_, "bull", "bear")
    else:
        model = None
        next_refit = None
        for i in range(len(out)):
            boundary = int(out.end_ms.iloc[i])
            if boundary >= end_ms:
                break
            # Fit on strictly preceding daily rows; do not fit on the row being labelled.
            lo = i - config.train_days
            can_fit = lo >= 0 and ready[lo:i].all()
            if model is None and not can_fit:
                continue
            if model is None or boundary >= next_refit:
                if not can_fit:
                    raise ValueError("Missing fitting coverage at scheduled refit")
                model, receipt = fit_checked(data.iloc[lo:i], config, solver)
                next_refit = int((pd.to_datetime(boundary, unit="ms", utc=True)
                                 + pd.DateOffset(months=config.refit_months)).timestamp()*1000)
                receipt.update(fit_id=len(refits), fit_start_ms=int(data.end_ms.iloc[lo]),
                    fit_end_ms=int(data.end_ms.iloc[i-1]), effective_from_ms=boundary)
                refits.append(receipt)
            if not ready[i]:
                raise ValueError("Feature unavailable after initial training")
            lookback = max(0, i-config.inference_days+1)
            if i-lookback+1 != config.inference_days or not ready[lookback:i+1].all():
                continue
            snapshot = gate(i)
            state = solver._last_state(solver._losses(model._transform(data.iloc[lookback:i+1][FEATURES].to_numpy()),
                                                   model.centers_), config.jump_penalty)
            out.iloc[i, out.columns.get_loc("state")] = state
            out.iloc[i, out.columns.get_loc("regime")] = "bull" if state == model.bull_state_ else "bear"
            out.iloc[i, out.columns.get_loc("fit_id")] = len(refits)-1
            out.iloc[i, out.columns.get_loc("status")] = "online_inferred"
            out.iloc[i, out.columns.get_loc("label_available_at_ms")] = snapshot["feature_available_at_ms"]
    result = out.loc[(out.end_ms >= start_ms) & (out.end_ms < end_ms)].reset_index(drop=True)
    result["mode"] = config.mode
    if result.state.isna().all():
        raise ValueError("No usable regimes; supply prehistory or explicitly change fitting/warmup settings")
    return result, refits


def attribute_trades(master, regimes, instrument, start_ms, end_ms, configuration=None, submodel=None):
    required = {"record_type", "trade_id", "decision_timestamp_ms", "entry_timestamp_ms", "exit_timestamp_ms", "gross_R", "net_R"}
    if not required.issubset(master):
        raise ValueError(f"Missing trade fields: {sorted(required-set(master))}")
    trades = master.loc[master.record_type == "closed_trade"].copy()
    if "instrument" in trades:
        trades = trades.loc[trades.instrument == instrument]
    for column, value in (("configuration_id", configuration), ("submodel_id", submodel)):
        if value is not None:
            if column not in trades:
                raise ValueError(f"Missing selector field {column}")
            trades = trades.loc[trades[column].astype(str) == value]
    for col in ("decision_timestamp_ms", "entry_timestamp_ms", "exit_timestamp_ms"):
        # Mixed position_block rows may make nullable timestamps float after Parquet loading.
        values = pd.to_numeric(trades[col], errors="raise")
        if not np.isfinite(values).all() or (values < 0).any() or (values % 1).any():
            raise ValueError(f"Invalid closed-trade {col}")
        trades[col] = values.astype("int64")
    trades = trades.loc[(trades.decision_timestamp_ms >= start_ms) & (trades.decision_timestamp_ms < end_ms)
                        & (trades.exit_timestamp_ms < end_ms)].copy()
    if trades.trade_id.isna().any() or trades.trade_id.duplicated().any():
        raise ValueError("Missing/duplicate closed trade IDs")
    if ((trades.decision_timestamp_ms > trades.entry_timestamp_ms).any()
            or (trades.entry_timestamp_ms > trades.exit_timestamp_ms).any()):
        raise ValueError("Invalid decision/entry/exit order")
    if not np.isfinite(trades[["gross_R", "net_R"]].to_numpy(dtype=float)).all():
        raise ValueError("Nonfinite closed outcomes")
    for col in ("run_id", "model_id", "configuration_id", "submodel_id"):
        if col in trades and trades[col].nunique(dropna=False) > 1:
            raise ValueError(f"Select one {col}; independent experiments cannot be pooled")
    if (trades.net_R > trades.gross_R + 1e-10).any():
        raise ValueError("Net R exceeds gross R: check fee accounting")
    if trades.empty:
        trades["entry_regime"] = pd.Series(dtype=str)
        trades["regime_available_at_ms"] = pd.Series(dtype="Int64")
        return trades
    order = ["exit_timestamp_ms"] + (["exit_agg_trade_id"] if "exit_agg_trade_id" in trades else []) + ["trade_id"]
    if regimes["mode"].iloc[0] == "retrospective":
        key = "end_ms"
    else:
        key = "label_available_at_ms"
    right = regimes[[key, "regime", "label_available_at_ms"]].loc[:, lambda d: ~d.columns.duplicated()].copy()
    right = right.rename(columns={"regime": "entry_regime", "label_available_at_ms": "regime_available_at_ms"})
    join_key = "regime_available_at_ms" if key == "label_available_at_ms" else key
    joined = pd.merge_asof(trades.sort_values("decision_timestamp_ms"), right.sort_values(join_key),
        left_on="decision_timestamp_ms", right_on=join_key, direction="backward")
    joined["entry_regime"] = joined.entry_regime.fillna("unknown")
    joined["regime_available_at_ms"] = joined.regime_available_at_ms.astype("Int64")
    return joined.sort_values(order, kind="stable").reset_index(drop=True)


def summaries(trades):
    rows = []
    for regime in ("bull", "bear", "unknown"):
        part = trades.loc[trades.entry_regime == regime]
        n = len(part)
        rows.append(dict(regime=regime, closed_trades=n, share=n/len(trades) if len(trades) else None,
            gross_expectancy_R=float(part.gross_R.mean()) if n else None,
            net_expectancy_R=float(part.net_R.mean()) if n else None,
            gross_total_R=float(part.gross_R.sum()) if n else None,
            net_total_R=float(part.net_R.sum()) if n else None))
    return rows


def plot_equity(trades, path, mode, instrument, *, daily, regimes, start_ms, end_ms, account_equity=None):
    """Template B: shared UTC price/equity/daily-state/entry-event panels."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as dates
    from matplotlib.patches import Patch
    from matplotlib.ticker import FixedLocator

    colors = {"bull": "#259f83", "bear": "#d15b72", "unknown": "#888e9c"}
    ink, grid = "#23384d", "#e2e7ec"
    start, end = pd.to_datetime([start_ms, end_ms], unit="ms", utc=True)
    if start_ms >= end_ms or mode not in ("causal", "retrospective"):
        raise ValueError("Invalid chart window/mode")
    if regimes.empty or not regimes.regime.isin(colors).all() or not trades.entry_regime.isin(colors).all():
        raise ValueError("Missing/unsupported regime labels")
    if "record_type" in trades and not trades.record_type.eq("closed_trade").all():
        raise ValueError("Chart outcomes must contain only closed trades")
    order = ["exit_timestamp_ms"] + (["exit_agg_trade_id"] if "exit_agg_trade_id" in trades else []) + ["trade_id"]
    trades = trades.sort_values(order, kind="stable")
    exits = integer_ms(trades.exit_timestamp_ms, "exit_timestamp_ms")
    decisions = integer_ms(trades.decision_timestamp_ms, "decision_timestamp_ms")
    if ((exits >= end_ms) | (exits < start_ms) | (decisions < start_ms) | (decisions > exits)).any():
        raise ValueError("Trade events fall outside chart scope or temporal order")
    if not np.isfinite(trades[["gross_R", "net_R"]].to_numpy(dtype=float)).all():
        raise ValueError("Nonfinite chart outcomes")
    price = daily.loc[(daily.end_ms >= start_ms) & (daily.end_ms <= end_ms)]
    if price.empty:
        raise ValueError("No measured daily prices in chart window")
    price_times = integer_ms(price.end_ms, "daily close timestamp")
    if (not price_times.is_monotonic_increasing or price_times.duplicated().any()
            or not np.isfinite(price.close.to_numpy(dtype=float)).all() or (price.close <= 0).any()):
        raise ValueError("Invalid/duplicate measured daily prices")
    key = "label_available_at_ms" if mode == "causal" else "end_ms"
    timeline = regimes.sort_values(key)
    previous = timeline.loc[timeline[key] <= start_ms]
    spans = [(start_ms, str(previous.regime.iloc[-1]) if len(previous) else "unknown")]
    for stamp, label in timeline.loc[(timeline[key] > start_ms) & (timeline[key] < end_ms), [key, "regime"]].itertuples(index=False, name=None):
        if label != spans[-1][1]:
            spans.append((int(stamp), str(label)))

    fig, axes = plt.subplots(4, 1, figsize=(15.5, 11), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 3.2, .55, .65], "hspace": .16}, facecolor="white")
    price_ax, equity_ax, regime_ax, entry_ax = axes
    fig.subplots_adjust(left=.085, right=.965, top=.87, bottom=.14)
    fig.suptitle(f"{instrument} | Price, strategy equity and regimes | {mode.upper()}",
        x=.085, y=.965, ha="left", fontsize=17, weight="bold", color=ink)
    fig.text(.085, .923, f"{start:%d %b %Y} - {end:%d %b %Y} (end exclusive) | Shared calendar axis: UTC", color=ink)
    fig.legend(handles=[Patch(facecolor=c, label=k.title()) for k,c in colors.items()],
        loc="upper right", bbox_to_anchor=(.969, .939), ncol=3, frameon=False)
    price_ax.plot(pd.to_datetime(price.end_ms, unit="ms", utc=True), price.close, color=ink, linewidth=1.35)
    price_ax.set_ylabel("Price (quote units)")
    price_ax.set_title("Daily closing price", loc="left", pad=9)

    x = pd.DatetimeIndex([start, *pd.to_datetime(exits, unit="ms", utc=True), end])
    if account_equity is None:
        initial, gross_values, net_values = 0., trades.gross_R, trades.net_R
        unit, title, note = "R", "Strategy equity in R", "No compounding"
    else:
        initial = account_equity["initial_equity"]
        if not np.isfinite(initial) or initial <= 0 or not np.isfinite(trades[["gross_pnl", "net_pnl"]].to_numpy(dtype=float)).all():
            raise ValueError("Invalid monetary equity chart inputs")
        gross_values, net_values = trades.gross_pnl, trades.net_pnl
        unit = account_equity["pnl_unit"]
        title, note = "Observed realized account equity", "Source quantities; gross adds back recorded fees, without resizing"
    gross = np.r_[initial, initial+gross_values.cumsum(), initial+gross_values.sum()]
    net = np.r_[initial, initial+net_values.cumsum(), initial+net_values.sum()]
    equity_ax.step(x, gross, where="post", color="#919ba8", linestyle="--", linewidth=1.45, label=f"Gross {gross[-1]:+.3f} {unit}")
    equity_ax.step(x, net, where="post", color=ink, linewidth=1.8, label=f"Net {net[-1]:+.3f} {unit}")
    equity_ax.scatter(pd.to_datetime(exits, unit="ms", utc=True), initial+net_values.cumsum(),
        c=trades.entry_regime.map(colors), s=19, edgecolors="white", linewidths=.45, zorder=4)
    equity_ax.axhline(initial, color="#adb5bd", linewidth=.7)
    equity_ax.set_ylabel("Realized cumulative R" if account_equity is None else f"Equity ({unit})")
    equity_ax.set_title(f"{title} | Changes at exits; dots colored by entry regime", loc="left", pad=9)
    equity_ax.legend(loc="lower left", ncol=2, facecolor="white", edgecolor=grid)
    equity_ax.text(.995, .97, f"{len(trades)} closed trades | {note}", transform=equity_ax.transAxes,
        ha="right", va="top", fontsize=9, color="#657284")
    if trades.empty:
        equity_ax.text(.5, .5, "No closed trades in the declared window", ha="center", transform=equity_ax.transAxes)

    for i, (a, label) in enumerate(spans):
        b = spans[i+1][0] if i+1 < len(spans) else end_ms
        left, right = pd.to_datetime([a,b], unit="ms", utc=True)
        regime_ax.axvspan(left, right, color=colors[label], linewidth=0)
        if (b-a)/(end_ms-start_ms) >= .05:
            regime_ax.text(left+(right-left)/2, .5, label.title(), ha="center", va="center", fontsize=9, color="white", weight="bold")
    regime_ax.set_ylim(0, 1)
    regime_ax.set_yticks([])
    regime_ax.set_ylabel("Daily\nregime", rotation=0, ha="right", va="center", labelpad=15)
    entry_ax.set_ylim(0, 1)
    entry_ax.set_yticks([])
    entry_ax.set_ylabel("Entry\nregime", rotation=0, ha="right", va="center", labelpad=15)
    for label, marker in (("bull", "^"), ("bear", "v"), ("unknown", "o")):
        selected = trades.entry_regime == label
        when = pd.to_datetime(decisions[selected], unit="ms", utc=True)
        entry_ax.vlines(when, .1, .7, color=colors[label], linewidth=1.2, alpha=.85)
        entry_ax.scatter(when, np.full(selected.sum(), .7), marker=marker, s=24, color=colors[label], zorder=3)
    months = max(1, (end.year-start.year)*12+end.month-start.month)
    step = max(1, int(np.ceil(months/10)))
    month_start = start.replace(day=1)
    ticks = [start] + [d for d in pd.date_range(month_start, end, freq=f"{step}MS")
        if d-start >= (end-start)*.055 and end-d >= (end-start)*.055] + [end]
    entry_ax.xaxis.set_major_locator(FixedLocator(dates.date2num(ticks)))
    entry_ax.xaxis.set_major_formatter(dates.DateFormatter("%d %b\n%Y", tz=start.tzinfo))
    entry_ax.tick_params(axis="x", length=0, pad=11)
    entry_ax.set_xlabel("Calendar date (UTC)", labelpad=12)
    for ax in axes:
        ax.set_xlim(start, end)
        ax.set_facecolor("white")
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#b2bbc5")
        ax.grid(axis="x", color=grid, linewidth=.65)
        if ax in (price_ax, equity_ax):
            ax.grid(axis="y", color=grid, linewidth=.7)
        if ax is not entry_ax:
            ax.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
    fig.text(.085, .045, "Entry marks = decision times. Exit dots retain each trade's entry regime. Daily regime = state at that time.", fontsize=9, color="#657284")
    note = "Unknown = insufficient warmup/training. Realized equity is not mark-to-market."
    if mode == "retrospective":
        note += " HINDSIGHT: daily states were not available at historical entry time."
    fig.text(.085, .025, note, fontsize=9, color="#657284")
    fig.savefig(path, dpi=150, facecolor="white", metadata={"Title": f"{instrument} | {CHART} | {mode}",
        "Description": "Shared UTC price/realized-equity/daily-regime/entry-regime panels; no outcome refitting."})
    plt.close(fig)
    return fig


def render_saved_run(run, output):
    """Re-render complete saved results into a new folder, without fitting or overwriting."""
    run, output = Path(run).resolve(), Path(output).resolve()
    if output.is_relative_to(run) or run.is_relative_to(output):
        raise ValueError("Chart export and original run must be separate folders")
    source_files = {name: run/name for name in ("run.json", "regimes.parquet", "attributed_trades.parquet")}
    hashes = {name: digest(path) for name,path in source_files.items()}
    saved = json.loads(source_files["run.json"].read_text(encoding="utf-8"))
    if saved.get("status") != "complete":
        raise ValueError("Cannot render an incomplete/failed run")
    output.mkdir(parents=True, exist_ok=False)
    receipt = dict(version=VERSION, operation="render_saved_run", status="running", chart=CHART,
        source_run=str(run), source_sha256=hashes, source_config=saved["config"],
        source_version=saved.get("version"), renderer_sha256=digest(Path(__file__)), model_refit=False,
        source_chart=saved.get("chart"),
        price_source="daily close snapshots saved in regimes.parquet; no raw/cache rebuild")
    def save():
        (output/"run.json").write_text(json.dumps(receipt, indent=2, allow_nan=False), encoding="utf-8")
    save()
    try:
        # Saved daily snapshots are authoritative for this rendering, even if cache code changed later.
        regimes = pd.read_parquet(source_files["regimes.parquet"])
        trades = pd.read_parquet(source_files["attributed_trades.parquet"])
        plot_equity(trades, output/"equity_regimes.png", saved["config"]["mode"], saved["instrument"],
            daily=regimes, regimes=regimes, start_ms=saved["start_ms"], end_ms=saved["end_ms"],
            account_equity=saved.get("chart", {}).get("account_equity"))
        if any(digest(path) != hashes[name] for name,path in source_files.items()):
            raise ValueError("Saved run changed during rendering")
        receipt.update(status="complete", closed_trades=len(trades), gross_total_R=float(trades.gross_R.sum()),
            net_total_R=float(trades.net_R.sum()), chart_sha256=digest(output/"equity_regimes.png"))
        save()
    except Exception as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        save()
        raise
    print(f"Rendered without refitting: {output}")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            value.update(block)
    return value.hexdigest()


def selected_run(args, start, end):
    """Validate one immutable account before fitting; retain original master columns."""
    import pyarrow.parquet as pq
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from model_development.phase_3 import run_input
    data = run_input.load_run(args.run, dict(model_id=args.model_id, instrument=args.instrument,
        configuration_id=args.configuration, submodel_id=args.submodel, portfolio_id=args.portfolio))
    output, folder = args.output.resolve(), data["folder"]
    if output.is_relative_to(folder) or folder.is_relative_to(output):
        raise ValueError("Evaluation output must be separate from the immutable source run")
    cache = (args.cache_dir or args.project_root/"tmp/cache/observe"/args.instrument) if args.input else args.candle_cache
    if cache is not None and (cache.resolve().is_relative_to(folder) or folder.is_relative_to(cache.resolve())):
        raise ValueError("Candle cache must be separate from the immutable source run")
    for key in ("venue", "product"):
        declared = data["account_metadata"].get(key, data["metadata"].get(key))
        if declared is not None and declared != getattr(args, key):
            raise ValueError(f"Selected source {key} conflicts with market declaration")
    if any(r["decision_timestamp_ms"] < start or r["exit_timestamp_ms"] >= end for r in data["trades"]):
        raise ValueError("Diagnosis window must preserve every selected closed outcome; no silent exclusion")
    table = pq.read_table(folder/"trades.parquet")
    columns = ["record_type", *[k for k in run_input.IDENTITY if k in table.column_names]]
    identity = tuple(data["identity"][k] for k in run_input.IDENTITY)
    indexes = [i for i, r in enumerate(table.select(columns).to_pylist())
        if r["record_type"] == "closed_trade" and run_input.identify(r, data["metadata"]) == identity]
    master = (table.take(indexes) if indexes else table.slice(0, 0)).to_pandas()
    run_input.verify_source(data)
    if set(master.trade_id) != {r["trade_id"] for r in data["trades"]}:
        raise ValueError("Selected master identity changed during loading")
    return data, master, Path(run_input.__file__)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "render":
        p = argparse.ArgumentParser(description="Render a complete saved run with Template B; no model fitting")
        p.add_argument("--run", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        a = p.parse_args(argv[1:])
        return render_saved_run(a.run, a.output)
    p = argparse.ArgumentParser(description=__doc__, epilog="To redraw saved results: statistical_jump.py render --run <folder> --output <new-folder>")
    market = p.add_mutually_exclusive_group(required=True)
    market.add_argument("--market", type=Path, help="Measured OHLCV Parquet, UTC opening timestamp_ms")
    market.add_argument("--input", type=Path, nargs="+", help="Chronological raw aggTrade Parquets; reuse/build global cache")
    market.add_argument("--candle-cache", type=Path, help="Existing shared folder containing candles_1m.parquet and cache.json")
    p.add_argument("--cache-dir", type=Path, help="Shared cache root for --input; outside run output")
    p.add_argument("--instrument", required=True)
    p.add_argument("--venue", required=True, help="Declared provenance; use unverified when unknown")
    p.add_argument("--product", required=True)
    p.add_argument("--base-timeframe", default="1d")
    p.add_argument("--start", required=True, help="First regime decision boundary; UTC inclusive")
    p.add_argument("--end", required=True, help="UTC exclusive report bound; input through this close is required")
    p.add_argument("--reserved-oos-start", help="Refuse access to this period or later")
    p.add_argument("--inspection-status", required=True, choices=["development", "unknown", "uninspected"])
    p.add_argument("--mode", choices=["causal", "retrospective"], default="causal")
    p.add_argument("--jump-penalty", type=float, default=50.)
    p.add_argument("--train-days", type=int, default=3000)
    p.add_argument("--inference-days", type=int)
    p.add_argument("--warmup-days", type=int, default=300)
    p.add_argument("--refit-months", type=int, default=6)
    p.add_argument("--restarts", type=int, default=10)
    p.add_argument("--max-iter", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--run", type=Path, help="Explicit completed Phase 1/2 run; select exactly one account")
    p.add_argument("--model-id")
    p.add_argument("--portfolio")
    p.add_argument("--trades", type=Path, help="Optional legacy observer master; creates cumulative-R/regime chart")
    p.add_argument("--trade-run", type=Path, help="Defaults to run.json beside --trades; must be complete")
    p.add_argument("--configuration")
    p.add_argument("--submodel")
    p.add_argument("--project-root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--shared-core", type=Path, default=DEFAULT_CORE)
    p.add_argument("--output", type=Path, required=True, help="Must not already exist")
    a = p.parse_args(argv)
    cfg = Config(jump_penalty=a.jump_penalty, train_days=a.train_days,
        inference_days=a.train_days if a.inference_days is None else a.inference_days, refit_months=a.refit_months,
        warmup_days=a.warmup_days, restarts=a.restarts, max_iter=a.max_iter, seed=a.seed, mode=a.mode)
    cfg.validate()
    start, end = utc_boundary(a.start), utc_boundary(a.end)
    if start >= end:
        p.error("start must precede end")
    if a.reserved_oos_start and end > utc_boundary(a.reserved_oos_start):
        p.error("Requested window touches reserved OOS")
    if a.cache_dir is not None and not a.input:
        p.error("--cache-dir is only used with --input")
    if not a.market and a.base_timeframe != "1d":
        p.error("Raw/cache modes determine their own 1m input and 1d prepared timeframes")
    if a.run and (a.trades or a.trade_run):
        p.error("--run cannot be combined with legacy --trades/--trade-run")
    if not a.run and (a.model_id or a.portfolio):
        p.error("--model-id/--portfolio require --run")
    selected, selected_master, reader_path = selected_run(a, start, end) if a.run else (None, None, None)
    if selected is not None:
        a.trades, a.trade_run = selected["folder"]/"trades.parquet", selected["folder"]/"run.json"
    dependencies, dependency_paths = load_dependencies(a.project_root, a.shared_core)
    if a.trade_run and not a.trades:
        p.error("--trade-run requires --trades")
    trade_run = a.trade_run or (a.trades.with_name("run.json") if a.trades else None)
    sources = {"market": a.market} if a.market else {}
    if a.trades:
        sources.update(trades=a.trades, trade_run=trade_run)
    fingerprints = {name: {"path": str(path.resolve()), "sha256": digest(path)} for name,path in sources.items()}
    code = {str(path.resolve()): digest(path) for path in [Path(__file__), *dependency_paths.values()]}
    if reader_path is not None:
        code[str(reader_path.resolve())] = digest(reader_path)
        dispatcher = reader_path.with_name("evaluate.py")
        code[str(dispatcher.resolve())] = digest(dispatcher)
    a.output.mkdir(parents=True, exist_ok=False)
    receipt = dict(version=VERSION, status="running", config=asdict(cfg),
        arguments={k: str(v) if isinstance(v,Path) else [str(x) for x in v] if isinstance(v,list) else v for k,v in vars(a).items()},
        sources=fingerprints, code_sha256=code, phase="post_modelling_evaluation",
        purpose="diagnosis_only", instrument=a.instrument, venue=a.venue, product=a.product,
        start_ms=start, end_ms=end, base_timeframe=a.base_timeframe if a.market else "1m", prepared_timeframe="1d", calculation_timeframe="1d",
        evaluation_timeframe="1d", prior_inspection=a.inspection_status,
        reserved_oos_start=a.reserved_oos_start, selection="fixed predeclared lambda; no optimization",
        paper="https://arxiv.org/html/2402.05272v3",
        limits=["Not a numerical replication of published equity-index results", "No forecast/switching probability or entry filter",
                "Bull/bear are relative training-return labels", "End-open positions excluded by source observer",
                "Short windows are explicit adaptations; no fresh OOS claim", "Historical availability assumes candle close when unspecified"])
    if selected is not None:
        receipt.update(input_contract="selected_phase_run_v1", selected_account=selected["identity"],
            source_hashes=selected["hashes"], source_bounds_ms=selected["source_bounds_ms"],
            source_accounting=selected["source_summary"], source_context=selected["source_context"],
            source_prior_inspection=selected["metadata"].get("prior_inspection", selected["metadata"].get("prior_test_inspection")),
            iteration_id=selected["account_metadata"].get("iteration_id", selected["metadata"].get("iteration_id")),
            checkpoint_id=selected["account_metadata"].get("checkpoint_id", selected["metadata"].get("checkpoint_id")))
        receipt["limits"].append("All selected closed outcomes retained; gross monetary curve uses observed quantities, not a resized fee-free backtest")
    def save():
        (a.output/"run.json").write_text(json.dumps(receipt, indent=2, allow_nan=False), encoding="utf-8")
    save()
    try:
        daily, cache_receipt, market_sources = load_market(a, end, dependencies)
        for name,path in market_sources.items():
            sources[name] = path
            if name not in fingerprints:
                expected = (cache_receipt["manifest"]["sha256"] if name == "market_cache"
                            else cache_receipt["manifest_sha256"])
                fingerprints[name] = {"path": str(path.resolve()), "sha256": expected}
        receipt["risk_free_assumption"] = daily.attrs["risk_free_assumption"]
        if cache_receipt is not None:
            receipt["market_cache"] = cache_receipt
        regimes, refits = classify(daily, cfg, start, end, dependencies)
        regimes["instrument"] = a.instrument
        regimes["venue"] = a.venue
        regimes["product"] = a.product
        regimes.to_parquet(a.output/"regimes.parquet", index=False)
        receipt.update(refits=refits, counts=dict(daily_rows=len(regimes), unknown_days=int(regimes.state.isna().sum())))
        if a.trades:
            source_run = json.loads(trade_run.read_text(encoding="utf-8"))
            if source_run.get("status") != "complete" or any(j.get("status") != "complete" for j in source_run.get("jobs", [])):
                raise ValueError("Trade source is incomplete/failed")
            declared = source_run.get("instrument")
            if declared and declared != a.instrument:
                raise ValueError("Trade metadata instrument conflicts")
            master = selected_master if selected is not None else pd.read_parquet(a.trades)
            if "instrument" not in master and not declared:
                raise ValueError("Trade provenance needs instrument in master or source run")
            trades = attribute_trades(master, regimes, a.instrument, start, end, a.configuration, a.submodel)
            if selected is not None:
                expected = {r["trade_id"] for r in selected["trades"]}
                if len(trades) != len(expected) or set(trades.trade_id) != expected:
                    raise ValueError("Regime attribution discarded selected outcomes")
                for column in ("gross_pnl", "fees", "net_pnl"):
                    if not np.isclose(trades[column].sum(), selected["source_summary"][column], rtol=1e-9, atol=1e-8):
                        raise ValueError("Attributed source accounting mismatch")
            trades.to_parquet(a.output/"attributed_trades.parquet", index=False)
            receipt["trade_source"] = source_run
            receipt["by_entry_regime"] = summaries(trades)
            receipt["counts"].update(closed_trades=len(trades), unknown_trades=int((trades.entry_regime == "unknown").sum()))
            capital = None
            if selected is not None and selected["initial_equity"] is not None:
                unit = selected["account_metadata"].get("pnl_unit", selected["metadata"].get("pnl_unit"))
                capital = dict(initial_equity=selected["initial_equity"], pnl_unit=unit or "declared account units")
            plot_equity(trades, a.output/"equity_regimes.png", cfg.mode, a.instrument,
                daily=regimes, regimes=regimes, start_ms=start, end_ms=end, account_equity=capital)
            receipt["chart"] = dict(template=CHART, x_axis="calendar_utc",
                equity="observed_realized_monetary_equity_at_exit" if capital else "realized_cumulative_R_at_exit",
                account_equity=capital, daily_regime="availability time (causal) or historical close time (retrospective)",
                entry_marks="decision_timestamp_ms")
        if any(digest(sources[name]) != info["sha256"] for name,info in fingerprints.items()):
            raise ValueError("Input changed during evaluation")
        if cache_receipt is not None:
            identities = cache_receipt["manifest"]["identity"]["sources"]
            if dependencies["cache"].source_identity([Path(s["path"]) for s in identities]) != identities:
                raise ValueError("Raw source identities changed during evaluation")
        if selected is not None:
            from model_development.phase_3.run_input import verify_source
            verify_source(selected)
        receipt["status"] = "complete"
        save()
    except Exception as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        save()
        raise
    print(f"Complete ({cfg.mode}): {a.output}")


if __name__ == "__main__":
    main()
