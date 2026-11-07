from __future__ import annotations

import pandas as pd
import pytest

from model_development.models.bitcoin_onchain_cycles.data_sources import parse_treasury_yield


def test_parse_treasury_yield_converts_percent_and_skips_missing_rows() -> None:
    text = (
        "observation_date,DTB3\n"
        "2013-12-02,0.05\n"
        "2013-12-03,.\n"
        "2013-12-04,4.20\n"
    )

    rates = parse_treasury_yield(text)

    assert rates.index.tolist() == [
        pd.Timestamp("2013-12-02"),
        pd.Timestamp("2013-12-04"),
    ]
    assert rates.tolist() == pytest.approx([0.0005, 0.042])


def test_parse_treasury_yield_sorts_and_deduplicates() -> None:
    text = (
        "observation_date,DTB3\n"
        "2014-01-03,0.10\n"
        "2014-01-02,0.09\n"
        "2014-01-02,0.09\n"
    )

    rates = parse_treasury_yield(text)

    assert rates.tolist() == pytest.approx([0.0009, 0.001])
    assert rates.index.is_monotonic_increasing


@pytest.mark.parametrize(
    "text",
    [
        "observation_date\n2013-12-02\n",
        "observation_date,DTB3\n2013-12-02,.\n",
        "observation_date,DTB3\n",
    ],
)
def test_parse_treasury_yield_rejects_unusable_payloads(text: str) -> None:
    with pytest.raises(ValueError):
        parse_treasury_yield(text)
