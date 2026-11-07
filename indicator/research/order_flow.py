"""Second-layer order-flow interpretations for research; no entries or orders.

Price response uses open/close only. High/low are retained but never classified.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from indicator.research.cvd import cumulative_volume_delta, volume_delta
from utils.closed_candle_context import CandleContext, CandleNotReady


@dataclass(frozen=True)
class OrderFlowConfig:
    lookback: int = 10
    tail_fraction: float = 0.15
    temporal_window: int = 5
    min_initiative: int = 2

    def __post_init__(self):
        for name in ("lookback", "temporal_window", "min_initiative"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 2:
                raise ValueError(f"{name} must be an integer >= 2")
        if self.min_initiative > self.temporal_window:
            raise ValueError("min_initiative cannot exceed temporal_window")
        if not isinstance(self.tail_fraction, (int, float)) or not 0 < self.tail_fraction < 0.5:
            raise ValueError("tail_fraction must be between 0 and 0.5")


def _classify_candle(volume_class, delta_class, movement_class, alignment):
    if "warmup" in (volume_class, delta_class, movement_class):
        return "warmup"
    if volume_class == "typical":
        return "typical"
    if volume_class == "low":
        return {"low": "lack_of_participation", "typical": "low_volume_typical_movement",
                "high": "path_of_least_resistance"}[movement_class]
    if movement_class == "typical" or (movement_class == "low" and delta_class == "typical"):
        return "typical"
    if movement_class == "low":
        return "absorption" if delta_class == "high" else "high_volume_churn"
    if delta_class == "low":
        return "low_delta_expansion"
    if delta_class == "typical":
        return "typical_delta_expansion"
    return {"aligned": "initiative", "opposed": "opposing_expansion"}.get(alignment, "unclassified")


def order_flow_features(candles: pd.DataFrame, *, config: OrderFlowConfig | None = None,
                        include_temporal: bool = True) -> pd.DataFrame:
    """Pure indicator callback for already closed CandleContext candle frames.

    Use interpret_order_flow(context, timeframe=...) for the clock-gated entry.
    Requires measured buy_volume and sell_volume; never guesses sides from OHLC.
    Historical rows may be warmup; latest warmup blocks CandleContext.evaluate.
    Set include_temporal=False to return the candle-label layer only.
    """
    config = OrderFlowConfig() if config is None else config
    if not isinstance(config, OrderFlowConfig):
        raise ValueError("config must be OrderFlowConfig")
    if candles.empty:
        raise CandleNotReady("No completed candles for order-flow interpretation")
    required = ("open", "close", "buy_volume", "sell_volume", "volume",
                "timestamp_ms", "end_ms", "available_at_ms")
    if not candles.columns.is_unique or any(col not in candles for col in required):
        raise ValueError("Use closed CandleContext candles with measured buy_volume and sell_volume")
    # Shared indicators validate quantities, timestamps and the daily UTC reset.
    delta = volume_delta(candles)
    cvd = cumulative_volume_delta(candles)
    stamps = candles.index.as_unit("ms").asi8
    for col in ("timestamp_ms", "end_ms", "available_at_ms"):
        if candles[col].dtype.kind not in "iu" or (candles[col] < 0).any():
            raise ValueError("Candle timing must be nonnegative integer milliseconds")
    durations = candles.end_ms.to_numpy() - stamps
    duration = int(durations[0])
    if (not np.array_equal(candles.timestamp_ms.to_numpy(), stamps) or duration < 60_000
            or 86_400_000 % duration or (stamps % duration).any()
            or (durations != duration).any() or (np.diff(stamps) != duration).any()
            or (candles.available_at_ms < candles.end_ms).any()):
        raise ValueError("Order-flow candles must be contiguous, UTC-aligned and available after close")
    prices = candles[["open", "close"]].astype(float)
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("Order-flow open/close prices must be finite and positive")

    out = candles.copy(deep=True)
    out["interpretation_version"] = "order_flow_v1"
    out["delta"] = delta
    out["cvd"] = cvd
    out["buy_volume"] = candles.buy_volume.astype(float)
    out["sell_volume"] = candles.sell_volume.astype(float)
    out["signed_return"] = (prices.close - prices.open) / prices.open
    if not np.isfinite(out.signed_return.to_numpy()).all():
        raise ValueError("Price return overflowed float64")
    out["movement_magnitude"] = out.signed_return.abs()
    out["delta_magnitude"] = delta.abs()
    out["price_direction"] = np.sign(out.signed_return).map({-1: "down", 0: "neutral", 1: "up"})
    out["delta_direction"] = np.sign(delta).map({-1: "sell", 0: "neutral", 1: "buy"})
    product = np.sign(out.signed_return) * np.sign(delta)
    out["alignment"] = product.map({-1: "opposed", 0: "neutral", 1: "aligned"})
    for name, values in (("volume", out.volume.astype(float)), ("delta", out.delta_magnitude),
                         ("movement", out.movement_magnitude)):
        history = values.shift(1).rolling(config.lookback, min_periods=config.lookback)
        low = history.quantile(config.tail_fraction, interpolation="linear")
        high = history.quantile(1 - config.tail_fraction, interpolation="linear")
        out[f"{name}_q_low"], out[f"{name}_q_high"] = low, high
        out[f"{name}_class"] = pd.Series(np.select(
            [values < low, values > high], ["low", "high"], default="typical"), index=out.index).where(low.notna(), "warmup")
    out["candle_label"] = [_classify_candle(*values) for values in zip(
        out.volume_class, out.delta_class, out.movement_class, out.alignment)]
    out["high_expansion"] = out.movement_class.eq("high")
    out["base_status"] = np.where(out.candle_label.eq("warmup"), "warmup", "ready")
    # A late historical dependency must not make an earlier label look available sooner.
    out["available_at_ms"] = out.available_at_ms.cummax()
    if not include_temporal:
        return out

    flags = ("aggression_support", "aggression", "buy_volume_decreasing", "sell_volume_decreasing",
             "directional_volume_weakening", "directional_delta_weakening", "opposing_cvd",
             "exhaustion_weakening_support", "exhaustion_opposing_cvd", "exhaustion")
    for col in flags:
        out[col] = False
    metrics = ("initiative_count", "sequence_direction_sign", "sequence_price_return",
               "directional_cvd_change", "directional_delta", "previous_directional_delta",
               "reference_close", "reference_cvd", "directional_close_progress")
    for col in metrics:
        out[col] = np.nan
    out["sequence_direction"] = "neutral"
    out["temporal_status"] = "warmup"
    out["temporal_state"] = "warmup"
    out["exhaustion_subtype"] = "none"
    days = out.index.tz_convert("UTC").normalize()
    for i in range(config.temporal_window - 1, len(out)):
        start = i - config.temporal_window + 1
        window = out.iloc[start:i+1]
        if days[start] != days[i] or not window.base_status.eq("ready").all():
            continue
        row, prior = out.iloc[i], out.iloc[i-1]
        # Determine direction from earlier prices, then hold it fixed for this window.
        direction = int(np.sign(float(prior.close) - float(window.open.iloc[0])))
        progress = (float(row.close) - float(window.open.iloc[0])) / float(window.open.iloc[0])
        cvd_before_window = float(window.cvd.iloc[0] - window.delta.iloc[0])
        cvd_change = direction * (float(row.cvd) - cvd_before_window)
        initiative = window.candle_label.eq("initiative") & np.sign(window.signed_return).eq(direction)
        allowed = initiative | window.candle_label.isin(("typical", "low_volume_typical_movement"))
        aggression_support = bool(direction and initiative.sum() >= config.min_initiative
                                  and allowed.all() and direction * progress > 0 and cvd_change > 0)
        buy_fading = bool(row.buy_volume < prior.buy_volume)
        sell_fading = bool(row.sell_volume < prior.sell_volume)
        own_fading = buy_fading if direction == 1 else sell_fading if direction == -1 else False
        directional_delta = direction * float(row.delta)
        previous_delta = direction * float(prior.delta)
        delta_fading = bool(0 < directional_delta < previous_delta)
        previous_prices = out.close.iloc[start:i].astype(float).to_numpy()
        reference_i = start + int(np.argmax(direction * previous_prices)) if direction else i-1
        reference = out.iloc[reference_i]
        opposing = bool(direction and direction * (row.close - reference.close) > 0
                        and direction * (row.cvd - reference.cvd) < 0)
        close_progress = direction * float(row.close - prior.close)
        confirmed = bool(direction and close_progress > 0 and row.movement_class != "high")
        weak_exhaustion = bool(confirmed and (own_fading or delta_fading))
        opposing_exhaustion = bool(confirmed and opposing)
        exhausted = weak_exhaustion or opposing_exhaustion
        updates = dict(aggression_support=aggression_support, aggression=aggression_support and not exhausted,
            buy_volume_decreasing=buy_fading, sell_volume_decreasing=sell_fading,
            directional_volume_weakening=own_fading, directional_delta_weakening=delta_fading,
            opposing_cvd=opposing, exhaustion_weakening_support=weak_exhaustion,
            exhaustion_opposing_cvd=opposing_exhaustion, exhaustion=exhausted,
            initiative_count=int(initiative.sum()), sequence_direction_sign=direction,
            sequence_direction={-1: "down", 0: "neutral", 1: "up"}[direction],
            sequence_price_return=progress, directional_cvd_change=cvd_change,
            directional_delta=directional_delta, previous_directional_delta=previous_delta,
            reference_close=float(reference.close), reference_cvd=float(reference.cvd),
            directional_close_progress=close_progress, temporal_status="ready",
            temporal_state="exhaustion" if exhausted else "aggression" if aggression_support else "none",
            exhaustion_subtype=("weakening_support_and_opposing_cvd" if weak_exhaustion and opposing_exhaustion
                else "weakening_support" if weak_exhaustion else "opposing_cvd" if opposing_exhaustion else "none"))
        for col, value in updates.items():
            out.iat[i, out.columns.get_loc(col)] = value
    out["status"] = np.where(out.base_status.eq("ready") & out.temporal_status.eq("ready"), "ready", "warmup")
    return out


def interpret_order_flow(context: CandleContext, *, timeframe: str,
                         config: OrderFlowConfig | None = None) -> pd.DataFrame:
    """Clock-gated research entry: aggregate first, then interpret that timeframe."""
    if not isinstance(context, CandleContext):
        raise ValueError("Pass the shared research CandleContext")
    candles = context.candles(timeframe)
    context._latest(candles, timeframe)  # Same freshness gate as the shared evaluation path.
    return order_flow_features(candles, config=config)
