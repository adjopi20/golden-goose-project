from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def _load_env() -> None:
    if load_dotenv is None:
        return
    here = Path(__file__).resolve()
    env_path = here.parents[2] / ".env"
    load_dotenv(env_path)


def _get_float(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name, "")
    if value == "":
        return default
    return float(value)


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name, "")
    return default if value == "" else int(value)


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "")
    if value == "":
        return default
    return value.lower() in {"1", "true", "yes", "y"}


def _get_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class AgentConfig:
    symbol: str
    stream_base: str
    log_dir: Path
    ai_provider: str
    ai_live_calls_enabled: bool
    ai_base_url: str
    ai_model: str
    ai_max_tokens: int
    ai_timeout_seconds: int
    rules_file: Path
    max_ai_calls_per_day: int
    session_timezone: str
    ny_open_time: str
    setup_cutoff_time: str
    overnight_start_time: str
    pre_ny_start_time: str
    orb_session_start_time: str
    orb_entry_start_time: str
    volume_profile_bins: int
    bubble_lookback_min_trades: int
    bubble_percentile: float
    bubble_min_qty: float | None
    bubble_min_notional: float | None
    orb_entry_window_minutes: int
    orb_min_volume_expansion_ratio: float | None
    orb_min_supportive_bubble_qty_ratio: float | None
    orb_min_candidate_body_ratio: float
    orb_short_max_close_position: float
    orb_long_min_close_position: float
    orb_require_directional_delta: bool
    orb_min_preentry_delta_ratio: float
    orb_preentry_delta_lookback_minutes: int
    orb_opposite_touch_policy: str
    orb_direct_min_body_ratio: float
    orb_direct_short_max_close_position: float
    orb_direct_long_min_close_position: float
    orb_direct_min_range_ratio: float
    orb_direct_min_delta_ratio: float
    orb_stop_model: str
    paper_initial_equity: float
    paper_risk_fraction: float
    paper_fee_bps: float
    paper_slippage_bps: float
    paper_min_stop_risk_pct: float
    paper_max_stop_risk_pct: float
    paper_tp1_r: float
    paper_tp1_fraction: float
    paper_runner_trail_tp1_fraction: float
    paper_exit_mode: str
    paper_trail_activation_r: float
    paper_trail_distance_r: float
    paper_protection_enabled: bool
    paper_protection_activation_r: float
    paper_protection_stop_r: float
    paper_protection_fraction: float
    paper_max_hold_exit_time: str
    audit_kline_1m: bool


