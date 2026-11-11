"""JetStream market input for an isolated, disarmed Burst worker."""

import asyncio
import json
import math
from uuid import uuid4

from utils.engine.adapters.binance_public import UnrecoverableGap
from utils.engine.collectors.market_data.broker import connect
from utils.engine.contracts import validate_event

STREAM = "GG_EXEC_V1"
BROKER_TIMEOUT_SECONDS = 5


def trade_batch(raw, symbol):
    if not isinstance(raw, dict):
        raise ValueError("invalid_burst_market_event")
    event = validate_event(raw)
    if (event["venue"], event["product"], event["instrument_id"], event["event_type"], event["source"]) != (
        "binance", "futures", symbol, "trade", "native_aggregate_trades"
    ) or event["quality"] not in {"partial", "complete"}:
        raise ValueError("unexpected_burst_market_event")
    epoch = event.get("source_epoch")
    payload = event["payload"]
    trades = payload.get("trades")
    if not isinstance(epoch, str) or not epoch or not isinstance(trades, list) or not 1 <= len(trades) <= 500:
        raise ValueError("invalid_burst_trade_batch")
    previous_id = previous_time = None
    for tick in trades:
        if not isinstance(tick, dict) or tick.get("symbol") != symbol or type(tick.get("buy")) is not bool:
            raise ValueError("invalid_burst_trade_side_or_symbol")
        trade_id, timestamp = tick.get("agg_trade_id"), tick.get("timestamp_ms")
        if (type(trade_id) is not int or trade_id < 0 or type(timestamp) is not int or timestamp < 0
                or any(type(tick.get(key)) not in (float, int) or not math.isfinite(tick[key]) or tick[key] <= 0
                       for key in ("price", "quantity"))):
            raise ValueError("invalid_burst_trade_fields")
        if previous_id is not None and (trade_id != previous_id + 1 or timestamp < previous_time):
            raise ValueError("invalid_burst_trade_batch_order")
        previous_id, previous_time = trade_id, timestamp
    if (payload.get("first_source_sequence") != trades[0]["agg_trade_id"]
            or payload.get("last_source_sequence") != previous_id
            or event["source_sequence"] != previous_id or event["event_timestamp_ms"] != previous_time):
        raise ValueError("burst_trade_batch_boundary_mismatch")
    return epoch, trades


class BurstMarketInput:
    def __init__(self, url, model):
        self.url = url
        self.model = model
        self.symbol = model.executor.symbol

    def repair(self, symbol, previous, incoming):
        # The collector owns repair. A gap in the delivered journal needs a new profile.
        raise UnrecoverableGap(f"{symbol}: broker trade coverage interrupted")

    async def _subscribe(self, nc, durable):
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
        js = nc.jetstream()
        info = await js.stream_info(STREAM)
        subscription = await js.pull_subscribe(
            f"exec.v1.binance.futures.{self.symbol}.trade", durable, stream=STREAM,
            config=ConsumerConfig(durable_name=durable, ack_policy=AckPolicy.EXPLICIT,
                                  deliver_policy=DeliverPolicy.ALL, ack_wait=30, max_ack_pending=1),
        )
        return str(info.created), subscription

    def apply(self, broker_identity, sequence, raw):
        model = self.model
        if model.get("burst_broker_identity") == broker_identity and sequence <= model.get("burst_broker_sequence", 0):
            return
        epoch, trades = trade_batch(raw, self.symbol)
        if (model.get("burst_broker_identity") != broker_identity
                or model.get("burst_source_epoch") != epoch):
            with model.store.transaction():
                model.reset_profile("new_burst_broker_source")
                model.put("burst_broker_identity", broker_identity)
                model.put("burst_source_epoch", epoch)
                model.put("burst_broker_sequence", 0)
        cursor = model.get("public_cursor")
        after = cursor["agg_trade_id"] if cursor else -1
        for trade in trades:
            if trade["agg_trade_id"] == after and trade != cursor:
                raise ValueError("burst_broker_cursor_conflict")
            if trade["agg_trade_id"] > after:
                yield trade
        cursor = model.get("public_cursor")
        if not cursor or cursor["agg_trade_id"] < trades[-1]["agg_trade_id"]:
            raise OSError("burst_trade_batch_not_committed")
        with model.store.transaction():
            model.put("burst_broker_sequence", sequence)

    def stream(self, symbols):
        from nats.errors import Error as NatsError
        from utils.engine.collectors.market_data.shadow import fetch_batch

        if list(symbols) != [self.symbol]:
            raise ValueError("one_burst_consumer_per_symbol")
        durable = self.model.get("burst_consumer_id")
        if durable is None:
            durable = f"burst_shadow_{self.symbol.lower()}_{uuid4().hex[:12]}"
            with self.model.store.transaction():
                self.model.put("burst_consumer_id", durable)
        with asyncio.Runner() as loop:
            nc = None
            try:
                # Initial NATS retries can be unlimited. Return control to the worker
                # for heartbeat and exchange-side exit management during broker outages.
                nc = loop.run(asyncio.wait_for(connect(self.url), BROKER_TIMEOUT_SECONDS))
                identity, subscription = loop.run(asyncio.wait_for(
                    self._subscribe(nc, durable), BROKER_TIMEOUT_SECONDS,
                ))
                while True:
                    messages = loop.run(fetch_batch(subscription, batch=1))
                    if not messages:
                        yield None
                    for message in messages:
                        yield from self.apply(identity, message.metadata.sequence.stream, json.loads(message.data))
                        loop.run(message.ack_sync(timeout=3))
            except NatsError:
                raise OSError("burst_broker_unavailable") from None
            finally:
                if nc is not None:
                    loop.run(nc.close())
