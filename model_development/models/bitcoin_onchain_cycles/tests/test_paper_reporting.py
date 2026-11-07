"""Tests for the paper-replication reporting fixes (audit findings C1-C5)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model_development.phase_3.portfolio_report import calculate_portfolio_metrics
from model_development.models.bitcoin_onchain_cycles.run_backtest import (
    PAPER_REPLICATION_CAPITAL,
    build_portfolio_ledgers,
)
from model_development.models.bitcoin_onchain_cycles.strategy import (
    EX_POST_ENTRY_STRATEGIES,
    PAPER_CVDD_TRADES,
    backtest_paper_rules,
    cvdd_monte_carlo,
    extract_trades,
    paper_cvdd_results,
    strategy_summary,
)


def _long_history() -> pd.DataFrame:
    """Daily rows covering every paper CVDD trade date."""
    dates = pd.date_range("2015-01-01", "2025-04-12", freq="D")
    index = np.arange(len(dates))
    close = 250.0 * np.exp(0.0009 * index) * (1.0 + 0.4 * np.sin(index / 70.0))
    return pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "nupl": 0.3 + 0.1 * np.sin(index / 50.0),
            "mvrv_zscore": 1.5 * np.sin(index / 90.0),
            "cvdd": close * 1.5,
        }
    )


def _short_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=8, freq="D"),
            "close": [100.0, 110.0, 121.0, 118.0, 120.0, 130.0, 121.0, 124.0],
            "nupl": [0.3] * 8,
            "mvrv_zscore": [-0.3, -0.3, 1.0, 1.0, 5.0, 5.0, -0.3, 1.0],
            "cvdd": [90.0] * 8,
        }
    )


def test_extract_trades_does_not_depend_on_the_incoming_index() -> None:
    result = backtest_paper_rules(_short_history())
    shifted = result.copy()
    shifted.index = np.arange(500, 500 + len(shifted))

    assert extract_trades(shifted).equals(extract_trades(result))
    trades = extract_trades(shifted)
    closed = trades.loc[trades["status"].eq("closed")]
    assert not closed.empty
    assert (closed["holding_days"] >= 1).all()


def test_strategy_summary_flags_ex_post_entries() -> None:
    summary = strategy_summary(backtest_paper_rules(_long_history()))
    flagged = set(summary.loc[summary["ex_post_entries"], "strategy"])

    assert flagged == set(EX_POST_ENTRY_STRATEGIES)
    assert "cvdd_mvrv_6" not in flagged


def test_monte_carlo_separates_beat_share_from_p_value() -> None:
    table = cvdd_monte_carlo(_long_history(), simulations=25)

    assert "beat_random_share" in table.columns
    assert "log_return_p_value" not in table.columns
    assert table["beat_random_share"].between(0.0, 1.0).all()
    assert (table["beat_random_share"] + table["p_value"]).to_numpy() == pytest.approx(
        np.ones(len(table))
    )
    assert (
        table["annualized_beat_random_share"] + table["annualized_p_value"]
    ).to_numpy() == pytest.approx(np.ones(len(table)))


def test_paper_cvdd_results_labels_both_ends_as_ex_post() -> None:
    table = paper_cvdd_results(_long_history())

    assert table["ex_post_entries"].all()
    assert table["exit_source"].tolist() == [
        "ex_post_cycle_peak",
        "ex_post_cycle_peak",
        "sample_end",
    ]
    assert "lookahead_exit" not in table.columns


def test_portfolio_ledgers_are_denominated_in_the_stated_capital() -> None:
    result = backtest_paper_rules(_long_history())
    ledgers = build_portfolio_ledgers(result)

    for name, ledger in ledgers.items():
        assert ledger["equity"].iloc[0] == pytest.approx(PAPER_REPLICATION_CAPITAL)
        assert ledger["net_flow"].sum() == pytest.approx(PAPER_REPLICATION_CAPITAL)
        expected = float(np.exp(result[f"{name}_log_return"].sum())) * PAPER_REPLICATION_CAPITAL
        assert ledger["equity"].iloc[-1] == pytest.approx(expected)

    metrics = calculate_portfolio_metrics(ledgers["buy_hold"])
    assert metrics["ending_portfolio_value"] > 0
    assert metrics["contributions"] == pytest.approx(PAPER_REPLICATION_CAPITAL)
    # Scaling cannot change a price ratio, so the average paid stays a price.
    assert metrics["average_btc_acquisition_price"] == pytest.approx(
        float(result["close"].iloc[0])
    )


def test_paper_cvdd_trade_dates_exist_in_the_fixture() -> None:
    dates = set(_long_history()["date"])
    for _, entry, exit_ in PAPER_CVDD_TRADES:
        assert pd.Timestamp(entry) in dates
        assert pd.Timestamp(exit_) in dates
