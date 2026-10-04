"""Existing supplied-threshold gates; calibration fitting stays in research."""


def _gate(features: dict, plug: str, thresholds: dict[str, float]) -> bool | None:
    if plug in ("vwap_persistence", "value_edge_acceptance", "atr_activation"):
        score = features[plug]
        return None if score is None else score >= thresholds[plug]
    if plug == "fee_stop_burden":
        score = features[plug]
        return None if score is None else score <= thresholds[plug]
    effort_key = ("same_clock_relative_volume" if plug == "volume_without_result"
                  else "directional_delta_imbalance")
    effort, result = features[effort_key], features["directional_result_atr"]
    if effort is None or result is None:
        return None
    # Only reject high effort that fails to produce commensurate price result.
    return not (effort >= thresholds[effort_key]
                and result < thresholds["directional_result_atr"])
