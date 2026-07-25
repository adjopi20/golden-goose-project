from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


PAPER_START = "2013-12-07"
PAPER_END = "2025-04-12"
BITCOIN_GENESIS = pd.Timestamp("2009-01-03")
CVDD_SCALE = 6_000_000

PAPER_THRESHOLDS = {
    "nupl_1": ("nupl", 0.0, 0.67),
    "nupl_2": ("nupl", 0.0, 0.70),
    "nupl_3": ("nupl", 0.0, 0.73),
    "mvrv_1": ("mvrv_zscore", -0.2, 5.0),
    "mvrv_2": ("mvrv_zscore", -0.2, 6.0),
    "mvrv_3": ("mvrv_zscore", -0.2, 7.0),
}

PAPER_CVDD_TRADES = (
    ("cvdd_1", "2015-01-14", "2017-12-17"),
    ("cvdd_2", "2018-12-14", "2021-11-08"),
    ("cvdd_3", "2022-11-09", PAPER_END),
)

HYBRID_STRATEGIES = (
    "cvdd_mvrv_6",
    "paper_cvdd_entries_mvrv_6",
)

PAPER_REPORTED = {
    "nupl_1": (4.77, 0.82),
    "nupl_2": (5.19, 0.88),
    "nupl_3": (7.12, 1.12),
    "mvrv_1": (5.81, 1.01),
    "mvrv_2": (7.40, 1.19),
    "mvrv_3": (8.09, 1.28),
    "buy_hold": (4.79, 0.45),
}

PAPER_CVDD_REPORTED = {
    "cvdd_1": (4.75, 2.34),
    "cvdd_2": (3.03, 1.35),
    "cvdd_3": (1.66, 1.41),
}


def prepare_daily_data(
    btc_csv: str | Path,
    cdd_tsv: str | Path,
    start: str = PAPER_START,
    end: str = PAPER_END,
) -> pd.DataFrame:
    """Build the paper's daily close, NUPL, MVRV Z-score, CDD, and CVDD data."""
    btc = pd.read_csv(btc_csv)
    required = {"time", "PriceUSD", "CapMrktCurUSD", "CapMVRVCur"}
    missing = required - set(btc.columns)
    if missing:
        raise ValueError(f"BTC CSV missing columns: {', '.join(sorted(missing))}")

    btc = btc[list(required)].copy()
    btc["date"] = pd.to_datetime(btc.pop("time"), utc=True).dt.tz_localize(None).dt.normalize()
    if btc["date"].duplicated().any():
        raise ValueError("BTC CSV contains duplicate dates")
    btc = btc.sort_values("date")
    for column in required - {"time"}:
        btc[column] = pd.to_numeric(btc[column], errors="coerce")

    cdd = pd.read_csv(cdd_tsv, sep="\t")
    if len(cdd.columns) != 2:
        raise ValueError("CDD TSV must contain exactly two columns: date and daily CDD")
    cdd.columns = ["date", "cdd"]
    cdd["date"] = pd.to_datetime(cdd["date"], format="%d.%m.%Y")
    cdd["cdd"] = pd.to_numeric(cdd["cdd"], errors="coerce")
    if cdd["date"].duplicated().any():
        raise ValueError("CDD TSV contains duplicate dates")
    if cdd["cdd"].isna().any() or cdd["cdd"].lt(0).any():
        raise ValueError("CDD must contain non-negative numeric values")

    full = btc.merge(cdd, on="date", how="left")
    full["cdd"] = full["cdd"].fillna(0.0)

    mvrv = full["CapMVRVCur"].where(full["CapMVRVCur"].gt(0))
    full["market_cap"] = full["CapMrktCurUSD"]
    full["realized_cap"] = full["market_cap"].div(mvrv)
    full["nupl"] = 1.0 - full["realized_cap"].div(full["market_cap"])
    market_cap_std = full["market_cap"].expanding(min_periods=2).std(ddof=0)
    full["mvrv_zscore"] = (
        full["market_cap"].sub(full["realized_cap"]).div(market_cap_std)
    ).replace([np.inf, -np.inf], np.nan)

    market_age_days = full["date"].sub(BITCOIN_GENESIS).dt.days.add(1)
    if market_age_days.le(0).any():
        raise ValueError("data cannot predate the Bitcoin genesis block")
    value_days_destroyed = full["cdd"].mul(full["PriceUSD"].fillna(0.0))
    full["cvdd"] = value_days_destroyed.cumsum().div(market_age_days.mul(CVDD_SCALE))
    full["close"] = full["PriceUSD"]

    start_date, end_date = pd.Timestamp(start), pd.Timestamp(end)
    daily = full.loc[
        full["date"].between(start_date, end_date),
        [
            "date",
            "close",
            "market_cap",
            "realized_cap",
            "nupl",
            "mvrv_zscore",
            "cdd",
            "cvdd",
        ],
    ].reset_index(drop=True)

    expected = pd.date_range(start_date, end_date, freq="D")
    missing_dates = expected.difference(daily["date"])
    if len(missing_dates):
        raise ValueError(f"prepared data is missing {len(missing_dates)} calendar dates")
    required_values = ["close", "nupl", "mvrv_zscore", "cdd", "cvdd"]
    if daily[required_values].isna().any().any():
        raise ValueError("prepared data contains missing strategy values")
    if daily["close"].le(0).any() or daily["cvdd"].le(0).any():
        raise ValueError("paper-window close and CVDD values must be positive")
    return daily


