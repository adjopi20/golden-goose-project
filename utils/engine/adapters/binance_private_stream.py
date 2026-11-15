"""Shadow-only Binance private stream, with account-isolated JetStream transport."""
import argparse
import asyncio
from contextlib import closing, contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import time
from uuid import uuid4

import requests
from websockets.asyncio.client import connect

from utils.engine.adapters.binance_futures_readonly import BinanceFuturesReadOnly, secret_bytes
from utils.engine.adapters.binance_rest_budget import RestDeferred
from utils.engine.monitoring.binance_private_compare import (
    ACCOUNTS, CONTROLS, ShadowStore, binding, canonical, envelope, native_id, number, now_ms,
    project_algo, project_order, project_position, stamp, token,
)
from utils.engine.release import verify_release


STREAM = "GG_PRIVATE_V1"
DURABLE = "gg_shadow_v1"
LISTEN = "/fapi/v1/listenKey"
ACCOUNT_PREFIX = "private."
ROTATE = 23 * 3600 + 50 * 60
KEEPALIVE = 30 * 60
HEARTBEAT = 20
SNAPSHOT_INTERVAL = 15 * 60
WS_LOG = logging.getLogger(__name__ + ".websocket")
WS_LOG.disabled = True


class DirectPrivateSocket(connect):
    def process_redirect(self, exc):
        return exc  # Never copy a credential-bearing request to a redirected URL.


class ListenKeyExpired(RuntimeError):
    pass


class BinancePrivateReads(BinanceFuturesReadOnly):
    rest_background = True

    def listen(self, method):
        """Start/renew only. Both actions are API-key header authenticated."""
        if method not in ("POST", "PUT"):
            raise ValueError("listen_key_method_invalid")
        self.load_credentials()
        ticket = self.budget.acquire(method, LISTEN, {}, background=True)
        try:
            reply = self.http.request(method, self.BASE + LISTEN, headers={"X-MBX-APIKEY": self._key},
                                      timeout=(5, 10), allow_redirects=False)
        except requests.RequestException:
            self.budget.transport_failure(ticket)
            raise ConnectionError("listen_key_transport_error") from None
        try:
            payload = reply.json()
        except ValueError:
            payload = None
        self.budget.observe(ticket, reply, payload)
        code = payload.get("code") if isinstance(payload, dict) else None
        if reply.status_code in (418, 429) or code in (-1003, -1015):
            raise RestDeferred("listen_key_throttled", 5)
        if code == -1125:
            raise ListenKeyExpired()
        key = payload.get("listenKey") if isinstance(payload, dict) else None
        if reply.status_code != 200 or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]{32,192}", key):
            raise ConnectionError("listen_key_response_invalid")
        return key

    def verify_uid(self, expected):
        self.sync_time()
        if hashlib.sha256(str(self.account_uid()).encode()).hexdigest() != expected:
            raise ValueError("private_binance_uid_mismatch")

    def baseline(self):
        # REST reads are sequential. Record their interval without claiming a coherent
        # cross-endpoint snapshot during concurrent venue activity.
        start = now_ms() + self.offset
        positions = self.get("/fapi/v3/positionRisk")
        orders = self.get("/fapi/v1/openOrders")
        algos = self.get("/fapi/v1/openAlgoOrders")
        if not all(isinstance(rows, list) for rows in (positions, orders, algos)):
            raise ValueError("private_snapshot_shape")
        return {"started_ms": start, "through_ms": now_ms() + self.offset,
                "positions": [project_position(x) for x in positions],
                "orders": [project_order(x) for x in orders],
                "algos": [project_algo(x) for x in algos]}


