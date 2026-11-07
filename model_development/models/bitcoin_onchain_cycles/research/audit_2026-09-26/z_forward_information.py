"""Descriptive evidence for the edge-ideation note.

Question: does the MVRV Z-score carry information about forward returns, which is
what the whole accumulation family assumes? This is a description of the
in-sample window only. It fits nothing, chooses nothing, and is not evidence of
deployability; the out-of-sample window is one 531-day observation and cannot add
to it.

Run from the repository root:
    python model_development/models/bitcoin_onchain_cycles/research/audit_2026-09-26/z_forward_information.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PREPARED = "model_development/models/bitcoin_onchain_cycles/data/prepared/paper_daily.csv"
HORIZONS = (180, 365, 730)
# The buckets are the model's own thresholds, so the table speaks about the rule.
BUCKETS = (
    ("Z < -0.20 (tier 3 zone)", -np.inf, -0.20),
    ("-0.20 <= Z < 0.00 (tier 2 zone)", -0.20, 0.00),
    ("0.00 <= Z < 0.50 (tier 1 zone)", 0.00, 0.50),
    ("0.50 <= Z < 2.00 (neutral)", 0.50, 2.00),
    ("2.00 <= Z < 5.00 (elevated)", 2.00, 5.00),
    ("Z >= 5.00 (no-buy)", 5.00, np.inf),
)


def maximum_drawdown(prices: np.ndarray) -> float:
    """A trough is measured against a peak already reached, never a future peak."""
    prices = np.asarray(prices, dtype=float)
    if len(prices) == 0 or not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("prices must be nonempty, finite and positive")
    return float(np.min(prices / np.maximum.accumulate(prices) - 1.0))


def main() -> None:
    daily = pd.read_csv(PREPARED, parse_dates=["date"]).sort_values("date")
    close = daily["close"].to_numpy(dtype=float)
    zscore = daily["mvrv_zscore"].to_numpy(dtype=float)
    dates = daily["date"].to_numpy()

    frame = pd.DataFrame({"date": dates, "zscore": zscore})
    for horizon in HORIZONS:
        forward_return = np.full(len(close), np.nan)
        forward_return[:-horizon] = close[horizon:] / close[:-horizon] - 1.0
        forward_drawdown = np.full(len(close), np.nan)
        for index in range(len(close) - horizon):
            window = close[index : index + horizon + 1]
            forward_drawdown[index] = maximum_drawdown(window)
        frame[f"fwd_return_{horizon}d"] = forward_return
        frame[f"fwd_worst_drawdown_{horizon}d"] = forward_drawdown

    print(f"In-sample description, {frame['date'].iloc[0].date()} to {frame['date'].iloc[-1].date()}")
    print(f"rows {len(frame)}, Z range {zscore.min():.3f} to {zscore.max():.3f}")
    print()
    header = f"{'bucket':34s} {'days':>5s} {'share':>6s}"
    for horizon in HORIZONS:
        header += f" | {horizon}d eligible {horizon}d med ret {horizon}d %neg {horizon}d med worst DD"
    print(header)
    for label, low, high in BUCKETS:
        mask = (frame["zscore"] >= low) & (frame["zscore"] < high)
        rows = frame.loc[mask]
        line = f"{label:34s} {len(rows):5d} {len(rows) / len(frame):6.1%}"
        for horizon in HORIZONS:
            returns = rows[f"fwd_return_{horizon}d"].dropna()
            drawdowns = rows[f"fwd_worst_drawdown_{horizon}d"].dropna()
            if returns.empty:
                line += " | 0        n/a       n/a             n/a"
                continue
            line += (
                f" | {len(returns):5d} {returns.median():+11.1%} {float(returns.lt(0).mean()):9.1%}"
                f" {drawdowns.median():14.1%}"
            )
        print(line)

    print()
    print("Whole-sample reference (every day, same windows):")
    for horizon in HORIZONS:
        returns = frame[f"fwd_return_{horizon}d"].dropna()
        drawdowns = frame[f"fwd_worst_drawdown_{horizon}d"].dropna()
        print(
            f"  {horizon:4d}d: median {returns.median():+.1%}, "
            f"share negative {float(returns.lt(0).mean()):.1%}, "
            f"median worst drawdown {drawdowns.median():.1%}"
        )


if __name__ == "__main__":
    main()