def load_config() -> AgentConfig:
    _load_env()
    tp1_r = _get_float("PAPER_TP1_R", 4.0) or 4.0
    legacy_trail_fraction = _get_float("PAPER_RUNNER_TRAIL_TP1_FRACTION", 0.5) or 0.5
    trail_distance_r = _get_float("PAPER_TRAIL_DISTANCE_R")
    exit_mode = _get_str("PAPER_EXIT_MODE", "tp1_trail").lower()
    if exit_mode not in {"tp1_trail", "trail_only"}:
        raise ValueError("PAPER_EXIT_MODE must be tp1_trail or trail_only")
    opposite_touch_policy = _get_str("ORB_OPPOSITE_TOUCH_POLICY", "strict").lower()
    if opposite_touch_policy not in {"strict", "displacement_override", "ignore"}:
        raise ValueError("ORB_OPPOSITE_TOUCH_POLICY must be strict, displacement_override, or ignore")
    stop_model = _get_str("ORB_STOP_MODEL", "opposite_extreme").lower()
    if stop_model not in {"opposite_extreme", "poc", "opposite_value_area"}:
        raise ValueError("ORB_STOP_MODEL must be opposite_extreme, poc, or opposite_value_area")
    return AgentConfig(
        symbol=_get_str("SYMBOL", "AVAXUSDC").upper(),
        stream_base=_get_str("BINANCE_STREAM_BASE", "wss://stream.binance.com:9443/stream"),
        log_dir=Path(_get_str("LOG_DIR", "apps/orb_live_agent/data")),
        ai_provider=_get_str("AI_PROVIDER", "stub").lower(),
        ai_live_calls_enabled=_get_bool("AI_LIVE_CALLS_ENABLED"),
        ai_base_url=_get_str("AI_BASE_URL", "https://api.deepseek.com"),
        ai_model=_get_str("AI_MODEL", "deepseek-v4-pro"),
        ai_max_tokens=_get_int("AI_MAX_TOKENS", 384000),
        ai_timeout_seconds=_get_int("AI_TIMEOUT_SECONDS", 300),
        rules_file=Path(_get_str("RULES_FILE", "apps/orb_live_agent/rules/trend_following_orb.md")),
        max_ai_calls_per_day=_get_int("MAX_AI_CALLS_PER_DAY", 150),
        session_timezone=_get_str("SESSION_TIMEZONE", "America/New_York"),
        ny_open_time=_get_str("NY_OPEN_TIME", "09:30"),
        setup_cutoff_time=_get_str("SETUP_CUTOFF_TIME", "17:30"),
        overnight_start_time=_get_str("OVERNIGHT_START_TIME", "17:30"),
        pre_ny_start_time=_get_str("PRE_NY_START_TIME", "01:30"),
        orb_session_start_time=_get_str("ORB_SESSION_START_TIME", _get_str("NY_OPEN_TIME", "09:30")),
        orb_entry_start_time=_get_str("ORB_ENTRY_START_TIME", "09:45"),
        volume_profile_bins=_get_int("VOLUME_PROFILE_BINS", 50),
        bubble_lookback_min_trades=_get_int("BUBBLE_LOOKBACK_MIN_TRADES", 100),
        bubble_percentile=_get_float("BUBBLE_PERCENTILE", 0.95) or 0.95,
        bubble_min_qty=_get_float("BUBBLE_MIN_QTY"),
        bubble_min_notional=_get_float("BUBBLE_MIN_NOTIONAL"),
        orb_entry_window_minutes=_get_int("ORB_ENTRY_WINDOW_MINUTES", 30),
        orb_min_volume_expansion_ratio=_get_float("ORB_MIN_VOLUME_EXPANSION_RATIO", 2.0),
        orb_min_supportive_bubble_qty_ratio=_get_float("ORB_MIN_SUPPORTIVE_BUBBLE_QTY_RATIO"),
        orb_min_candidate_body_ratio=_get_float("ORB_MIN_CANDIDATE_BODY_RATIO", 0.35) or 0.0,
        orb_short_max_close_position=_get_float("ORB_SHORT_MAX_CLOSE_POSITION", 0.45) or 0.0,
        orb_long_min_close_position=_get_float("ORB_LONG_MIN_CLOSE_POSITION", 0.55) or 0.0,
        orb_require_directional_delta=_get_bool("ORB_REQUIRE_DIRECTIONAL_DELTA", True),
        orb_min_preentry_delta_ratio=_get_float("ORB_MIN_PREENTRY_DELTA_RATIO", 0.05) or 0.0,
        orb_preentry_delta_lookback_minutes=_get_int("ORB_PREENTRY_DELTA_LOOKBACK_MINUTES", 15),
        orb_opposite_touch_policy=opposite_touch_policy,
        orb_direct_min_body_ratio=_get_float("ORB_DIRECT_MIN_BODY_RATIO", 0.65) or 0.0,
        orb_direct_short_max_close_position=_get_float("ORB_DIRECT_SHORT_MAX_CLOSE_POSITION", 0.30) or 0.0,
        orb_direct_long_min_close_position=_get_float("ORB_DIRECT_LONG_MIN_CLOSE_POSITION", 0.70) or 0.0,
        orb_direct_min_range_ratio=_get_float("ORB_DIRECT_MIN_RANGE_RATIO", 1.5) or 0.0,
        orb_direct_min_delta_ratio=_get_float("ORB_DIRECT_MIN_DELTA_RATIO", 0.85) or 0.0,
        orb_stop_model=stop_model,
        paper_initial_equity=_get_float("PAPER_INITIAL_EQUITY", 1000.0) or 1000.0,
        paper_risk_fraction=_get_float("PAPER_RISK_FRACTION", 0.05) or 0.05,
        paper_fee_bps=_get_float("PAPER_FEE_BPS", 4.0) or 0.0,
        paper_slippage_bps=_get_float("PAPER_SLIPPAGE_BPS", 5.0) or 0.0,
        paper_min_stop_risk_pct=_get_float("PAPER_MIN_STOP_RISK_PCT", 0.0015) or 0.0015,
        paper_max_stop_risk_pct=_get_float("PAPER_MAX_STOP_RISK_PCT", 0.025) or 0.025,
        paper_tp1_r=tp1_r,
        paper_tp1_fraction=_get_float("PAPER_TP1_FRACTION", 0.5) or 0.5,
        paper_runner_trail_tp1_fraction=legacy_trail_fraction,
        paper_exit_mode=exit_mode,
        paper_trail_activation_r=_get_float("PAPER_TRAIL_ACTIVATION_R", tp1_r) or tp1_r,
        paper_trail_distance_r=trail_distance_r if trail_distance_r is not None else tp1_r * legacy_trail_fraction,
        paper_protection_enabled=_get_bool("PAPER_PROTECTION_ENABLED", True),
        paper_protection_activation_r=_get_float("PAPER_PROTECTION_ACTIVATION_R", 1.0) or 1.0,
        paper_protection_stop_r=_get_float("PAPER_PROTECTION_STOP_R", 0.0) or 0.0,
        paper_protection_fraction=_get_float("PAPER_PROTECTION_FRACTION", 0.0) or 0.0,
        paper_max_hold_exit_time=_get_str("PAPER_MAX_HOLD_EXIT_TIME", _get_str("PRE_NY_START_TIME", "01:30")),
        audit_kline_1m=_get_bool("AUDIT_KLINE_1M"),
    )