def backtest_paper_rules(daily: pd.DataFrame) -> pd.DataFrame:
    """Apply Grobys, Nasman & Sandretto (2026) to daily on-chain data.

    Positions are selected at each close and earn the following close-to-close
    return. CVDD produces entries only; it has no real-time exit rule.
    """
    missing = {"close", "nupl", "mvrv_zscore", "cvdd"} - set(daily.columns)
    if missing:
        raise ValueError(f"missing columns: {', '.join(sorted(missing))}")
    if daily.empty:
        return daily.copy()

    out = daily.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    if close.isna().any() or close.le(0).any():
        raise ValueError("close must contain positive numeric values")
    simple_return = close.pct_change(fill_method=None).fillna(0.0)
    log_return = np.log(close).diff().fillna(0.0)

    for name, (column, entry, exit_) in PAPER_THRESHOLDS.items():
        position, action = _long_cash(out[column], entry, exit_)
        out[f"{name}_position"] = position
        out[f"{name}_action"] = action
        out[f"{name}_return"] = position.shift(fill_value=0).mul(simple_return)
        out[f"{name}_log_return"] = position.shift(fill_value=0).mul(log_return)

    out["buy_hold_position"] = 1
    out["buy_hold_return"] = simple_return
    out["buy_hold_log_return"] = log_return

    cvdd = pd.to_numeric(out["cvdd"], errors="coerce")
    out["cvdd_distance"] = close.div(cvdd).sub(1.0).abs()
    cvdd_zone = cvdd.gt(0) & out["cvdd_distance"].le(0.01)
    out["cvdd_entry"] = cvdd_zone & ~cvdd_zone.shift(fill_value=False)

    paper_entry_dates = {pd.Timestamp(entry) for _, entry, _ in PAPER_CVDD_TRADES}
    paper_entries = (
        out["date"].isin(paper_entry_dates)
        if "date" in out
        else pd.Series(False, index=out.index)
    )
    for name, entries in (
        ("cvdd_mvrv_6", out["cvdd_entry"]),
        ("paper_cvdd_entries_mvrv_6", paper_entries),
    ):
        position, action = _entry_signal_exit(out["mvrv_zscore"], entries, 6.0)
        out[f"{name}_position"] = position
        out[f"{name}_action"] = action
        out[f"{name}_return"] = position.shift(fill_value=0).mul(simple_return)
        out[f"{name}_log_return"] = position.shift(fill_value=0).mul(log_return)
    return out


