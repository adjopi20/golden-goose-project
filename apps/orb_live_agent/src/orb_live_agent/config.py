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
    rules_file: Path
    max_ai_calls_per_day: int
    session_timezone: str
    ny_open_time: str
    setup_cutoff_time: str
    overnight_start_time: str
    pre_ny_start_time: str
    volume_profile_bins: int
    bubble_lookback_min_trades: int
    bubble_percentile: float
    bubble_min_qty: float | None
    bubble_min_notional: float | None
    paper_initial_equity: float
    paper_risk_fraction: float
    paper_fee_bps: float
    paper_slippage_bps: float
    paper_tp1_r: float
    paper_tp1_fraction: float
    paper_runner_trail_tp1_fraction: float
    audit_kline_1m: bool


def load_config() -> AgentConfig:
    _load_env()
    return AgentConfig(
        symbol=_get_str("SYMBOL", "AVAXUSDC").upper(),
        stream_base=_get_str("BINANCE_STREAM_BASE", "wss://stream.binance.com:9443/stream"),
        log_dir=Path(_get_str("LOG_DIR", "apps/orb_live_agent/data")),
        ai_provider=_get_str("AI_PROVIDER", "stub").lower(),
        ai_live_calls_enabled=_get_bool("AI_LIVE_CALLS_ENABLED"),
        ai_base_url=_get_str("AI_BASE_URL", "https://api.deepseek.com"),
        ai_model=_get_str("AI_MODEL", "deepseek-v4-pro"),
        rules_file=Path(_get_str("RULES_FILE", "models/orb/model/checkpoint_ai_assisted_main_benchmark.md")),
        max_ai_calls_per_day=_get_int("MAX_AI_CALLS_PER_DAY", 150),
        session_timezone=_get_str("SESSION_TIMEZONE", "America/New_York"),
        ny_open_time=_get_str("NY_OPEN_TIME", "09:30"),
        setup_cutoff_time=_get_str("SETUP_CUTOFF_TIME", "17:30"),
        overnight_start_time=_get_str("OVERNIGHT_START_TIME", "17:30"),
        pre_ny_start_time=_get_str("PRE_NY_START_TIME", "01:30"),
        volume_profile_bins=_get_int("VOLUME_PROFILE_BINS", 50),
        bubble_lookback_min_trades=_get_int("BUBBLE_LOOKBACK_MIN_TRADES", 100),
        bubble_percentile=_get_float("BUBBLE_PERCENTILE", 0.95) or 0.95,
        bubble_min_qty=_get_float("BUBBLE_MIN_QTY"),
        bubble_min_notional=_get_float("BUBBLE_MIN_NOTIONAL"),
        paper_initial_equity=_get_float("PAPER_INITIAL_EQUITY", 1000.0) or 1000.0,
        paper_risk_fraction=_get_float("PAPER_RISK_FRACTION", 0.05) or 0.05,
        paper_fee_bps=_get_float("PAPER_FEE_BPS", 4.0) or 0.0,
        paper_slippage_bps=_get_float("PAPER_SLIPPAGE_BPS", 5.0) or 0.0,
        paper_tp1_r=_get_float("PAPER_TP1_R", 4.0) or 4.0,
        paper_tp1_fraction=_get_float("PAPER_TP1_FRACTION", 0.5) or 0.5,
        paper_runner_trail_tp1_fraction=_get_float("PAPER_RUNNER_TRAIL_TP1_FRACTION", 0.5) or 0.5,
        audit_kline_1m=_get_bool("AUDIT_KLINE_1M"),
    )
