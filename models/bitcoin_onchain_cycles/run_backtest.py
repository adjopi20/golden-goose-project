from __future__ import annotations

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics.backtest_report import write_portfolio_report
from models.bitcoin_onchain_cycles.strategy import (
    HYBRID_STRATEGIES,
    PAPER_THRESHOLDS,
    backtest_paper_rules,
    cvdd_monte_carlo,
    extract_trades,
    paper_cvdd_results,
    prepare_daily_data,
    strategy_summary,
)


MODEL_DIR = Path(__file__).resolve().parent


def build_portfolio_ledgers(result):
    """Translate model positions and returns into the shared daily ledger."""
    ledgers = {}
    for strategy in (*PAPER_THRESHOLDS, *HYBRID_STRATEGIES, "buy_hold"):
        equity = np.exp(result[f"{strategy}_log_return"].cumsum())
        position = result[f"{strategy}_position"].astype(float)
        position_change = position.diff().fillna(position)
        ledgers[strategy] = result[["date"]].assign(
            equity=equity,
            btc_units=equity.mul(position).div(result["close"]),
            cash=equity.mul(1.0 - position),
            net_flow=np.where(result.index == result.index[0], 1.0, 0.0),
            traded_notional=position_change.abs().mul(equity),
            btc_bought=position_change.clip(lower=0).mul(equity).div(result["close"]),
            purchase_notional=position_change.clip(lower=0).mul(equity),
        )
    return ledgers


def save_hybrid_signal_plot(result, output_path: Path) -> None:
    dates = result["date"]
    buys = result["paper_cvdd_entries_mvrv_6_action"].eq("buy")
    sells = result["paper_cvdd_entries_mvrv_6_action"].eq("sell")

    fig, (price_ax, mvrv_ax) = plt.subplots(
        2, 1, figsize=(15, 9), sharex=True, height_ratios=(2, 1)
    )
    price_ax.plot(dates, result["close"], color="#111827", linewidth=1.4, label="BTC price")
    price_ax.plot(dates, result["cvdd"], color="#10b981", linewidth=1.2, label="Calculated CVDD")
    price_ax.scatter(
        dates[buys],
        result.loc[buys, "close"],
        marker="^",
        color="#059669",
        s=80,
        zorder=3,
        label="Paper CVDD entry date",
    )
    price_ax.scatter(
        dates[sells],
        result.loc[sells, "close"],
        marker="v",
        color="#dc2626",
        s=80,
        zorder=3,
        label="MVRV 6 exit",
    )
    price_ax.set_yscale("log")
    price_ax.set_ylabel("USD (log scale)")
    price_ax.set_title("CVDD-entry / MVRV-exit hybrid signals", loc="left", fontweight="bold")
    price_ax.legend(frameon=False, ncol=2)

    mvrv_ax.plot(dates, result["mvrv_zscore"], color="#d97706", linewidth=1.2)
    mvrv_ax.axhline(6.0, color="#dc2626", linestyle="--", linewidth=1.2, label="Exit threshold: 6")
    mvrv_ax.set_ylabel("MVRV Z-score")
    mvrv_ax.legend(frameon=False)

    for ax in (price_ax, mvrv_ax):
        ax.grid(axis="y", color="#e5e7eb", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(output_path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Bitcoin on-chain cycle paper backtest")
    parser.add_argument("--btc", required=True, type=Path, help="Coin Metrics btc.csv")
    parser.add_argument("--cdd", required=True, type=Path, help="Daily CDD data.tsv")
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "paper_replication" / "results",
    )
    parser.add_argument(
        "--prepared",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "paper_daily.csv",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    daily = prepare_daily_data(args.btc, args.cdd)
    result = backtest_paper_rules(daily)
    summary = strategy_summary(result)
    trades = extract_trades(result)
    cvdd = paper_cvdd_results(daily)
    monte_carlo = cvdd_monte_carlo(daily, seed=args.seed)

    args.output.mkdir(parents=True, exist_ok=True)
    args.prepared.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(args.prepared, index=False)
    result.to_csv(args.output / "daily_backtest.csv", index=False)
    summary.to_csv(args.output / "strategy_summary.csv", index=False)
    trades.to_csv(args.output / "threshold_trades.csv", index=False)
    cvdd.to_csv(args.output / "paper_cvdd_trades.csv", index=False)
    monte_carlo.to_csv(args.output / "cvdd_monte_carlo.csv", index=False)
    performance = write_portfolio_report(
        build_portfolio_ledgers(result),
        args.output,
        benchmark="buy_hold",
        groups={
            "NUPL strategies vs buy-and-hold": ("nupl_1", "nupl_2", "nupl_3"),
            "MVRV Z-score strategies vs buy-and-hold": ("mvrv_1", "mvrv_2", "mvrv_3"),
            "CVDD entry + MVRV 6 exit vs buy-and-hold": HYBRID_STRATEGIES,
        },
        labels={
            "buy_hold": "Buy & Hold",
            "nupl_1": "NUPL 1",
            "nupl_2": "NUPL 2",
            "nupl_3": "NUPL 3",
            "mvrv_1": "MVRV 1",
            "mvrv_2": "MVRV 2",
            "mvrv_3": "MVRV 3",
            "cvdd_mvrv_6": "Calculated CVDD ±1%",
            "paper_cvdd_entries_mvrv_6": "Paper CVDD entry dates",
        },
        title="Bitcoin Strategy Wealth Paths, 2013–2025",
    )
    save_hybrid_signal_plot(result, args.output / "hybrid_signals.png")

    print(summary[["strategy", "cumulative_log_return", "sharpe_ratio"]].to_string(index=False))
    print("\nShared portfolio report:")
    print(
        performance[
            ["strategy", "ending_portfolio_value", "maximum_drawdown", "sharpe_ratio"]
        ].to_string(index=False)
    )
    print(f"\nComputed CVDD +/-1% entries: {int(result['cvdd_entry'].sum())}")
    print(f"Outputs: {args.output.resolve()}")


if __name__ == "__main__":
    main()
