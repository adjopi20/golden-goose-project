from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import brentq


OPTIONAL_COLUMNS = {
    "btc_units": 0.0,
    "cash": 0.0,
    "net_flow": 0.0,
    "traded_notional": 0.0,
    "btc_bought": 0.0,
    "purchase_notional": 0.0,
}


def calculate_portfolio_metrics(
    ledger: pd.DataFrame,
    benchmark_returns: pd.Series | None = None,
    periods_per_year: int = 365,
) -> dict[str, float | int | pd.Timestamp]:
    """Calculate cash-flow-aware metrics from a daily portfolio ledger."""
    data = _validate_ledger(ledger)
    returns = _time_weighted_returns(data)
    wealth = (1.0 + returns).cumprod()
    drawdown = wealth.div(wealth.cummax()).sub(1.0)
    elapsed_days = max((data["date"].iloc[-1] - data["date"].iloc[0]).days, 0)
    years = elapsed_days / 365.25
    cagr = wealth.iloc[-1] ** (1.0 / years) - 1.0 if years > 0 else np.nan
    volatility = returns.std(ddof=0)
    downside = returns.clip(upper=0).pow(2).mean() ** 0.5
    max_drawdown = drawdown.min()
    cash_weight = data["cash"].div(data["equity"]).replace([np.inf, -np.inf], np.nan).fillna(0)

    if benchmark_returns is None:
        cash_drag = np.nan
    else:
        benchmark_series = pd.Series(
            benchmark_returns, index=data.index, dtype=float
        ).fillna(0.0)
        cash_drag = float(
            (cash_weight.shift(fill_value=cash_weight.iloc[0]) * benchmark_series).sum()
        )

    return {
        "start_date": data["date"].iloc[0],
        "end_date": data["date"].iloc[-1],
        "ending_portfolio_value": data["equity"].iloc[-1],
        "ending_btc_units": data["btc_units"].iloc[-1],
        "average_btc_acquisition_price": _safe_divide(
            data["purchase_notional"].sum(), data["btc_bought"].sum()
        ),
        "money_weighted_return_xirr": _xirr(data),
        "time_weighted_return": wealth.iloc[-1] - 1.0,
        "maximum_drawdown": max_drawdown,
        "time_underwater_days": _longest_underwater_days(data["date"], drawdown),
        "cagr": cagr,
        "sharpe_ratio": _safe_divide(
            returns.mean() * periods_per_year**0.5, volatility
        ),
        "sortino_ratio": _safe_divide(
            returns.mean() * periods_per_year**0.5, downside
        ),
        "calmar_ratio": _safe_divide(cagr, abs(max_drawdown)),
        "average_cash_weight": cash_weight.mean(),
        "cash_drag_vs_benchmark": cash_drag,
        "turnover": _safe_divide(
            data["traded_notional"].abs().sum(), data["equity"].mean()
        ),
        "contributions": data["net_flow"].clip(lower=0).sum(),
        "withdrawals": -data["net_flow"].clip(upper=0).sum(),
    }


