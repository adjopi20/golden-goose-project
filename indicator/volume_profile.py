import json
import numpy as np
import pandas as pd


HVN_THRESHOLD = 0.65
LVN_THRESHOLD = 0.20
SMOOTHING_WINDOW = 3


def _round_float(value: float, ndigits: int) -> float:
    """
    Deterministically round numeric output and force native Python float type.
    """
    return float(round(float(value), ndigits))


def _format_export_volume_profile(volume_profile: list[dict]) -> list[dict]:
    """
    Apply output-only precision normalization for exported volume profile bins.
    Internal computation values remain full precision.
    """
    return [
        {
            "bin_index": int(bin_row["bin_index"]),
            "bin_low": _round_float(bin_row["bin_low"], 2),
            "bin_high": _round_float(bin_row["bin_high"], 2),
            "buy_volume": _round_float(bin_row["buy_volume"], 3),
            "sell_volume": _round_float(bin_row["sell_volume"], 3),
            "total_volume": _round_float(bin_row["total_volume"], 3),
            "delta": _round_float(bin_row["delta"], 3),
        }
        for bin_row in volume_profile
    ]


def _format_export_regions(regions: list[dict]) -> list[dict]:
    """
    Apply output-only precision normalization for regime segmentation regions.
    """
    return [
        {
            "start_bin": int(region["start_bin"]),
            "end_bin": int(region["end_bin"]),
            "dominant_bin": int(region["dominant_bin"]),
            "dominant_price": _round_float(region["dominant_price"], 2),
            "max_density": _round_float(region["max_density"], 4),
            "mean_density": _round_float(region["mean_density"], 4),
        }
        for region in regions
    ]


def _smooth_profile_volumes(volume_profile: list[dict]) -> np.ndarray:
    """
    Apply light rolling mean smoothing to reduce noisy bin fluctuations.

    This is NOT intended to distort topology.
    Only to stabilize adjacent participation density.
    """
    raw = np.array(
        [float(bin_data["total_volume"]) for bin_data in volume_profile],
        dtype=np.float64,
    )

    if len(raw) == 0:
        return raw

    if SMOOTHING_WINDOW <= 1:
        return raw

    kernel = np.ones(SMOOTHING_WINDOW, dtype=np.float64) / SMOOTHING_WINDOW

    padded = np.pad(
        raw,
        pad_width=SMOOTHING_WINDOW // 2,
        mode="edge",
    )

    smoothed = np.convolve(padded, kernel, mode="valid")

    return smoothed


def _normalize_profile_volumes(smoothed_volumes: np.ndarray) -> np.ndarray:
    """
    Normalize profile volumes into density space [0, 1].

    Density represents relative participation strength within session.
    """
    if len(smoothed_volumes) == 0:
        return smoothed_volumes

    max_volume = np.max(smoothed_volumes)

    if max_volume <= 0.0:
        return np.zeros_like(smoothed_volumes)

    return smoothed_volumes / max_volume


def _classify_density_regime(density: float) -> str:
    """
    Classify normalized participation density into regime type.
    """
    if density >= HVN_THRESHOLD:
        return "hvn"

    if density <= LVN_THRESHOLD:
        return "lvn"

    return "neutral"


def _validate_lvn_regions(
    lvn_regions: list[dict],
    normalized: np.ndarray,
    n_bins: int,
) -> list[dict]:
    """
    Validate LVN regions using structural embedding constraints.

    Rules:
    - Reject edge tails (start at 0 or end at n_bins - 1)
    - Keep only structurally embedded LVNs where both neighboring
      participation densities are materially stronger than LVN density.
    """
    valid_regions = []

    for region in lvn_regions:
        start_bin = int(region["start_bin"])
        end_bin = int(region["end_bin"])
        current_density = float(region["mean_density"])

        # Mandatory edge-tail rejection
        if start_bin == 0 or end_bin == n_bins - 1:
            continue

        left_density = float(normalized[start_bin - 1])
        right_density = float(normalized[end_bin + 1])

        if (
            left_density > current_density * 1.3
            and right_density > current_density * 1.3
        ):
            valid_regions.append(region)

    return valid_regions


