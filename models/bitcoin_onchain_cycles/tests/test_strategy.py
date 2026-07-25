from __future__ import annotations

import pandas as pd
import pytest

from models.bitcoin_onchain_cycles.strategy import (
    backtest_paper_rules,
    prepare_daily_data,
)


def test_paper_thresholds_and_close_to_close_timing() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0, 110.0, 121.0, 118.0, 120.0],
            "nupl": [-0.1, 0.2, 0.70, 0.8, -0.1],
            "mvrv_zscore": [-0.3, 1.0, 5.0, 6.0, 7.0],
            "cvdd": [99.5, 109.5, 100.0, 118.5, 100.0],
        }
    )

    result = backtest_paper_rules(daily)

    assert result["nupl_1_action"].tolist() == ["buy", "hold", "sell", "cash", "buy"]
    assert result["nupl_2_action"].tolist() == ["buy", "hold", "sell", "cash", "buy"]
    assert result["nupl_3_position"].tolist() == [1, 1, 1, 0, 1]
    assert result["mvrv_1_action"].tolist() == ["buy", "hold", "sell", "cash", "cash"]
    assert result["mvrv_2_action"].tolist() == ["buy", "hold", "hold", "sell", "cash"]
    assert result["mvrv_3_action"].tolist() == ["buy", "hold", "hold", "hold", "sell"]
    assert result["nupl_1_return"].tolist() == pytest.approx([0.0, 0.1, 0.1, 0.0, 0.0])
    assert result["cvdd_entry"].tolist() == [True, False, False, True, False]


def test_prepare_daily_data_calculates_onchain_indicators(tmp_path) -> None:
    btc_path = tmp_path / "btc.csv"
    cdd_path = tmp_path / "data.tsv"
    pd.DataFrame(
        {
            "time": ["2009-01-03", "2009-01-04", "2009-01-05"],
            "PriceUSD": [1.0, 2.0, 3.0],
            "CapMrktCurUSD": [100.0, 200.0, 300.0],
            "CapMVRVCur": [2.0, 2.0, 2.0],
        }
    ).to_csv(btc_path, index=False)
    pd.DataFrame(
        {
            "Time": ["03.01.2009", "04.01.2009", "05.01.2009"],
            "CDD": [0.0, 10.0, 20.0],
        }
    ).to_csv(cdd_path, sep="\t", index=False)

    daily = prepare_daily_data(
        btc_path,
        cdd_path,
        start="2009-01-04",
        end="2009-01-05",
    )

    assert daily["nupl"].tolist() == pytest.approx([0.5, 0.5])
    assert daily["realized_cap"].tolist() == pytest.approx([100.0, 150.0])
    assert daily["cvdd"].tolist() == pytest.approx(
        [20.0 / 12_000_000, 80.0 / 18_000_000]
    )


def test_cvdd_entry_mvrv_exit_hybrid() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2015-01-14", "2015-01-15", "2015-01-16", "2015-01-17"]
            ),
            "close": [100.0, 110.0, 120.0, 115.0],
            "nupl": [0.1, 0.2, 0.3, 0.4],
            "mvrv_zscore": [-0.5, 2.0, 6.0, 5.0],
            "cvdd": [80.0, 80.0, 80.0, 80.0],
        }
    )

    result = backtest_paper_rules(daily)

    assert result["cvdd_mvrv_6_action"].tolist() == ["cash"] * 4
    assert result["paper_cvdd_entries_mvrv_6_action"].tolist() == [
        "buy",
        "hold",
        "sell",
        "cash",
    ]
    assert result["paper_cvdd_entries_mvrv_6_return"].tolist() == pytest.approx(
        [0.0, 0.1, 120 / 110 - 1, 0.0]
    )
