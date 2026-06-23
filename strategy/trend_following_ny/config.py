from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class TrendFollowingNYConfig:
    symbol: str
    volume_profile_bins: int = 50
    value_area_pct: float = 0.70
    entry_model: str = "VALUE_AREA_ZONE"
    stop_model: str = "INSIDE_VALUE_PERCENTAGE"
    regime_mode: str = "ALL_SESSIONS_DIAGNOSTIC"
    eligible_asia_europe_context_combos: list[str] = field(default_factory=list)
    entry_zone_pct: float = 0.005
    inside_value_stop_pct: float = 0.005
    fee_pct_per_side: float = 0.0
    slippage_pct_per_side: float = 0.0
    entry_latency_ms: int = 0
    tp1_R: float = 2.0
    tp1_fraction: float = 0.50
    trailing_distance_R: float = 1.0
    max_trades_per_ny_session: int = 1
    confirmation_mode: str = "location_only_diagnostic"

    def __post_init__(self):
        if self.volume_profile_bins <= 0:
            raise ValueError("volume_profile_bins must be > 0")
        if not (0 < self.value_area_pct <= 1):
            raise ValueError("value_area_pct must satisfy 0 < value_area_pct <= 1")
        if self.entry_zone_pct < 0:
            raise ValueError("entry_zone_pct must be >= 0")
        if self.inside_value_stop_pct <= 0:
            raise ValueError("inside_value_stop_pct must be > 0")
        if self.fee_pct_per_side < 0 or self.slippage_pct_per_side < 0:
            raise ValueError("fees and slippage must be >= 0")
        if self.entry_latency_ms < 0:
            raise ValueError("entry_latency_ms must be >= 0")
        if not (0 < self.tp1_fraction <= 1):
            raise ValueError("tp1_fraction must satisfy 0 < tp1_fraction <= 1")
        if self.tp1_R <= 0 or self.trailing_distance_R <= 0:
            raise ValueError("tp1_R and trailing_distance_R must be > 0")
        if self.entry_model not in {"VALUE_AREA_ZONE", "SECOND_BREAKOUT"}:
            raise ValueError("Unsupported entry_model")
        if self.stop_model not in {"INSIDE_VALUE_PERCENTAGE", "BUBBLE_STOP", "NEARBY_HIGH_LOW"}:
            raise ValueError("Unsupported stop_model")
        if self.regime_mode not in {"ALL_SESSIONS_DIAGNOSTIC", "CONTEXT_ALLOWLIST"}:
            raise ValueError("Unsupported regime_mode")
        if self.confirmation_mode not in {"location_only_diagnostic"}:
            raise ValueError("Unsupported confirmation_mode")

    def to_dict(self) -> dict:
        return asdict(self)