def _segment_profile_regimes(
    volume_profile: list[dict],
) -> dict:
    """
    Segment volume profile into contiguous participation distributions.

    This replaces:
    - local extrema detection
    - expansion logic
    - prominence filtering
    - iterative merge

    The profile is now treated as:
    # participation density topology
    instead of:
    # extrema topology
    """
    if not volume_profile:
        return {
            "hvn_regions": [],
            "lvn_regions": [],
        }

    smoothed = _smooth_profile_volumes(volume_profile)

    normalized = _normalize_profile_volumes(smoothed)

    classified = [
        _classify_density_regime(float(density))
        for density in normalized
    ]

    hvn_regions = []
    lvn_regions = []

    current_regime = None
    start_idx = None

    for i, regime in enumerate(classified):
        if regime == "neutral":
            if current_regime is not None:
                _append_distribution_region(
                    regions=hvn_regions if current_regime == "hvn" else lvn_regions,
                    regime=current_regime,
                    start_idx=start_idx,
                    end_idx=i - 1,
                    volume_profile=volume_profile,
                    normalized=normalized,
                )

                current_regime = None
                start_idx = None

            continue

        if current_regime is None:
            current_regime = regime
            start_idx = i
            continue

        if regime != current_regime:
            _append_distribution_region(
                regions=hvn_regions if current_regime == "hvn" else lvn_regions,
                regime=current_regime,
                start_idx=start_idx,
                end_idx=i - 1,
                volume_profile=volume_profile,
                normalized=normalized,
            )

            current_regime = regime
            start_idx = i

    if current_regime is not None:
        _append_distribution_region(
            regions=hvn_regions if current_regime == "hvn" else lvn_regions,
            regime=current_regime,
            start_idx=start_idx,
            end_idx=len(classified) - 1,
            volume_profile=volume_profile,
            normalized=normalized,
        )

    lvn_regions = _validate_lvn_regions(
        lvn_regions=lvn_regions,
        normalized=normalized,
        n_bins=len(volume_profile),
    )

    return {
        "hvn_regions": hvn_regions,
        "lvn_regions": lvn_regions,
    }


def _append_distribution_region(
    regions: list[dict],
    regime: str,
    start_idx: int,
    end_idx: int,
    volume_profile: list[dict],
    normalized: np.ndarray,
) -> None:
    """
    Append contiguous participation distribution region.
    """
    region_bins = volume_profile[start_idx : end_idx + 1]

    region_volumes = [
        float(bin_data["total_volume"])
        for bin_data in region_bins
    ]

    dominant_local_idx = int(np.argmax(region_volumes))

    dominant_bin_idx = start_idx + dominant_local_idx

    dominant_bin = volume_profile[dominant_bin_idx]

    dominant_price = (
        float(dominant_bin["bin_low"]) +
        float(dominant_bin["bin_high"])
    ) / 2.0

    region = {
        "start_bin": int(start_idx),
        "end_bin": int(end_idx),
        "dominant_bin": int(dominant_bin_idx),
        "dominant_price": float(dominant_price),
        "max_density": float(np.max(normalized[start_idx : end_idx + 1])),
        "mean_density": float(np.mean(normalized[start_idx : end_idx + 1])),
    }

    regions.append(region)


def _compute_poc(volume_profile: list[dict]) -> dict:
    """
    Compute Point of Control (POC) from session volume profile.

    POC is the bin with highest total volume. In ties, the lowest bin_index is used
    to keep behavior deterministic.
    """
    if not volume_profile:
        raise ValueError("volume_profile must not be empty")

    poc_bin = max(volume_profile, key=lambda b: (b["total_volume"], -b["bin_index"]))
    poc_price = (poc_bin["bin_low"] + poc_bin["bin_high"]) / 2.0

    return {
        "poc_bin_index": int(poc_bin["bin_index"]),
        "poc_price": float(poc_price),
        "poc_volume": float(poc_bin["total_volume"]),
    }


