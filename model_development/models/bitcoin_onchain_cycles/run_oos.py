from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.phase_3.portfolio_report import write_portfolio_report
from model_development.models.bitcoin_onchain_cycles.allocation import (
    simulate_buy_and_hold,
    simulate_cvdd_confirmed_allocation,
    simulate_mvrv_allocation,
)
from model_development.models.bitcoin_onchain_cycles.data_sources import (
    download_coinmetrics,
    latest_cdd_date,
)
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_START, prepare_daily_data


MODEL_DIR = Path(__file__).resolve().parent
OOS_START = pd.Timestamp("2025-04-13")
def normalize_oos(ledger: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    result = ledger.loc[ledger["date"].ge(OOS_START)].copy().reset_index(drop=True)
    if result.empty:
        raise ValueError("ledger contains no OOS dates")
    scale = initial_capital / float(result["equity"].iloc[0])
    for column in (
        "equity",
        "cash",
        "btc_units",
        "traded_notional",
        "btc_bought",
        "purchase_notional",
    ):
        if column in result:
            result[column] = result[column].mul(scale)
    result["net_flow"] = 0.0
    result.loc[0, "net_flow"] = initial_capital
    return result


def write_findings(
    report: pd.DataFrame,
    ledgers: dict[str, pd.DataFrame],
    output: Path,
    requested_end: pd.Timestamp,
    btc_end: pd.Timestamp,
    cdd_end: pd.Timestamp | None,
    effective_end: pd.Timestamp,
) -> None:
    report = report.copy()
    report["transactions"] = report["strategy"].map(
        lambda name: int(ledgers[name]["transaction_count"].sum())
    )
    report["ending_cash"] = report["strategy"].map(
        lambda name: float(ledgers[name]["cash"].iloc[-1])
    )
    report["ending_btc_weight"] = report["strategy"].map(
        lambda name: float(ledgers[name]["actual_btc_weight"].iloc[-1])
    )
    report.to_csv(output / "performance_report.csv", index=False)

    lines = [
        "# Bitcoin On-Chain OOS Validation",
        "",
        f"- Frozen OOS start: `{OOS_START.date()}`.",
        f"- Requested end: `{requested_end.date()}`.",
        f"- Coin Metrics complete through: `{btc_end.date()}`.",
        (
            f"- Blockchair CDD complete through: `{cdd_end.date()}`."
            if cdd_end is not None
            else "- Blockchair CDD: not used (CVDD strategies skipped)."
        ),
        f"- Effective jointly complete end: `{effective_end.date()}`.",
        "- Initial OOS value: `$10,000` per strategy; no contributions, fees, or slippage.",
        "",
        "| Strategy | Ending value | Return | Max DD (magnitude) | Calmar | BTC weight | Transactions |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {row.strategy} | ${row.ending_portfolio_value:,.2f} | "
            f"{row.time_weighted_return:.2%} | {abs(row.maximum_drawdown):.2%} | "
            f"{row.calmar_ratio:.2f} | {row.ending_btc_weight:.0%} | "
            f"{row.transactions} |"
            for row in report.sort_values(
                "ending_portfolio_value", ascending=False
            ).itertuples()
        ],
        "",
        "Strategies were simulated from the paper start before the OOS slice was",
        "normalized, so open positions and indicator state carry across the frozen",
        "boundary without restarting the strategy.",
    ]
    (output / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh BTC on-chain inputs and run the frozen OOS comparison."
    )
    parser.add_argument(
        "--cdd",
        type=Path,
        default=None,
        help=(
            "Optional Coin Days Destroyed TSV. When omitted, CVDD strategies are "
            "skipped and no browser-based data source is required."
        ),
    )
    parser.add_argument(
        "--btc-output",
        type=Path,
        default=MODEL_DIR / "data" / "source" / "btc.csv",
    )
    parser.add_argument(
        "--prepared-output",
        type=Path,
        default=MODEL_DIR / "data" / "prepared" / "oos_daily.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MODEL_DIR / "research" / "oos_validation" / "results",
    )
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sample = pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-04-12", "2025-04-13", "2025-04-14"]),
                "equity": [5.0, 10.0, 11.0],
                "cash": [0.0, 0.0, 0.0],
                "btc_units": [0.05, 0.1, 0.1],
                "net_flow": [5.0, 0.0, 0.0],
            }
        )
        normalized = normalize_oos(sample, 1_000.0)
        assert normalized["equity"].tolist() == [1_000.0, 1_100.0]
        assert normalized["net_flow"].tolist() == [1_000.0, 0.0]
        print("self-test passed")
        return

    requested_end = pd.Timestamp(args.end)
    btc_end = download_coinmetrics(args.btc_output, args.end)
    cdd_end = latest_cdd_date(args.cdd) if args.cdd is not None else None
    effective_end = min(
        [requested_end, btc_end, *([cdd_end] if cdd_end is not None else [])]
    )
    if effective_end < OOS_START:
        raise ValueError("sources do not reach the frozen OOS start")

    daily = prepare_daily_data(
        args.btc_output,
        args.cdd,
        start=PAPER_START,
        end=effective_end.strftime("%Y-%m-%d"),
    )
    args.prepared_output.parent.mkdir(parents=True, exist_ok=True)
    daily.loc[daily["date"].ge(OOS_START)].to_csv(args.prepared_output, index=False)

    full_ledgers = {
        "buy_hold": simulate_buy_and_hold(
            daily, initial_capital=args.initial_capital
        ),
        "paper_mvrv_3": simulate_mvrv_allocation(
            daily,
            (1.0, 1.0, 0.0),
            initial_capital=args.initial_capital,
        ),
    }
    if args.cdd is not None:
        full_ledgers["cvdd_15_paper_exit"] = simulate_cvdd_confirmed_allocation(
            daily,
            initial_capital=args.initial_capital,
            cvdd_band=0.15,
            exit_mode="paper",
        )
    ledgers = {
        name: normalize_oos(ledger, args.initial_capital)
        for name, ledger in full_ledgers.items()
    }
    args.output.mkdir(parents=True, exist_ok=True)
    report = write_portfolio_report(
        ledgers,
        args.output,
        benchmark="buy_hold",
        groups={
            "Frozen OOS strategies": tuple(
                name for name in ledgers if name != "buy_hold"
            )
        },
        labels={
            "buy_hold": "Buy & Hold",
            "paper_mvrv_3": "Paper MVRV-3",
            "cvdd_15_paper_exit": "CVDD 15% + Paper Exit",
        },
        title="Bitcoin On-Chain Strategies: Frozen OOS",
    )
    pd.concat(
        [ledger.assign(strategy=name) for name, ledger in ledgers.items()],
        ignore_index=True,
    ).to_csv(args.output / "daily_ledgers.csv.gz", index=False, compression="gzip")
    write_findings(
        report,
        ledgers,
        args.output,
        requested_end,
        btc_end,
        cdd_end,
        effective_end,
    )
    print(
        report[
            [
                "strategy",
                "ending_portfolio_value",
                "time_weighted_return",
                "maximum_drawdown",
                "calmar_ratio",
            ]
        ].to_string(index=False)
    )
    print(f"\nEffective OOS end: {effective_end.date()}")
    print(f"Outputs: {args.output.resolve()}")


if __name__ == "__main__":
    main()
