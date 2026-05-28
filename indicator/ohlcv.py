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
    aggressive_side: str


def _row_to_parsed_trade(row: pd.Series) -> ParsedTrade:
    is_buyer_maker = bool(row["is_buyer_maker"])
    aggressive_side = "sell" if is_buyer_maker else "buy"
    return ParsedTrade(
        timestamp_ms=int(row["timestamp"]),
        price=float(row["price"]),
        qty=float(row["qty"]),
        aggressive_side=aggressive_side,
    )


def timeframe_to_ms(timeframe: str) -> int:
    mapping = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
    if timeframe not in mapping:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return mapping[timeframe]


def get_bucket_start(timestamp_ms: int, timeframe: str) -> datetime:
    timeframe_ms = timeframe_to_ms(timeframe)
    bucket_ms = (int(timestamp_ms) // timeframe_ms) * timeframe_ms
    return datetime.fromtimestamp(bucket_ms / 1000.0, tz=timezone.utc)


def finalize_candle(symbol: str, timeframe: str, bucket_dt: datetime, state: dict[str, Any]) -> dict[str, Any]:
    open_price = float(state["open"])
    high_price = float(state["high"])
    low_price = float(state["low"])
    close_price = float(state["close"])

    volume = float(state["volume"])
    buy_volume = float(state["buy_volume"])
    sell_volume = float(state["sell_volume"])

    price_change = close_price - open_price
    candle_range = high_price - low_price
    body = abs(close_price - open_price)
    upper_wick = high_price - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low_price

    return {
        "symbol": str(symbol),
        "timeframe": str(timeframe),
        "timestamp": bucket_dt.isoformat(),
        "timestamp_ms": int(bucket_dt.timestamp() * 1000),
        "open": _round_float(open_price, 8),
        "high": _round_float(high_price, 8),
        "low": _round_float(low_price, 8),
        "close": _round_float(close_price, 8),
        "volume": _round_float(volume, 8),
        "buy_volume": _round_float(buy_volume, 8),
        "sell_volume": _round_float(sell_volume, 8),
        "delta": _round_float(buy_volume - sell_volume, 8),
        "trade_count": int(state["trade_count"]),
        "buy_trade_count": int(state["buy_trade_count"]),
        "sell_trade_count": int(state["sell_trade_count"]),
        "largest_trade_qty": _round_float(float(state["largest_trade_qty"]), 8),
        "largest_trade_side": str(state["largest_trade_side"]),
        "price_change": _round_float(price_change, 8),
        "range": _round_float(candle_range, 8),
        "body": _round_float(body, 8),
        "upper_wick": _round_float(upper_wick, 8),
        "lower_wick": _round_float(lower_wick, 8),
    }


def aggregate_trades_to_ohlcv(
    trades_df: pd.DataFrame,
    symbol: str,
    timeframe: str = "1m",
) -> list[dict[str, Any]]:
    required_cols = {"timestamp", "price", "qty", "is_buyer_maker"}
    missing_cols = sorted(required_cols - set(trades_df.columns))
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")

    if trades_df.empty:
        return []

    sorted_df = trades_df.sort_values(["timestamp", "price", "qty"], kind="mergesort")
    parsed_trades = [_row_to_parsed_trade(row) for _, row in sorted_df.iterrows()]

    candles_state: dict[datetime, dict[str, Any]] = {}
    for trade in parsed_trades:
        bucket_dt = get_bucket_start(trade.timestamp_ms, timeframe)
        if bucket_dt not in candles_state:
            candles_state[bucket_dt] = {
                "open": trade.price,
                "high": trade.price,
                "low": trade.price,
                "close": trade.price,
                "volume": 0.0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "trade_count": 0,
                "buy_trade_count": 0,
                "sell_trade_count": 0,
                "largest_trade_qty": 0.0,
                "largest_trade_side": "buy",
            }

        state = candles_state[bucket_dt]
        state["high"] = max(float(state["high"]), trade.price)
        state["low"] = min(float(state["low"]), trade.price)
        state["close"] = trade.price
        state["volume"] += trade.qty
        state["trade_count"] += 1

        if trade.aggressive_side == "buy":
            state["buy_volume"] += trade.qty
            state["buy_trade_count"] += 1
        else:
            state["sell_volume"] += trade.qty
            state["sell_trade_count"] += 1

        if trade.qty > float(state["largest_trade_qty"]):
            state["largest_trade_qty"] = trade.qty
            state["largest_trade_side"] = trade.aggressive_side

    return [
        finalize_candle(symbol=symbol, timeframe=timeframe, bucket_dt=bucket_dt, state=candles_state[bucket_dt])
        for bucket_dt in sorted(candles_state.keys())
    ]
