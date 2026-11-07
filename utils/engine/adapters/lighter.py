"""Verified public trade normalization only; NOT an executable paper adapter yet.

Public recentTrades returns at most 100 prints. Long-disconnect repair and
historical delta warmup remain unproven; no Binance feature fallback is allowed.
"""
import math


MARKETS = {"ETH":0, "HYPE":24, "BNB":25}  # revalidate with orderBooks at startup


def normalize(row, market_id):
    if row.get("market_id") != market_id or type(row.get("is_maker_ask")) is not bool:
        raise ValueError("Invalid Lighter market/side")
    if row.get("type") != "trade":
        return None  # liquidation/deleverage/settlement are not ordinary prints
    price, quantity = float(row["price"]), float(row["size"])
    if not all(math.isfinite(x) and x > 0 for x in (price, quantity)):
        raise ValueError("Invalid Lighter price/size")
    if any(type(row.get(k)) is not int or row[k] < 0 for k in ("timestamp", "trade_id")):
        raise ValueError("Invalid Lighter trade identity")
    return dict(market_id=market_id, agg_trade_id=row["trade_id"], timestamp_ms=row["timestamp"],
                price=price, quantity=quantity, buy=row["is_maker_ask"])