def write_portfolio_report(
    ledgers: Mapping[str, pd.DataFrame],
    output_dir: str | Path,
    *,
    benchmark: str,
    groups: Mapping[str, Sequence[str]],
    labels: Mapping[str, str] | None = None,
    title: str = "Portfolio equity comparison",
) -> pd.DataFrame:
    """Write a metrics CSV and grouped equity comparison chart."""
    if benchmark not in ledgers:
        raise ValueError(f"benchmark '{benchmark}' is missing from ledgers")
    validated = {name: _validate_ledger(ledger) for name, ledger in ledgers.items()}
    dates = validated[benchmark]["date"]
    for name, ledger in validated.items():
        if not ledger["date"].equals(dates):
            raise ValueError(f"ledger '{name}' does not share the benchmark dates")

    benchmark_returns = _time_weighted_returns(validated[benchmark])
    report = pd.DataFrame(
        [
            {
                "strategy": name,
                **calculate_portfolio_metrics(ledger, benchmark_returns),
            }
            for name, ledger in validated.items()
        ]
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    report.to_csv(destination / "performance_report.csv", index=False)
    _save_equity_comparison(
        validated,
        destination / "equity_curves.png",
        benchmark=benchmark,
        groups=groups,
        labels=labels or {},
        title=title,
    )
    return report


def _validate_ledger(ledger: pd.DataFrame) -> pd.DataFrame:
    missing = {"date", "equity"} - set(ledger.columns)
    if missing:
        raise ValueError(f"ledger missing columns: {', '.join(sorted(missing))}")
    if ledger.empty:
        raise ValueError("ledger cannot be empty")

    data = ledger.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["equity"] = pd.to_numeric(data["equity"], errors="coerce")
    if data["date"].isna().any() or data["date"].duplicated().any():
        raise ValueError("ledger dates must be valid and unique")
    if data["equity"].isna().any() or data["equity"].le(0).any():
        raise ValueError("ledger equity must be positive and numeric")
    data = data.sort_values("date").reset_index(drop=True)
    for column, default in OPTIONAL_COLUMNS.items():
        if column not in data:
            data[column] = default
        data[column] = pd.to_numeric(data[column], errors="coerce")
        if data[column].isna().any():
            raise ValueError(f"ledger {column} must be numeric")
    return data


def _time_weighted_returns(data: pd.DataFrame) -> pd.Series:
    returns = data["equity"].sub(data["net_flow"]).div(data["equity"].shift()).sub(1.0)
    returns.iloc[0] = 0.0
    return returns


def _xirr(data: pd.DataFrame) -> float:
    cash_flows = -data["net_flow"].to_numpy(dtype=float)
    cash_flows[-1] += data["equity"].iloc[-1]
    if not (np.any(cash_flows < 0) and np.any(cash_flows > 0)):
        return np.nan
    years = (data["date"] - data["date"].iloc[0]).dt.days.to_numpy() / 365.0

    def npv(rate: float) -> float:
        return float(np.sum(cash_flows / np.power(1.0 + rate, years)))

    try:
        return float(brentq(npv, -0.999999, 1_000_000))
    except ValueError:
        return np.nan


def _longest_underwater_days(dates: pd.Series, drawdown: pd.Series) -> int:
    longest = 0
    start = None
    for date, value in zip(dates, drawdown, strict=True):
        if value < 0 and start is None:
            start = date
        elif value >= 0 and start is not None:
            longest = max(longest, (date - start).days)
            start = None
    if start is not None:
        longest = max(longest, (dates.iloc[-1] - start).days + 1)
    return int(longest)


def _safe_divide(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else np.nan


def _save_equity_comparison(
    ledgers: Mapping[str, pd.DataFrame],
    output_path: Path,
    *,
    benchmark: str,
    groups: Mapping[str, Sequence[str]],
    labels: Mapping[str, str],
    title: str,
) -> None:
    figure, axes = plt.subplots(1, len(groups), figsize=(7 * len(groups), 7), squeeze=False)
    for axis, (group_title, strategies) in zip(axes[0], groups.items(), strict=True):
        keys = (benchmark, *strategies)
        for index, key in enumerate(keys):
            if key not in ledgers:
                raise ValueError(f"strategy '{key}' is missing from ledgers")
            ledger = ledgers[key]
            axis.plot(
                ledger["date"],
                ledger["equity"],
                linewidth=2.2 if key == benchmark else 1.8,
                linestyle="--" if key == benchmark else ("-", "-.", ":")[(index - 1) % 3],
                label=labels.get(key, key),
            )
        axis.set_title(group_title, loc="left", fontweight="bold")
        axis.set_yscale("log")
        axis.set_ylabel("Portfolio value (log scale)")
        axis.grid(axis="y", color="#e5e7eb", linewidth=0.8)
        axis.spines[["top", "right"]].set_visible(False)
        axis.legend(frameon=False)
        axis.text(
            0.98,
            0.03,
            "\n".join(
                f"{labels.get(key, key)}: {ledgers[key]['equity'].iloc[-1]:,.0f}x"
                for key in keys
            ),
            transform=axis.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            bbox={"facecolor": "white", "edgecolor": "#e5e7eb", "alpha": 0.9},
        )
    figure.suptitle(title, fontsize=18, fontweight="bold")
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    figure.savefig(output_path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(figure)
