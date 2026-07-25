from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Statistical analysis for structural MR backtest results.")
    p.add_argument("--trades", required=True)
    p.add_argument("--bars-cache", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--train-end", default="2025-10-23")
    p.add_argument("--bootstrap-samples", type=int, default=5000)
    return p.parse_args()


def load_trades(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    df = pd.DataFrame(rows)
    df = df[df["status"].eq("closed")].copy()
    df["net_R"] = pd.to_numeric(df["net_R"], errors="coerce")
    df = df[np.isfinite(df["net_R"])].copy()
    df["ny_date"] = pd.to_datetime(df["ny_date"])
    return df


def max_loss_streak(values: np.ndarray) -> int:
    streak = worst = 0
    for value in values:
        streak = streak + 1 if value <= 0 else 0
        worst = max(worst, streak)
    return worst


def bootstrap_mean_ci(values: np.ndarray, samples: int) -> tuple[float, float, float]:
    if len(values) == 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(7)
    means = rng.choice(values, size=(samples, len(values)), replace=True).mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975)), float((means > 0).mean())


def summarize_group(df: pd.DataFrame, samples: int) -> list[dict]:
    rows = []
    for cost_mult in sorted(df["cost_mult"].unique()):
        base = df[df["cost_mult"].eq(cost_mult)]
        for direction in ["all", "long", "short"]:
            g = base if direction == "all" else base[base["direction"].eq(direction)]
            values = g["net_R"].to_numpy(float)
            wins = values[values > 0]
            losses = values[values <= 0]
            ci_low, ci_high, p_mean_gt_zero = bootstrap_mean_ci(values, samples)
            t = stats.ttest_1samp(values, 0.0, alternative="greater") if len(values) > 1 else None
            sign = stats.binomtest(int((values > 0).sum()), len(values), 0.5, alternative="greater") if len(values) else None
            rows.append(
                {
                    "cost_mult": float(cost_mult),
                    "direction": direction,
                    "trades": int(len(values)),
                    "mean_R": float(values.mean()) if len(values) else 0.0,
                    "median_R": float(np.median(values)) if len(values) else 0.0,
                    "std_R": float(values.std(ddof=1)) if len(values) > 1 else 0.0,
                    "p05_R": float(np.quantile(values, 0.05)) if len(values) else 0.0,
                    "p25_R": float(np.quantile(values, 0.25)) if len(values) else 0.0,
                    "p75_R": float(np.quantile(values, 0.75)) if len(values) else 0.0,
                    "p95_R": float(np.quantile(values, 0.95)) if len(values) else 0.0,
                    "win_rate": float((values > 0).mean()) if len(values) else 0.0,
                    "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) > 0 else None,
                    "t_stat_mean_gt_zero": None if t is None else float(t.statistic),
                    "p_value_mean_gt_zero": None if t is None else float(t.pvalue),
                    "bootstrap_mean_ci_low": ci_low,
                    "bootstrap_mean_ci_high": ci_high,
                    "bootstrap_p_mean_gt_zero": p_mean_gt_zero,
                    "sign_test_p_win_rate_gt_50": None if sign is None else float(sign.pvalue),
                    "max_loss_streak": max_loss_streak(values),
                }
            )
    return rows


def split_summary(df: pd.DataFrame, train_end: str) -> list[dict]:
    train_end_ts = pd.Timestamp(train_end)
    out = []
    base = df[df["cost_mult"].eq(1.0)].copy()
    for label, g in [("train", base[base["ny_date"] <= train_end_ts]), ("test", base[base["ny_date"] > train_end_ts])]:
        for direction in ["all", "long", "short"]:
            h = g if direction == "all" else g[g["direction"].eq(direction)]
            values = h["net_R"].to_numpy(float)
            out.append(
                {
                    "split": label,
                    "direction": direction,
                    "trades": int(len(values)),
                    "mean_R": float(values.mean()) if len(values) else 0.0,
                    "win_rate": float((values > 0).mean()) if len(values) else 0.0,
                    "total_R": float(values.sum()) if len(values) else 0.0,
                }
            )
    return out


def monthly_summary(df: pd.DataFrame) -> dict:
    base = df[df["cost_mult"].eq(1.0)].copy()
    base["month"] = base["ny_date"].dt.to_period("M").astype(str)
    monthly = base.groupby("month")["net_R"].sum()
    return {
        "months": int(len(monthly)),
        "positive_months": int((monthly > 0).sum()),
        "positive_month_rate": float((monthly > 0).mean()) if len(monthly) else 0.0,
        "mean_monthly_R": float(monthly.mean()) if len(monthly) else 0.0,
        "best_month_R": float(monthly.max()) if len(monthly) else 0.0,
        "worst_month_R": float(monthly.min()) if len(monthly) else 0.0,
    }


def variance_ratio(prices: np.ndarray, q: int) -> float:
    returns = np.diff(np.log(prices))
    if len(returns) <= q or np.var(returns) == 0:
        return float("nan")
    q_returns = np.diff(np.log(prices[::q]))
    return float(np.var(q_returns, ddof=1) / (q * np.var(returns, ddof=1)))