def _compute_value_area_70(volume_profile: list[dict], poc_bin_index: int) -> dict:
    """
    Compute 70% Value Area using outward expansion from POC.

    Algorithm:
    1) Start from POC bin
    2) Expand outward
    3) At each step, compare neighboring bin volumes and include the larger first
    4) Stop when cumulative included volume >= 70% of total session volume

    VAL = lower edge of lowest included bin
    VAH = upper edge of highest included bin
    """
    if not volume_profile:
        raise ValueError("volume_profile must not be empty")

    total_volume = float(sum(bin_row["total_volume"] for bin_row in volume_profile))
    target_volume = 0.7 * total_volume

    included_bins = {int(poc_bin_index)}
    cumulative_volume = float(volume_profile[poc_bin_index]["total_volume"])

    left = poc_bin_index - 1
    right = poc_bin_index + 1

    while cumulative_volume < target_volume and (left >= 0 or right < len(volume_profile)):
        left_vol = volume_profile[left]["total_volume"] if left >= 0 else -1.0
        right_vol = volume_profile[right]["total_volume"] if right < len(volume_profile) else -1.0

        if left_vol >= right_vol:
            chosen = left
            left -= 1
        else:
            chosen = right
            right += 1

        if chosen < 0 or chosen >= len(volume_profile):
            continue

        included_bins.add(int(chosen))
        cumulative_volume += float(volume_profile[chosen]["total_volume"])

    low_idx = min(included_bins)
    high_idx = max(included_bins)

    return {
        "val": float(volume_profile[low_idx]["bin_low"]),
        "vah": float(volume_profile[high_idx]["bin_high"]),
    }


def build_volume_profile(trades_df: pd.DataFrame, n_bins: int = 50) -> dict:
    """
    Build one session volume profile from already filtered trades.
    """
    required_cols = {"timestamp", "price", "qty", "is_buyer_maker"}
    missing = sorted(required_cols - set(trades_df.columns))
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    if trades_df.empty:
        raise ValueError("trades_df must not be empty")
    if n_bins <= 0:
        raise ValueError("n_bins must be > 0")

    session_low = float(trades_df["price"].min())
    session_high = float(trades_df["price"].max())
    price_range = session_high - session_low
    bin_width = price_range / n_bins if price_range != 0 else 0.0

    if bin_width == 0.0:
        bin_index = np.zeros(len(trades_df), dtype=int)
    else:
        raw_idx = np.floor((trades_df["price"].to_numpy() - session_low) / bin_width).astype(int)
        bin_index = np.clip(raw_idx, 0, n_bins - 1)

    is_buyer_maker_arr = trades_df["is_buyer_maker"].astype(bool).to_numpy()
    qty_arr = trades_df["qty"].to_numpy(dtype=np.float64)
    buy_volume_arr = np.where(~is_buyer_maker_arr, qty_arr, 0.0)
    sell_volume_arr = np.where(is_buyer_maker_arr, qty_arr, 0.0)

    aggregation_df = pd.DataFrame({
        "bin_index": bin_index,
        "buy_volume": buy_volume_arr,
        "sell_volume": sell_volume_arr,
    })
    volume_at_bin = aggregation_df.groupby("bin_index", sort=True).sum().reindex(range(n_bins), fill_value=0.0)

    volume_profile = []
    for i in range(n_bins):
        bin_low = session_low + i * bin_width
        bin_high = session_low + (i + 1) * bin_width
        bin_data = volume_at_bin.loc[i]
        buy_volume = float(bin_data["buy_volume"])
        sell_volume = float(bin_data["sell_volume"])
        total_volume = buy_volume + sell_volume
        delta = buy_volume - sell_volume
        volume_profile.append(
            {
                "bin_index": int(i),
                "bin_low": float(bin_low),
                "bin_high": float(bin_high),
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "total_volume": total_volume,
                "delta": delta,
            }
        )

    poc_data = _compute_poc(volume_profile)
    value_area_data = _compute_value_area_70(volume_profile=volume_profile, poc_bin_index=poc_data["poc_bin_index"])
    segmentation = _segment_profile_regimes(volume_profile)

    return {
        "session_low": _round_float(session_low, 2),
        "session_high": _round_float(session_high, 2),
        "bin_width": _round_float(bin_width, 2),
        "bins": int(n_bins),
        "poc_bin_index": int(poc_data["poc_bin_index"]),
        "poc_price": _round_float(poc_data["poc_price"], 2),
        "poc_volume": _round_float(poc_data["poc_volume"], 3),
        "val": _round_float(value_area_data["val"], 2),
        "vah": _round_float(value_area_data["vah"], 2),
        "hvn_regions": _format_export_regions(segmentation["hvn_regions"]),
        "lvn_regions": _format_export_regions(segmentation["lvn_regions"]),
        "volume_profile": _format_export_volume_profile(volume_profile),
    }