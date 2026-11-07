from __future__ import annotations

import pandas as pd

from indicator.vwap import vwap, vwap_bands, vwap_std


def test_vwap_resets_by_group() -> None:
    result = vwap(pd.Series([10, 20, 100]), pd.Series([1, 3, 2]), pd.Series(["a", "a", "b"]))

    assert result.round(6).to_list() == [10.0, 17.5, 100.0]


def test_vwap_std_and_bands_use_caller_supplied_group_and_multiplier() -> None:
    price = pd.Series([10.0, 20.0, 100.0])
    volume = pd.Series([1.0, 3.0, 2.0])
    group = pd.Series(["a", "a", "b"])
    anchor = vwap(price, volume, group)
    std = vwap_std(price, volume, anchor, group)
    bands = vwap_bands(anchor, std, 2.0)

    assert anchor.round(6).to_list() == [10.0, 17.5, 100.0]
    assert std.round(6).to_list() == [0.0, 2.165064, 0.0]
    assert bands["vwap_upper"].round(6).to_list() == [10.0, 21.830127, 100.0]
    assert bands["vwap_lower"].round(6).to_list() == [10.0, 13.169873, 100.0]