def normalize(raw, account, epoch, received_ms):
    """Select fields only. Raw responses, free text and credentials never enter NATS."""
    event = raw.get("e")
    if event == "listenKeyExpired":
        raise ListenKeyExpired()
    if event not in {"ORDER_TRADE_UPDATE", "ALGO_UPDATE", "ACCOUNT_UPDATE",
                     "CONDITIONAL_ORDER_TRIGGER_REJECT"}:
        return []
    venue_ms = raw["E"]
    txn = raw.get("T", venue_ms)
    result = []

    def add(kind, fields, transaction=txn):
        result.append(envelope(account, epoch, kind, venue_ms, received_ms, fields, transaction))

    if event == "ORDER_TRADE_UPDATE":
        row = raw["o"]
        order = project_order(dict(symbol=row["s"], orderId=row["i"], clientOrderId=row["c"],
                                   status=row["X"], executedQty=row["z"], side=row["S"]))
        add("order", order)
        if row["x"] == "TRADE":
            qty, price = number(row["l"]), number(row["L"])
            if qty <= number(0) or price <= number(0):
                raise ValueError("private_fill_amount_invalid")
            add("fill", {**{k: order[k] for k in ("symbol", "order_id", "client_id", "side")},
                         "trade_id": native_id(row["t"]), "trade_ms": stamp(row["T"]),
                         "quantity": str(qty), "price": str(price)},
                row["T"])
    elif event == "ALGO_UPDATE":
        row = raw["o"]
        add("algo", project_algo(dict(symbol=row["s"], algoId=row["aid"], clientAlgoId=row["caid"],
                                      algoStatus=row["X"], actualOrderId=row.get("ai"))))
    elif event == "ACCOUNT_UPDATE":
        row = raw["a"]
        for position in row.get("P", []):
            add("position", project_position(dict(symbol=position["s"], positionSide=position["ps"],
                                                   positionAmt=position["pa"])))
        for balance in row.get("B", []):
            add("balance", {"asset": token(balance["a"]), "wallet_balance": str(number(balance["wb"]))})
    else:
        row = raw["or"]
        add("trigger_reject", {"symbol": token(row["s"]), "order_id": native_id(row["i"])})
    return result


def channel(account):
    if account not in ACCOUNTS:
        raise ValueError("unknown_private_account")
    return ACCOUNT_PREFIX + account.replace("-", "_") + ".event"


async def publish(js, event):
    # Include the epoch for transport dedupe so a replay can still be evaluated
    # against a new baseline. The consumer deduplicates fills across epochs.
    identity = {k: v for k, v in event.items() if k != "received_ms"}
    msg_id = uuid4().hex if event["kind"] in CONTROLS else hashlib.sha256(canonical(identity)).hexdigest()
    payload = canonical(event)
    if len(payload) > 384 * 1024:
        raise ValueError("shadow_message_too_large")
    await js.publish(channel(event["account"]), payload, stream=STREAM,
                     headers={"Nats-Msg-Id": msg_id}, timeout=3)


async def stream_session(reader, js, account, uid_hash, *, connector=DirectPrivateSocket):
    epoch = uuid4().hex

    async def control(kind, fields=None):
        await publish(js, envelope(account, epoch, kind, None, now_ms(), fields or {}))

    await control("gap")  # This epoch does not establish exchange replay continuity.
    try:
        await asyncio.to_thread(reader.verify_uid, uid_hash)
        key = await asyncio.to_thread(reader.listen, "POST")
        # Transport errors may contain the listenKey URL. The outer boundary
        # logs exception types only. Disable proxy inheritance and redirect fallback.
        async with connector("wss://fstream.binance.com/private/ws/" + key,
                             proxy=None, logger=WS_LOG, open_timeout=10, close_timeout=2, max_queue=64,
                             max_size=256 * 1024, ping_interval=None) as socket:
            if socket.response.status_code != 101 or socket.request.path != "/private/ws/" + key:
                raise ConnectionError("private_ws_handshake_invalid")
            born = time.monotonic()
            renew, beat = born + KEEPALIVE, born
            await control("connected")
            snapshot = await asyncio.to_thread(reader.baseline)
            await control("snapshot", snapshot)
            refresh = born + SNAPSHOT_INTERVAL + random.uniform(0, 45)
            while time.monotonic() - born < ROTATE:
                now = time.monotonic()
                if now >= beat:
                    pong = await socket.ping()
                    await asyncio.wait_for(pong, timeout=6)
                    await control("heartbeat")
                    beat = now + HEARTBEAT
                if now >= renew:
                    if await asyncio.to_thread(reader.listen, "PUT") != key:
                        raise ListenKeyExpired()
                    renew = now + KEEPALIVE
                if now >= refresh:
                    try:
                        snapshot = await asyncio.to_thread(reader.baseline)
                    except RestDeferred as error:
                        refresh = now + max(30, min(error.delay, 120)) + random.uniform(0, 15)
                    else:
                        await control("snapshot", snapshot)
                        refresh = now + SNAPSHOT_INTERVAL + random.uniform(0, 45)
                try:
                    data = await asyncio.wait_for(socket.recv(), timeout=1)
                except TimeoutError:
                    continue  # A quiet account is expected. Pong, not trading activity, proves liveness.
                for event in normalize(json.loads(data), account, epoch, now_ms()):
                    await publish(js, event)
    finally:
        # Disconnect explicitly if possible. A failed process/broker leaves a
        # stopped heartbeat and the next manager starts with a fresh gap marker.
        try:
            await control("gap")
        except Exception:
            pass


