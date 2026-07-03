from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from indicator.deep_trade import build_order_bubbles
from indicator.ohlcv import aggregate_trades_to_ohlcv, get_bucket_start
from indicator.volume_profile import build_volume_profile

from .config import AgentConfig

def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def parse_aggtrade(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": payload.get("e"),
        "event_time": int(payload.get("E", payload["T"])),
        "agg_trade_id": int(payload["a"]),
        "price": float(payload["p"]),
        "qty": float(payload["q"]),
        "first_trade_id": int(payload["f"]),
        "last_trade_id": int(payload["l"]),
        "timestamp": int(payload["T"]),
        "is_buyer_maker": bool(payload["m"]),
    }


@dataclass(frozen=True)
class ClosedMinute:
    candle: dict[str, Any]
    bubbles: list[dict[str, Any]]
    snapshot: dict[str, Any]
    trigger_reference_levels: dict[str, Any]


class LiveStateBuilder:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        try:
            self.session_tz = ZoneInfo(config.session_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Invalid SESSION_TIMEZONE={config.session_timezone!r}. "
                "Use a valid IANA timezone, for example America/New_York."
            ) from exc
        self.ny_open = _parse_hhmm(config.ny_open_time)
        self.cutoff = _parse_hhmm(config.setup_cutoff_time)
        self.overnight_start = _parse_hhmm(config.overnight_start_time)
        self.pre_ny_start = _parse_hhmm(config.pre_ny_start_time)
        self.current_bucket: datetime | None = None
        self.current_minute_trades: list[dict[str, Any]] = []
        self.current_minute_reference_levels: dict[str, Any] | None = None
        self.recent_trades: deque[dict[str, Any]] = deque()
        self.closed_candles: deque[dict[str, Any]] = deque(maxlen=500)
        self.extremes: dict[tuple[str, date], dict[str, Any]] = {}
        self.ny_first_15_trades: dict[date, list[dict[str, Any]]] = {}
        self.ny_first_15_profiles: dict[date, dict[str, Any]] = {}
        self.session_open_24h_profiles: dict[date, dict[str, Any]] = {}

    def push_trade(self, trade: dict[str, Any]) -> ClosedMinute | None:
        ts_ms = int(trade["timestamp"])
        bucket = get_bucket_start(ts_ms, "1m")
        closed = None
        if self.current_bucket is not None and bucket > self.current_bucket:
            closed = self._close_current_minute(
                self.current_minute_reference_levels or {},
            )
        if self.current_bucket is None or bucket > self.current_bucket:
            self.current_bucket = bucket
            self.current_minute_trades = []
            self.current_minute_reference_levels = self.build_trigger_reference_levels(ts_ms)

        self._drop_old_trades(ts_ms)
        self.recent_trades.append(trade)
        self._update_extremes(trade)
        self._update_ny_first_15(trade)
        self.current_minute_trades.append(trade)
        return closed

    def _close_current_minute(self, trigger_reference_levels: dict[str, Any]) -> ClosedMinute | None:
        if not self.current_minute_trades:
            return None
        candles = aggregate_trades_to_ohlcv(pd.DataFrame(self.current_minute_trades), self.config.symbol, "1m")
        if not candles:
            return None
        candle = candles[-1]
        bubbles = self._build_bubbles_for_closed_minute()
        self.closed_candles.append(candle)
        return ClosedMinute(candle=candle, bubbles=bubbles, snapshot=self.build_snapshot(candle), trigger_reference_levels=trigger_reference_levels)

    def build_snapshot(self, last_candle: dict[str, Any] | None = None) -> dict[str, Any]:
        now_ms = int(last_candle["timestamp_ms"]) if last_candle else (int(self.recent_trades[-1]["timestamp"]) if self.recent_trades else 0)
        target_day = self._target_session_day(now_ms)
        session_profile = self._session_open_24h_profile_for(target_day, now_ms)
        ny_15m_profile = self._ny_first_15m_profile_for(target_day, now_ms)
        return {
            "symbol": self.config.symbol,
            "snapshot_timestamp_ms": now_ms,
            "target_session_day": target_day.isoformat(),
            "session_timezone": self.config.session_timezone,
            "setup_observation_active": self.is_setup_observation_active(now_ms),
            "last_candle": last_candle,
            "recent_candles": list(self.closed_candles)[-30:],
            "session_extremes": {
                "pre_ny": self.extremes.get(("pre_ny", target_day)),
                "overnight": self.extremes.get(("overnight", target_day)),
                "previous_ny": self.extremes.get(("ny", target_day - timedelta(days=1))),
            },
            "previous_24h_profile_for_session": session_profile,
            "ny_first_15m_profile": ny_15m_profile,
        }

    def _profile_for(self, trades: list[dict[str, Any]]) -> dict[str, Any] | None:
        if len(trades) < 2:
            return None
        profile = build_volume_profile(pd.DataFrame(trades), n_bins=self.config.volume_profile_bins)
        total_volume = sum(float(row["total_volume"]) for row in profile.get("volume_profile", []))
        if total_volume > 0:
            profile["total_volume"] = total_volume
            profile["poc_volume_pct"] = float(profile["poc_volume"]) / total_volume
        if profile.get("val") is not None and profile.get("vah") is not None:
            profile["value_area_width"] = float(profile["vah"]) - float(profile["val"])
        return profile

    def build_trigger_reference_levels(self, timestamp_ms: int) -> dict[str, Any]:
        target_day = self._target_session_day(timestamp_ms)
        return copy.deepcopy({
            "session_extremes": {
                "pre_ny": self.extremes.get(("pre_ny", target_day)),
                "overnight": self.extremes.get(("overnight", target_day)),
                "previous_ny": self.extremes.get(("ny", target_day - timedelta(days=1))),
            },
            "previous_24h_profile_for_session": self._session_open_24h_profile_for(target_day, timestamp_ms),
            "ny_first_15m_profile": self._ny_first_15m_profile_for(target_day, timestamp_ms),
        })

    def _build_bubbles_for_closed_minute(self) -> list[dict[str, Any]]:
        min_qty = self.config.bubble_min_qty
        min_notional = self.config.bubble_min_notional
        if min_qty is None and min_notional is None:
            assert self.current_bucket is not None
            minute_start_ms = int(self.current_bucket.timestamp() * 1000)
            cutoff_ms = minute_start_ms - 24 * 60 * 60 * 1000
            bubble_window = [
                t
                for t in self.recent_trades
                if cutoff_ms <= int(t["timestamp"]) < minute_start_ms
            ]
            if len(bubble_window) < self.config.bubble_lookback_min_trades:
                return []
            qtys = [float(t["qty"]) for t in bubble_window]
            notionals = [float(t["price"]) * float(t["qty"]) for t in bubble_window]
            min_qty = float(np.quantile(qtys, self.config.bubble_percentile))
            min_notional = float(np.quantile(notionals, self.config.bubble_percentile))
        return build_order_bubbles(pd.DataFrame(self.current_minute_trades), self.config.symbol, min_qty=min_qty, min_notional=min_notional)

    def _drop_old_trades(self, now_ms: int) -> None:
        cutoff_ms = now_ms - 25 * 60 * 60 * 1000
        while self.recent_trades and int(self.recent_trades[0]["timestamp"]) < cutoff_ms:
            self.recent_trades.popleft()

    def _session_open_24h_profile_for(self, target_day: date, now_ms: int) -> dict[str, Any] | None:
        if target_day in self.session_open_24h_profiles:
            return self.session_open_24h_profiles[target_day]

        window_end = datetime.combine(target_day, self.ny_open, tzinfo=self.session_tz)
        window_end_ms = int(window_end.astimezone(timezone.utc).timestamp() * 1000)
        if now_ms < window_end_ms:
            return None

        window_start = window_end - timedelta(hours=24)
        window_start_ms = int(window_start.astimezone(timezone.utc).timestamp() * 1000)
        trades = [
            trade
            for trade in self.recent_trades
            if window_start_ms <= int(trade["timestamp"]) < window_end_ms
        ]
        profile = self._profile_for(trades)
        if profile is None:
            return None

        profile = {
            **profile,
            "profile_type": "previous_24h_profile_for_session",
            "frozen_at_session_open": True,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "timezone": self.config.session_timezone,
        }
        self.session_open_24h_profiles[target_day] = profile
        return profile

    def _ny_first_15m_profile_for(self, target_day: date, now_ms: int) -> dict[str, Any] | None:
        if target_day in self.ny_first_15_profiles:
            return self.ny_first_15_profiles[target_day]

        window_start = datetime.combine(target_day, self.ny_open, tzinfo=self.session_tz)
        window_end = window_start + timedelta(minutes=15)
        window_end_ms = int(window_end.astimezone(timezone.utc).timestamp() * 1000)
        if now_ms < window_end_ms:
            return None

        profile = self._profile_for(self.ny_first_15_trades.get(target_day, []))
        if profile is None:
            return None

        profile = {
            **profile,
            "profile_type": "ny_first_15m_profile",
            "frozen_at_window_end": True,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "timezone": self.config.session_timezone,
        }
        self.ny_first_15_profiles[target_day] = profile
        return profile

    def _update_extremes(self, trade: dict[str, Any]) -> None:
        label_day = self._label_for_trade(int(trade["timestamp"]))
        if label_day is None:
            return
        label, day = label_day
        key = (label, day)
        price = float(trade["price"])
        row = self.extremes.setdefault(key, {"session": label, "session_day": day.isoformat(), "timezone": self.config.session_timezone, "high": price, "low": price})
        row["high"] = max(float(row["high"]), price)
        row["low"] = min(float(row["low"]), price)

    def _update_ny_first_15(self, trade: dict[str, Any]) -> None:
        ts_local = self._to_session_time(int(trade["timestamp"]))
        day = self._target_session_day(int(trade["timestamp"]))
        start = datetime.combine(day, self.ny_open, tzinfo=self.session_tz)
        if start <= ts_local < start + timedelta(minutes=15):
            self.ny_first_15_trades.setdefault(day, []).append(trade)

    def _label_for_trade(self, timestamp_ms: int) -> tuple[str, date] | None:
        ts_local = self._to_session_time(timestamp_ms)
        day = ts_local.date()
        t = ts_local.time()
        if self.ny_open <= t < self.cutoff:
            return ("ny", day)
        if t >= self.overnight_start:
            return ("overnight", day + timedelta(days=1))
        if t < self.pre_ny_start:
            return ("overnight", day)
        if self.pre_ny_start <= t < self.ny_open:
            return ("pre_ny", day)
        return None

    def is_setup_observation_active(self, timestamp_ms: int) -> bool:
        t = self._to_session_time(timestamp_ms).time()
        return self.ny_open <= t < self.cutoff

    def _target_session_day(self, timestamp_ms: int) -> date:
        ts_local = self._to_session_time(timestamp_ms)
        if ts_local.time() >= self.overnight_start:
            return ts_local.date() + timedelta(days=1)
        return ts_local.date()

    def _to_session_time(self, timestamp_ms: int) -> datetime:
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(self.session_tz)
