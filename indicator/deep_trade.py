from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd


def _round_float(value: float, ndigits: int) -> float:
    return float(round(float(value), ndigits))


@dataclass(frozen=True)
class ParsedTrade:
    timestamp_ms: int
    price: float
    qty: float
    notional: float
    aggressive_side: str


def get_aggressive_side(is_buyer_maker: bool) -> str:
    return "sell" if bool(is_buyer_maker) else "buy"


def _row_to_parsed_trade(row: pd.Series) -> ParsedTrade:
    timestamp_ms = int(row["timestamp"])
    price = float(row["price"])
    qty = float(row["qty"])
    notional = float(price * qty)
    aggressive_side = get_aggressive_side(bool(row["is_buyer_maker"]))
    return ParsedTrade(
        timestamp_ms=timestamp_ms,
        price=price,
        qty=qty,
        notional=notional,
        aggressive_side=aggressive_side,
    )


def timestamp_ms_to_utc_iso(timestamp_ms: int) -> str:
    dt_utc = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    return dt_utc.isoformat()


def get_minute_bucket_utc(timestamp_ms: int) -> str:
    dt_utc = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    return dt_utc.replace(second=0, microsecond=0).isoformat()


def passes_threshold(
    qty: float,
    notional: float,
    min_qty: float | None,
    min_notional: float | None,
) -> tuple[bool, bool, bool]:
    passes_qty = min_qty is not None and qty >= float(min_qty)
    passes_notional = min_notional is not None and notional >= float(min_notional)
    return bool(passes_qty or passes_notional), bool(passes_qty), bool(passes_notional)


def compute_bubble_score(
    qty: float,
    notional: float,
    min_qty: float | None,
    min_notional: float | None,
    passes_qty: bool,
    passes_notional: bool,
) -> tuple[str, float, float]:
    if passes_qty and passes_notional:
        qty_score = qty / float(min_qty)
        notional_score = notional / float(min_notional)
        score = max(qty_score, notional_score)
        threshold_value = float(min_qty) if qty_score >= notional_score else float(min_notional)
        return "qty+notional", threshold_value, float(score)

    if passes_qty:
        return "qty", float(min_qty), float(qty / float(min_qty))

    if passes_notional:
        return "notional", float(min_notional), float(notional / float(min_notional))

    raise ValueError("compute_bubble_score called for a trade that did not pass thresholds")


def assign_bubble_tier(bubble_size_score: float) -> str:
    if bubble_size_score >= 3.0:
        return "extreme"
    if bubble_size_score >= 1.75:
        return "large"
    return "medium"


def _validate_thresholds(min_qty: float | None, min_notional: float | None) -> None:
    if min_qty is None and min_notional is None:
        raise ValueError("At least one threshold must be provided: min_qty and/or min_notional")
    if min_qty is not None and min_qty <= 0:
        raise ValueError("min_qty must be > 0")
    if min_notional is not None and min_notional <= 0:
        raise ValueError("min_notional must be > 0")


def build_order_bubbles(
    trades_df: pd.DataFrame,
    symbol: str,
    min_qty: float | None,
    min_notional: float | None,
) -> list[dict[str, Any]]:
    required_cols = {"timestamp", "price", "qty", "is_buyer_maker"}
    missing_cols = sorted(required_cols - set(trades_df.columns))
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")

    _validate_thresholds(min_qty=min_qty, min_notional=min_notional)

    if trades_df.empty:
        return []

    sorted_df = trades_df.sort_values(["timestamp", "price", "qty"], kind="mergesort")

    bubbles: list[dict[str, Any]] = []
    for _, row in sorted_df.iterrows():
        trade = _row_to_parsed_trade(row)
        passes_any, passes_qty, passes_notional = passes_threshold(
            qty=trade.qty,
            notional=trade.notional,
            min_qty=min_qty,
            min_notional=min_notional,
        )
        if not passes_any:
            continue

        threshold_mode, threshold_value, bubble_size_score = compute_bubble_score(
            qty=trade.qty,
            notional=trade.notional,
            min_qty=min_qty,
            min_notional=min_notional,
            passes_qty=passes_qty,
            passes_notional=passes_notional,
        )

        bubbles.append(
            {
                "symbol": str(symbol),
                "bubble_type": "agg_trade",
                "bubble_status": "confirmed",
                "timestamp": timestamp_ms_to_utc_iso(trade.timestamp_ms),
                "timestamp_ms": int(trade.timestamp_ms),
                "minute_bucket": get_minute_bucket_utc(trade.timestamp_ms),
                "price": float(trade.price),
                "qty": float(trade.qty),
                "notional": float(trade.notional),
                "aggressive_side": str(trade.aggressive_side),
                "threshold_mode": str(threshold_mode),
                "threshold_value": float(threshold_value),
                "min_qty": float(min_qty) if min_qty is not None else None,
                "min_notional": float(min_notional) if min_notional is not None else None,
                "bubble_size_score": _round_float(float(bubble_size_score), 6),
                "bubble_tier": assign_bubble_tier(float(bubble_size_score)),
            }
        )

    bubbles.sort(key=lambda r: (int(r["timestamp_ms"]), float(r["price"]), float(r["qty"])))
    return bubbles