async def manager(js, reader, account, uid_hash, *, connected):
    cooldown = 5
    while connected():
        started = time.monotonic()
        delay = 2
        try:
            await stream_session(reader, js, account, uid_hash)
        except Exception as error:
            safe_log("private_stream_disconnected", error, account=account)
            cooldown = 5 if time.monotonic() - started >= 60 else min(cooldown * 2, 120)
            delay = max(cooldown, error.delay) if isinstance(error, RestDeferred) else cooldown
        if connected():
            await asyncio.sleep(min(delay, 120) + random.uniform(0, 3))
    raise ConnectionError("private_nats_disconnected")


def safe_log(event, error=None, **facts):
    print(json.dumps(dict(event=event, error_type=type(error).__name__ if error else None, **facts)), flush=True)


async def connection(url, username, password_path):
    import nats
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme not in ("nats", "tls") or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise ValueError("private_nats_url_invalid")
    password = secret_bytes(password_path, 512).decode("utf-8").strip()
    if len(password) < 16:
        raise ValueError("private_nats_password_too_short")

    async def broker_error(error):
        safe_log("private_nats_error", error)

    nc = await nats.connect(url, user=username, password=password, error_cb=broker_error,
                            connect_timeout=5, allow_reconnect=False)
    # no_auth_user may clear the INFO auth_required bit even when explicit
    # credentials place the client in a different account. Verify the identity
    # reported by the broker, before provisioning or sending any private event.
    group = username.rsplit("_", 1)[0].upper()
    try:
        response = await nc.request("$SYS.REQ.USER.INFO", b"", timeout=2)
        identity = json.loads(response.data)["data"]
        if (identity.get("account_name", identity.get("account")) != "GG_" + group
                or identity.get("user_name", identity.get("user")) != username):
            raise ValueError("private_nats_wrong_identity")
    except Exception:
        await nc.close()
        raise ValueError("private_nats_identity_unverified") from None
    return nc


def stream_config(account):
    from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
    return StreamConfig(name=STREAM, subjects=[channel(account)], retention=RetentionPolicy.LIMITS,
                        storage=StorageType.FILE, discard=DiscardPolicy.NEW,
                        max_age=86400, max_bytes=32 * 1024**2, max_msgs=50_000,
                        max_msg_size=384 * 1024, duplicate_window=120, num_replicas=1)


def consumer_config(account):
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
    return ConsumerConfig(durable_name=DURABLE, filter_subject=channel(account),
                          ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL,
                          ack_wait=30, max_ack_pending=1, max_deliver=-1)


async def provision(js, account):
    from nats.js.errors import NotFoundError

    stream = stream_config(account)
    consumer = consumer_config(account)
    try:
        actual = (await js.stream_info(STREAM)).config
    except NotFoundError:
        actual = (await js.add_stream(config=stream)).config
    if any(getattr(actual, n) != getattr(stream, n) for n in (
            "subjects", "retention", "storage", "discard", "max_age", "max_bytes",
            "max_msgs", "max_msg_size", "duplicate_window", "num_replicas")):
        raise ValueError("shadow_stream_configuration_mismatch")
    try:
        actual = (await js.consumer_info(STREAM, DURABLE)).config
    except NotFoundError:
        actual = (await js.add_consumer(STREAM, config=consumer)).config
    if any(getattr(actual, n) != getattr(consumer, n) for n in (
            "filter_subject", "ack_policy", "deliver_policy", "ack_wait", "max_ack_pending", "max_deliver")):
        raise ValueError("shadow_consumer_configuration_mismatch")