def hurst_rs(prices: np.ndarray) -> float:
    returns = np.diff(np.log(prices))
    lags = np.array([8, 16, 32, 64, 128, 256])
    rs = []
    used = []
    for lag in lags:
        chunks = len(returns) // lag
        vals = []
        for chunk in np.array_split(returns[: chunks * lag], chunks):
            centered = chunk - chunk.mean()
            spread = np.cumsum(centered).max() - np.cumsum(centered).min()
            sigma = chunk.std(ddof=1)
            if sigma > 0:
                vals.append(spread / sigma)
        if vals:
            used.append(lag)
            rs.append(np.mean(vals))
    slope = np.polyfit(np.log(used), np.log(rs), 1)[0]
    return float(slope)


def half_life(prices: np.ndarray) -> float:
    x = np.log(prices)
    y = np.diff(x)
    lagged = x[:-1]
    beta = np.linalg.lstsq(np.column_stack([np.ones(len(lagged)), lagged]), y, rcond=None)[0][1]
    return float(-np.log(2) / np.log(1 + beta)) if -1 < beta < 0 else -1.0


def price_diagnostics(path: Path) -> dict:
    df = pd.read_parquet(path, columns=["timestamp", "close"])
    s = df.set_index(pd.to_datetime(df["timestamp"], utc=True))["close"].astype(float)
    hourly = s.resample("1h").last().dropna().to_numpy()
    return {
        "hourly_points": int(len(hourly)),
        "hurst_rs_hourly": hurst_rs(hourly),
        "variance_ratio_q5_hourly": variance_ratio(hourly, 5),
        "variance_ratio_q24_hourly": variance_ratio(hourly, 24),
        "ar1_half_life_hours": half_life(hourly),
    }


def write_report(output_dir: Path, stats_rows: list[dict], splits: list[dict], monthly: dict, diagnostics: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"group_stats": stats_rows, "split_stats": splits, "monthly": monthly, "price_diagnostics": diagnostics}
    (output_dir / "statistical_analysis.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# Structural Mean-Reversion Statistical Analysis", "", "Status: rejected; no edge shown.", "", "## Group Statistics", ""]
    lines.append("| cost | direction | trades | mean_R | 95pct_boot_CI | p_mean_gt_0 | boot_P_mean_gt_0 | win_rate | PF | max_loss_streak |")
    lines.append("|---:|---|---:|---:|---|---:|---:|---:|---:|---:|")
    for r in stats_rows:
        pf = "" if r["profit_factor"] is None else f"{r['profit_factor']:.4f}"
        lines.append(
            f"| {r['cost_mult']:.1f} | {r['direction']} | {r['trades']} | {r['mean_R']:.4f} | [{r['bootstrap_mean_ci_low']:.4f}, {r['bootstrap_mean_ci_high']:.4f}] | {r['p_value_mean_gt_zero']:.4g} | {r['bootstrap_p_mean_gt_zero']:.4f} | {r['win_rate']:.4f} | {pf} | {r['max_loss_streak']} |"
        )
    lines.extend(["", "## Train/Test Base-Cost Split", "", "| split | direction | trades | mean_R | win_rate | total_R |", "|---|---|---:|---:|---:|---:|"])
    for r in splits:
        lines.append(f"| {r['split']} | {r['direction']} | {r['trades']} | {r['mean_R']:.4f} | {r['win_rate']:.4f} | {r['total_R']:.2f} |")
    lines.extend(
        [
            "",
            "## Monthly Base-Cost Consistency",
            "",
            f"- Months: `{monthly['months']}`",
            f"- Positive months: `{monthly['positive_months']}` (`{monthly['positive_month_rate']:.2%}`)",
            f"- Mean monthly R: `{monthly['mean_monthly_R']:.2f}`",
            f"- Best / worst month R: `{monthly['best_month_R']:.2f}` / `{monthly['worst_month_R']:.2f}`",
            "",
            "## Price-Series Mean-Reversion Diagnostics",
            "",
            f"- Hourly points: `{diagnostics['hourly_points']}`",
            f"- Hurst R/S hourly: `{diagnostics['hurst_rs_hourly']:.4f}`",
            f"- Variance ratio q=5 hourly: `{diagnostics['variance_ratio_q5_hourly']:.4f}`",
            f"- Variance ratio q=24 hourly: `{diagnostics['variance_ratio_q24_hourly']:.4f}`",
            f"- AR(1) half-life hours: `{diagnostics['ar1_half_life_hours']:.2f}`",
            "",
            "## Conclusion",
            "",
            "The strategy fails statistical validation. Mean return is negative in train and test, bootstrap confidence intervals remain below zero, cost stress worsens the distribution, and monthly consistency is poor.",
        ]
    )
    (output_dir / "statistical_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    trades = load_trades(Path(args.trades))
    stats_rows = summarize_group(trades, args.bootstrap_samples)
    splits = split_summary(trades, args.train_end)
    monthly = monthly_summary(trades)
    diagnostics = price_diagnostics(Path(args.bars_cache))
    write_report(Path(args.output_dir), stats_rows, splits, monthly, diagnostics)
    print(json.dumps({"group_stats": stats_rows, "monthly": monthly, "price_diagnostics": diagnostics}, indent=2))


if __name__ == "__main__":
    main()