def strategy_summary(result: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy in (*PAPER_THRESHOLDS, *HYBRID_STRATEGIES, "buy_hold"):
        stats = _performance_stats(result[f"{strategy}_log_return"])
        position = result[f"{strategy}_position"]
        paper_log_return, paper_sharpe = PAPER_REPORTED.get(
            strategy, (np.nan, np.nan)
        )
        rows.append(
            {
                "strategy": strategy,
                **stats,
                "paper_cumulative_log_return": paper_log_return,
                "cumulative_log_return_difference": (
                    stats["cumulative_log_return"] - paper_log_return
                ),
                "paper_sharpe_ratio": paper_sharpe,
                "sharpe_ratio_difference": stats["sharpe_ratio"] - paper_sharpe,
                "invested_days": int(position.sum()),
                "sample_days": len(result),
            }
        )
    return pd.DataFrame(rows)


def extract_trades(result: pd.DataFrame) -> pd.DataFrame:
    if "date" not in result or "close" not in result:
        raise ValueError("result must contain date and close columns")
    rows = []
    for strategy in (*PAPER_THRESHOLDS, *HYBRID_STRATEGIES):
        actions = result[f"{strategy}_action"]
        entries = list(result.index[actions.eq("buy")])
        exits = list(result.index[actions.eq("sell")])
        for trade_number, entry_index in enumerate(entries, 1):
            exit_index = next((i for i in exits if i > entry_index), result.index[-1])
            is_open = not any(i > entry_index for i in exits)
            entry_price = float(result.at[entry_index, "close"])
            exit_price = float(result.at[exit_index, "close"])
            log_return = float(np.log(exit_price / entry_price))
            rows.append(
                {
                    "strategy": strategy,
                    "trade": trade_number,
                    "entry_date": result.at[entry_index, "date"],
                    "exit_date": result.at[exit_index, "date"],
                    "status": "open_at_sample_end" if is_open else "closed",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "cumulative_log_return": log_return,
                    "cumulative_simple_return": float(np.expm1(log_return)),
                    "holding_days": exit_index - entry_index + 1,
                }
            )
    return pd.DataFrame(rows)


def paper_cvdd_results(daily: pd.DataFrame) -> pd.DataFrame:
    """Reproduce the paper's CVDD evaluation using its ex-post exit dates."""
    indexed = daily.set_index("date").sort_index()
    rows = []
    for strategy, entry, exit_ in PAPER_CVDD_TRADES:
        entry_date, exit_date = pd.Timestamp(entry), pd.Timestamp(exit_)
        segment = indexed.loc[entry_date:exit_date]
        if segment.empty or segment.index[0] != entry_date or segment.index[-1] != exit_date:
            raise ValueError(f"missing dates for {strategy}")
        log_returns = np.log(segment["close"]).diff().fillna(0.0)
        stats = _performance_stats(log_returns)
        paper_log_return, paper_sharpe = PAPER_CVDD_REPORTED[strategy]
        rows.append(
            {
                "strategy": strategy,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": float(segment["close"].iloc[0]),
                "exit_price": float(segment["close"].iloc[-1]),
                **stats,
                "paper_cumulative_log_return": paper_log_return,
                "cumulative_log_return_difference": (
                    stats["cumulative_log_return"] - paper_log_return
                ),
                "paper_sharpe_ratio": paper_sharpe,
                "sharpe_ratio_difference": stats["sharpe_ratio"] - paper_sharpe,
                "holding_days": len(segment),
                "lookahead_exit": strategy != "cvdd_3",
            }
        )
    return pd.DataFrame(rows)


def cvdd_monte_carlo(
    daily: pd.DataFrame,
    simulations: int = 100,
    windows: tuple[int, ...] = (50, 75, 100),
    seed: int = 42,
) -> pd.DataFrame:
    """Compare the paper's CVDD entry dates with random nearby entry dates."""
    if simulations <= 0:
        raise ValueError("simulations must be positive")
    frame = daily.sort_values("date").reset_index(drop=True)
    date_to_index = {date: index for index, date in enumerate(frame["date"])}
    rng = np.random.default_rng(seed)
    rows = []

    for strategy, entry, exit_ in PAPER_CVDD_TRADES:
        entry_date, exit_date = pd.Timestamp(entry), pd.Timestamp(exit_)
        entry_index, exit_index = date_to_index[entry_date], date_to_index[exit_date]
        exit_price = float(frame.at[exit_index, "close"])
        actual_log_return = float(np.log(exit_price / frame.at[entry_index, "close"]))
        actual_days = max((exit_date - entry_date).days, 1)
        actual_annualized = actual_log_return * 365.0 / actual_days

        for window in windows:
            candidates = np.arange(
                max(0, entry_index - window),
                min(len(frame) - 1, entry_index + window) + 1,
            )
            sampled = rng.choice(candidates, size=simulations, replace=True)
            sampled_prices = frame.loc[sampled, "close"].to_numpy(dtype=float)
            sampled_dates = frame.loc[sampled, "date"].reset_index(drop=True)
            simulated_log = np.log(exit_price / sampled_prices)
            simulated_days = (exit_date - sampled_dates).dt.days.clip(lower=1).to_numpy()
            simulated_annualized = simulated_log * 365.0 / simulated_days
            rows.append(
                {
                    "strategy": strategy,
                    "window_days": window,
                    "simulations": simulations,
                    "seed": seed,
                    "log_return_p_value": float(np.mean(actual_log_return > simulated_log)),
                    "annualized_log_return_p_value": float(
                        np.mean(actual_annualized > simulated_annualized)
                    ),
                }
            )
    return pd.DataFrame(rows)


def _performance_stats(log_returns: pd.Series) -> dict[str, float]:
    values = pd.to_numeric(log_returns, errors="coerce").fillna(0.0)
    equity = np.exp(values.cumsum())
    max_drawdown = float(equity.div(equity.cummax()).sub(1.0).min())
    daily_std = float(values.std(ddof=1))
    annualized_log_return = float(values.mean() * 365.0)
    annualized_volatility = daily_std * np.sqrt(365.0)
    t_stat = (
        float(values.mean() / (daily_std / np.sqrt(len(values))))
        if daily_std > 0
        else np.nan
    )
    return {
        "cumulative_log_return": float(values.sum()),
        "cumulative_simple_return": float(np.expm1(values.sum())),
        "annualized_log_return": annualized_log_return,
        "t_stat": t_stat,
        "annualized_volatility": annualized_volatility,
        "max_drawdown": max_drawdown,
        "min_daily_log_return": float(values.min()),
        "max_daily_log_return": float(values.max()),
        "pearson_kurtosis": float(values.kurt() + 3.0),
        "sharpe_ratio": (
            annualized_log_return / annualized_volatility
            if annualized_volatility > 0
            else np.nan
        ),
    }


def _long_cash(
    values: pd.Series,
    entry_below: float,
    exit_at_or_above: float,
) -> tuple[pd.Series, pd.Series]:
    held = False
    positions: list[int] = []
    actions: list[str] = []

    for value in pd.to_numeric(values, errors="coerce"):
        action = "hold" if held else "cash"
        if pd.notna(value):
            if not held and value < entry_below:
                held, action = True, "buy"
            elif held and value >= exit_at_or_above:
                held, action = False, "sell"
        positions.append(int(held))
        actions.append(action)

    return (
        pd.Series(positions, index=values.index, dtype="int8"),
        pd.Series(actions, index=values.index, dtype="string"),
    )


def _entry_signal_exit(
    exit_values: pd.Series,
    entries: pd.Series,
    exit_at_or_above: float,
) -> tuple[pd.Series, pd.Series]:
    held = False
    positions: list[int] = []
    actions: list[str] = []

    for entry, exit_value in zip(entries, pd.to_numeric(exit_values, errors="coerce")):
        action = "hold" if held else "cash"
        if not held and bool(entry):
            held, action = True, "buy"
        elif held and pd.notna(exit_value) and exit_value >= exit_at_or_above:
            held, action = False, "sell"
        positions.append(int(held))
        actions.append(action)

    return (
        pd.Series(positions, index=exit_values.index, dtype="int8"),
        pd.Series(actions, index=exit_values.index, dtype="string"),
    )