async def handle_delivery(store, msg):
    if msg.subject != channel(store.account):
        raise ValueError("shadow_delivery_subject_mismatch")
    m = msg.metadata
    # SQLite commit precedes broker ACK. A crash here redelivers safely.
    try:
        store.apply(msg.data, m.sequence.stream, m.num_pending)
    except (KeyError, ValueError, TypeError):
        store.quarantine(msg.data, m.sequence.stream, m.num_pending)
    await msg.ack_sync(timeout=3)


async def consume(js, store, observed):
    from nats.errors import TimeoutError as NatsTimeout

    info = await js.stream_info(STREAM)
    if info.config.subjects != [channel(store.account)]:
        raise ValueError("shadow_consumer_subject_mismatch")
    store.bind_stream(str(info.created))
    sub = await js.pull_subscribe_bind(stream=STREAM, consumer=DURABLE)
    next_compare = 0
    while True:
        try:
            messages = await sub.fetch(1, timeout=2)
        except NatsTimeout:
            messages = []
        for msg in messages:
            await handle_delivery(store, msg)
        if time.monotonic() >= next_compare:
            # Broker generation matters if an operator deletes and recreates a stream.
            info = await js.stream_info(STREAM)
            store.bind_stream(str(info.created))
            state = await js.consumer_info(STREAM, DURABLE)
            counts = store.compare(observed, backlog=state.num_pending + state.num_ack_pending)
            safe_log("private_shadow_status", account=store.account, **counts)
            next_compare = time.monotonic() + 15


@contextmanager
def single_owner(path):
    import fcntl

    with Path(path).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


async def run(args):
    user = args.account.replace("-", "_") + "_" + args.role
    nc = await connection(args.nats_url, user, args.nats_password_file)
    try:
        js = nc.jetstream()
        if args.role == "init":
            await provision(js, args.account)
            return
        uid = binding(args.observed, args.account)
        if args.role == "consumer":
            with closing(ShadowStore(args.state / "shadow.sqlite", args.account, uid)) as store:
                await consume(js, store, args.observed)
            return
        secret = Path("/run/binance")
        signing = {"hmac_secret_file": secret / "hmac-secret"} if args.hmac else {
            "private_key_file": secret / "ed25519.pem", "passphrase_file": secret / "passphrase"}
        # Refuse to run a service using a per-container or temporary limiter fallback.
        if not os.environ.get("BINANCE_REST_BUDGET_DB"):
            raise ValueError("shared_budget_path_required")
        if os.environ.get("BINANCE_REST_ACCOUNT") != args.account:
            raise ValueError("shared_budget_account_mismatch")
        with closing(BinancePrivateReads(api_key_file=secret / "api-key", **signing)) as reader:
            await manager(js, reader, args.account, uid, connected=lambda: nc.is_connected)
    finally:
        await nc.close()


def main():
    import signal

    p = argparse.ArgumentParser()
    p.add_argument("role", choices=("manager", "consumer", "init"))
    p.add_argument("account", choices=tuple(ACCOUNTS))
    p.add_argument("--observed", type=Path, default=Path("/observed"))
    p.add_argument("--state", type=Path, default=Path("/shadow"))
    p.add_argument("--nats-url", default="nats://nats:4222")
    p.add_argument("--nats-password-file", type=Path, default=Path("/run/nats/password"))
    p.add_argument("--hmac", action="store_true")
    p.add_argument("--release-hash", required=True)
    args = p.parse_args()
    try:
        verify_release(Path(__file__).resolve().parents[1] / "releases/shared_engine_20261115_v10.json",
                       expected_sha256=args.release_hash)
        def stop(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        if args.role == "init":
            asyncio.run(run(args))
        else:
            with single_owner(args.state / (args.role + ".lock")):
                asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        safe_log("private_shadow_stopped", error, role=args.role, account=args.account)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
