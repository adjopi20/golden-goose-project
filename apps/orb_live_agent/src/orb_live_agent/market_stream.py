from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import websockets

from .config import AgentConfig


def stream_url(config: AgentConfig) -> str:
    symbol = config.symbol.lower()
    streams = [f"{symbol}@aggTrade"]
    if config.audit_kline_1m:
        streams.append(f"{symbol}@kline_1m")
    return f"{config.stream_base}?streams={'/'.join(streams)}"


async def stream_market_events(config: AgentConfig) -> AsyncIterator[dict[str, Any]]:
    url = stream_url(config)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                async for message in ws:
                    yield json.loads(message)
        except Exception as exc:
            yield {"stream": "system", "data": {"event": "websocket_error", "error": str(exc)}}
            await asyncio.sleep(5)
