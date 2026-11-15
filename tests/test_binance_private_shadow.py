"""Tests for Binance private-event shadowing. Fixtures contain synthetic evidence only."""
import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from utils.engine.adapters import binance_private_stream as stream
from utils.engine.adapters.binance_rest_budget import RestBudget, RestDeferred, request_weight
from utils.engine.monitoring.binance_private_compare import (
    ACCOUNTS, ShadowStore, audit_path, binding, canonical, envelope,
)

ACCOUNT = "quiet-tail"
EPOCH = "a" * 32
UID = hashlib.sha256(b"121212").hexdigest()
SYM = "HYPEUSDT"
WHEN = 1_790_000_000_000
CONFIG_DIR = Path(__file__).resolve().parents[1] / "utils/engine/deploy/market_feed"


def event(kind, fields=None, *, account=ACCOUNT, epoch=EPOCH, revision=WHEN + 10):
    live = kind not in ("gap", "connected", "snapshot", "heartbeat")
    return envelope(account, epoch, kind, revision if live else None, WHEN + 11,
                    fields or {}, revision if live else None)


def snapshot(start=WHEN, end=WHEN + 9, **fields):
    return event("snapshot", {"started_ms": start, "through_ms": end,
                 "positions": [], "orders": [], "algos": [], **fields})


def push(store, sequence, value, pending=0):
    store.apply(canonical(value), sequence, pending)


def seed(store):
    for seq, value in enumerate([event("gap"), event("connected"), snapshot(), event("heartbeat")], 1):
        push(store, seq, value)


def trade(who=ACCOUNT, epoch=EPOCH, qty="0.10", trade_id=181, status="PARTIALLY_FILLED"):
    return stream.normalize({"e": "ORDER_TRADE_UPDATE", "E": WHEN + 13, "T": WHEN + 12,
                             "api_key": "synthetic private material intentionally discarded",
                             "o": {"s": SYM, "i": 123, "c": "test-native", "X": status,
                                   "z": qty, "S": "SELL", "x": "TRADE", "l": qty, "L": "20",
                                   "t": trade_id, "T": WHEN + 12}},
                            who, epoch, WHEN + 15)


def test_event_shape_account_and_algo_do_not_synthesize_fill(tmp_path):
    p = tmp_path / "shadow.sqlite"
    with closing(ShadowStore(p, ACCOUNT, UID, clock=lambda: WHEN + 25)) as store:
        seed(store)
        other = trade("quiet-core")[1]
        with pytest.raises(ValueError, match="shadow_envelope_invalid"):
            push(store, 5, other)
        algo, = stream.normalize({"e": "ALGO_UPDATE", "E": WHEN + 13, "T": WHEN + 12,
                                  "o": {"s": SYM, "aid": 12, "caid": "test-stop",
                                        "X": "FINISHED", "ai": "123", "R": "discard raw reason"}},
                                 ACCOUNT, EPOCH, WHEN + 15)
        assert algo["fields"]["child_order_id"] == "123"
        push(store, 5, algo)
        push(store, 6, trade()[0])
        assert store.db.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone() == (0,)
        first = trade()[1]
        push(store, 7, first)
        later = trade(trade_id=182, qty="0.20", status="FILLED")[1]
        push(store, 8, later)
        push(store, 9, {**first, "received_ms": WHEN + 20})
        assert b"intentionally discarded" not in canonical(first)
        assert store.db.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone() == (2,)
    with closing(ShadowStore(tmp_path / "other.sqlite", "quiet-core", "d"*64)) as second:
        new_epoch = "b"*32
        push(second, 1, event("gap", account="quiet-core", epoch=new_epoch))
        push(second, 2, trade("quiet-core", new_epoch)[1])
        assert second.db.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone() == (1,)


def test_commit_precedes_ack_and_redelivery_has_no_double_fill(tmp_path):
    path = tmp_path / "shadow.sqlite"
    m = SimpleNamespace(subject=stream.channel(ACCOUNT),
                        metadata=SimpleNamespace(sequence=SimpleNamespace(stream=5), num_pending=0),
                        data=canonical(trade()[1]))

    async def interrupted(*, timeout):
        with closing(sqlite3.connect(path)) as d:
            assert d.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone() == (1,)
        raise ConnectionError("simulated crash after commit before ACK")

    m.ack_sync = interrupted
    with closing(ShadowStore(path, ACCOUNT, UID)) as s:
        seed(s)
        with pytest.raises(ConnectionError):
            asyncio.run(stream.handle_delivery(s, m))
    acknowledgements = []

    async def ack(*, timeout):
        acknowledgements.append(True)

    m.ack_sync = ack
    with closing(ShadowStore(path, ACCOUNT, UID)) as s:
        asyncio.run(stream.handle_delivery(s, m))
        assert s.db.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone() == (1,)
        m.metadata.sequence.stream = 6
        m.data = canonical(event("heartbeat"))
        s.db.execute("CREATE TRIGGER outage BEFORE INSERT ON receipts BEGIN SELECT RAISE(FAIL,'offline'); END")
        with pytest.raises(sqlite3.Error):
            asyncio.run(stream.handle_delivery(s, m))
        assert s.get("sequence") == "5"
        s.db.execute("DROP TRIGGER outage")
    with closing(ShadowStore(path, ACCOUNT, UID)) as s:
        asyncio.run(stream.handle_delivery(s, m))
        assert s.get("sequence") == "6"
        assert acknowledgements == [True, True]


def test_gap_overlap_backlog_stale_events_and_quiet_heartbeat(tmp_path):
    clock = [WHEN + 30]
    position = {"symbol": SYM, "position_side": "BOTH", "quantity": "1"}
    with closing(ShadowStore(tmp_path / "shadow.sqlite", ACCOUNT, UID, clock=lambda: clock[0])) as s:
        seed(s)
        assert s.health()["connection_alive"]  # No account events yet.
        push(s, 5, event("position", position, revision=WHEN + 12))
        push(s, 6, event("position", {**position, "quantity": "2"}, revision=WHEN + 11))
        row = s.db.execute("SELECT fields FROM facts WHERE kind='position'").fetchone()
        assert json.loads(row[0])["quantity"] == "1"
        push(s, 7, event("position", position, revision=WHEN + 5))
        assert s.health()["snapshot_overlap"]
        push(s, 9, event("heartbeat"), pending=88)
        assert s.health()["coverage"] == "broker_gap"
        assert s.health()["consumer_backlog"] == 88
        assert s.health()["broker_gaps"] == 1
        push(s, 10, snapshot(start=WHEN + 14, end=WHEN + 20))
        assert s.health()["coverage"] == "snapshot_baseline"
        assert not s.health()["snapshot_overlap"]
        push(s, 11, event("gap", epoch="b"*32))
        assert not s.health()["connection_alive"]
        assert s.health()["coverage"] == "venue_gap"
        s.bind_stream("old")
        with pytest.raises(ValueError, match="shadow_stream_generation_changed"):
            s.bind_stream("new")
        assert s.health()["coverage"] == "broker_generation_changed"


def test_conflicting_fill_and_invalid_payload_are_diagnosed(tmp_path):
    with closing(ShadowStore(tmp_path / "shadow.sqlite", ACCOUNT, UID)) as s:
        seed(s)
        push(s, 5, trade()[1])
        push(s, 6, trade(qty="0.20")[1])
        assert s.health()["coverage"] == "conflicting_event"
        assert s.health()["conflicts"] == 1
        assert s.db.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone() == (1,)
        s.quarantine(b'{"listenKey":"synthetic-discarded-payload"}', 7, 3)
        assert s.health()["coverage"] == "invalid_event"
        assert s.health()["invalid_events"] == 1
        assert s.get("sequence") == "7"


def prepare_evidence(directory, through):
    key_hash = "f" * 64
    paths = directory / (ACCOUNTS[ACCOUNT][0] + ".sqlite"), audit_path(directory, ACCOUNT)
    with closing(sqlite3.connect(directory / "portfolio-accounts.sqlite")) as db, db:
        db.execute("CREATE TABLE account_uids(name TEXT PRIMARY KEY,uid_hash TEXT)")
        db.execute("INSERT INTO account_uids VALUES (?,?)", (ACCOUNT, UID))
    with closing(sqlite3.connect(paths[0])) as db, db:
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE fills(id TEXT PRIMARY KEY,order_id TEXT,quantity TEXT,price TEXT);
            CREATE TABLE orders(client_id TEXT PRIMARY KEY,role TEXT,response TEXT);
            INSERT INTO orders VALUES ('test-native','STOP','{"actualOrderId":"123"}');
            INSERT INTO fills VALUES ('HYPEUSDT:181','123','0.10','20.0');""")
        db.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("owner", "binance_futures_pilot"), ("portfolio_account_uid", UID),
            ("key_hash", key_hash), ("worker_symbol", SYM)])
    with closing(sqlite3.connect(paths[1])) as db, db:
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
             CREATE TABLE events(kind TEXT,symbol TEXT,native_id TEXT,payload TEXT,observed_ms INTEGER);""")
        db.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("owner", "binance_broker_audit"), ("key_hash", key_hash), ("symbols", SYM),
            ("coverage_through_ms", through), ("coverage_from_ms", WHEN - 1000)])
        for kind in ("positions", "open_orders", "open_algos"):
            db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (kind, "", str(through), "[]", through))
    return paths


def add_audit_fill(audit, through, qty="0.1"):
    with closing(sqlite3.connect(audit)) as db, db:
        db.execute("UPDATE metadata SET value=? WHERE key='coverage_through_ms'", (through,))
        db.execute("DELETE FROM events WHERE kind='trade'")
        row = dict(orderId=123, side="SELL", qty=qty, price="20", time=WHEN + 12)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?)", ("trade", SYM, "181", json.dumps(row), through))


def test_shadow_never_writes_trading_evidence_and_waits_for_two_capture_windows(tmp_path):
    now = [WHEN + 300_000]
    journal, audit = prepare_evidence(tmp_path, WHEN + 30_000)
    assert binding(tmp_path, ACCOUNT) == UID
    with closing(ShadowStore(tmp_path / "shadow.sqlite", ACCOUNT, UID, clock=lambda: now[0])) as s:
        seed(s)
        push(s, 5, trade()[1])
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (journal, audit)]
        assert s.compare(tmp_path)["timing"] == 1
        assert s.compare(tmp_path)["timing"] == 1
        assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (journal, audit)] == hashes
        add_audit_fill(audit, WHEN + 180_000, qty="0.2")
        assert s.compare(tmp_path)["persistent"] == 1
        add_audit_fill(audit, WHEN + 290_000)
        assert s.compare(tmp_path)["match"] == 1
        with closing(sqlite3.connect(audit)) as db, db:
            db.execute("UPDATE metadata SET value=? WHERE key='key_hash'", ("a"*64,))
        assert s.compare(tmp_path)["state_verification"] == "incomplete"



def test_matching_current_state_and_snapshot_overlap_are_distinct(tmp_path):
    now = WHEN + 40_000
    _, audit = prepare_evidence(tmp_path, WHEN + 30_000)
    with closing(sqlite3.connect(audit)) as db, db:
        position = [{"symbol": SYM, "positionSide": "BOTH", "positionAmt": "1.0"}]
        db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                   ("positions", "", "new", json.dumps(position), WHEN + 30_001))
    with closing(ShadowStore(tmp_path / "state.sqlite", ACCOUNT, UID, clock=lambda: now)) as s:
        seed(s)
        push(s, 5, event("position", {"symbol": SYM, "position_side": "BOTH", "quantity": "1"},
                         revision=WHEN + 12))
        assert s.compare(tmp_path)["state_verification"] == "matched"
        assert s.compare(tmp_path, backlog=2)["state_verification"] == "incomplete"
        push(s, 6, event("position", {"symbol": SYM, "position_side": "BOTH", "quantity": "1"},
                         revision=WHEN + 7))
        assert s.compare(tmp_path)["state_verification"] == "incomplete"

def test_key_lifecycle_uses_budget_without_using_order_allowance(tmp_path, monkeypatch):
    p = tmp_path / "limiter.sqlite"
    p.touch()
    budget = RestBudget(p)
    requests = []
    key = "x" * 48

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        if method == "PUT":
            return SimpleNamespace(status_code=429, headers={"Retry-After": "15"}, json=lambda: {"code": -1003})
        return SimpleNamespace(status_code=200, headers={}, json=lambda: {"listenKey": key})

    reader = stream.BinancePrivateReads(session=SimpleNamespace(request=fake_request), budget=budget)
    monkeypatch.setattr(reader, "_key", "never-print-test-key")
    monkeypatch.setattr(reader, "load_credentials", lambda: None)
    assert request_weight("POST", stream.LISTEN, {}) == request_weight("PUT", stream.LISTEN, {}) == 1
    assert reader.listen("POST") == key
    with pytest.raises(RestDeferred):
        reader.listen("PUT")
    with pytest.raises(RestDeferred, match="venue_cooldown"):
        reader.listen("POST")
    assert len(requests) == 2
    assert all(key not in x[1] for x in requests)
    with closing(sqlite3.connect(p)) as db:
        assert db.execute("SELECT sum(weight),sum(orders) FROM reservations").fetchone() == (2, 0)
    with pytest.raises(ValueError):
        reader.listen("DELETE")


def test_manager_quiet_session_uses_pong_and_marks_gap_on_expiry(monkeypatch):
    controls = []
    key = "x"*48
    auth = []
    class FakeReads:
        def verify_uid(self, uid):
            auth.append(uid)
        def listen(self, method):
            assert method == "POST"
            return key
        def baseline(self):
            return snapshot()["fields"]

    class Socket:
        response = SimpleNamespace(status_code=101)
        request = SimpleNamespace(path="/private/ws/" + key)
        receives = 0

        async def ping(self):
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(None)
            return fut

        async def recv(self):
            self.receives += 1
            if self.receives == 1:
                raise TimeoutError
            return json.dumps({"e": "listenKeyExpired", "listenKey": key})

    @asynccontextmanager
    async def connector(url, **kwargs):
        assert url == "wss://fstream.binance.com/private/ws/" + key
        yield Socket()

    async def collect(_, item):
        controls.append(item)

    monkeypatch.setattr(stream, "publish", collect)

    async def run():
        for _ in range(2):
            with pytest.raises(stream.ListenKeyExpired):
                await stream.stream_session(FakeReads(), None, ACCOUNT, UID, connector=connector)
    asyncio.run(run())
    assert [x["kind"] for x in controls] == ["gap", "connected", "snapshot", "heartbeat", "gap"] * 2
    assert controls[0]["epoch"] != controls[5]["epoch"]
    assert auth == [UID, UID]
    assert key.encode() not in canonical(controls)


def test_nats_accounts_actual_acl_existing_public_store_and_ack(tmp_path):
    executable = os.environ.get("GG_TEST_NATS_SERVER")
    image = os.environ.get("GG_TEST_NATS_DOCKER_IMAGE")
    if not executable and not image:
        pytest.skip("set GG_TEST_NATS_SERVER or GG_TEST_NATS_DOCKER_IMAGE to test isolated JetStream")
    nats = pytest.importorskip("nats")
    from nats.js.errors import NotFoundError

    with socket.socket() as bind:
        bind.bind(("127.0.0.1", 0))
        port = bind.getsockname()[1]
    url = f"nats://127.0.0.1:{port}"
    public_conf = (CONFIG_DIR / "nats.conf").read_text()
    private_conf = (CONFIG_DIR / "nats.private.conf").read_text()
    env = dict(os.environ)
    passwords = {"GG_PUBLIC_NATS_FALLBACK_PASSWORD": "synthetic-public-fallback-0"}
    for group in ACCOUNTS:
        for role in ("manager", "consumer", "init"):
            passwords[f"GG_PRIVATE_{group.upper().replace('-', '_')}_{role.upper()}"] = (
                f"synthetic-{group}-{role}-only")
    env.update(passwords)

    @asynccontextmanager
    async def server(config):
        path = tmp_path / "nats.conf"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if image:
            name = "gg-private-fixture-" + uuid4().hex
            path.write_text(config.replace("http_port: 8222", "")
                            .replace("/data/jetstream", "/shadow-fixture/jetstream"))
            fixtures = tmp_path / "synthetic.env"
            fixtures.write_text("".join(f"{k}={v}\n" for k, v in passwords.items()))
            command = ["docker", "run", "--pull=never", "--rm", "--name", name,
                       "-p", f"127.0.0.1:{port}:4222", "--env-file", str(fixtures),
                       "--mount", f"type=bind,source={tmp_path},target=/shadow-fixture",
                       image, "-c", "/shadow-fixture/nats.conf"]
        else:
            path.write_text(config.replace("port: 4222", f'listen: "127.0.0.1:{port}"')
                            .replace("http_port: 8222", "")
                            .replace("/data/jetstream", json.dumps((tmp_path / "jetstream").as_posix())))
            command = [executable, "-c", str(path)]
        with (tmp_path / "nats-output.log").open("wb") as logs:
            process = subprocess.Popen(command, env=env, stdout=logs, stderr=logs, creationflags=flags)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        pytest.fail((tmp_path / "nats-output.log").read_text(errors="replace"))
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2) as probe:
                            if probe.recv(256).startswith(b"INFO "):
                                break
                    except OSError:
                        pass
                    await asyncio.sleep(.05)
                else:
                    pytest.fail("isolated NATS did not start")
                yield
            finally:
                if image:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10,
                                   creationflags=flags)
                if process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 10)

    async def dial(group, role):
        f = tmp_path / f"{group}.{role}"
        f.write_text(passwords[f"GG_PRIVATE_{group.upper().replace('-', '_')}_{role.upper()}"])
        return await stream.connection(url, f"{group.replace('-', '_')}_{role}", f)

    async def backup_public(nc):
        inbox = nc.new_inbox()
        sub = await nc.subscribe(inbox)
        await nc.flush()
        response = await nc.request("$JS.API.STREAM.SNAPSHOT.EXISTING", canonical({
            "deliver_subject": inbox, "no_consumers": False, "chunk_size": 32768,
        }), timeout=3)
        metadata = json.loads(response.data)
        assert "error" not in metadata, metadata
        chunks = []
        while True:
            msg = await sub.next_msg(timeout=3)
            if not msg.data:
                return metadata, chunks
            chunks.append(msg.data)
            if msg.reply:
                await msg.respond(b"")

    async def restore_public(nc, backup):
        metadata, chunks = backup
        response = await nc.request("$JS.API.STREAM.RESTORE.EXISTING", canonical({
            "config": metadata["config"], "state": metadata["state"],
        }), timeout=3)
        reply = json.loads(response.data)
        assert "error" not in reply, reply
        for chunk in [*chunks, b""]:
            response = await nc.request(reply["deliver_subject"], chunk, timeout=3)
            if response.data:
                assert "error" not in json.loads(response.data), response.data

    async def run():
        async with server(public_conf):
            with pytest.raises(ValueError, match="private_nats_identity_unverified"):
                await dial(ACCOUNT, "manager")
            n = await nats.connect(url)
            try:
                js = n.jetstream()
                await js.add_stream(name="EXISTING", subjects=["market.sample"])
                await js.publish("market.sample", b"old")
                await js.publish("market.sample", b"pending")
                sub = await js.pull_subscribe("market.sample", durable="public_worker")
                first, = await sub.fetch(1, timeout=1)
                await first.ack_sync()
                backup = await backup_public(n)
            finally:
                await n.close()
        async with server(private_conf):
            public = await nats.connect(url)
            clients = [public]
            try:
                for group in ACCOUNTS:
                    admin = await dial(group, "init")
                    clients.append(admin)
                    await stream.provision(admin.jetstream(), group)
                with pytest.raises(NotFoundError):
                    await public.jetstream().stream_info(stream.STREAM)
                public_js = public.jetstream()
                with pytest.raises(NotFoundError):
                    await public_js.stream_info("EXISTING")
                await restore_public(public, backup)
                restored = await public_js.consumer_info("EXISTING", "public_worker")
                assert restored.ack_floor.stream_seq == 1
                sub = await public_js.pull_subscribe_bind(stream="EXISTING", consumer="public_worker")
                pending, = await sub.fetch(1, timeout=1)
                assert pending.data == b"pending"
                await pending.ack_sync()
                assert (await public_js.publish("market.sample", b"new")).seq == 3
                manager, consumer, unrelated = [await dial(g, r) for g, r in (
                    (ACCOUNT, "manager"), (ACCOUNT, "consumer"), ("quiet-core", "consumer"))]
                clients += [manager, consumer, unrelated]
                private = consumer.jetstream()
                other = unrelated.jetstream()
                assert (await private.stream_info(stream.STREAM)).config.subjects == [stream.channel(ACCOUNT)]
                assert (await other.stream_info(stream.STREAM)).config.subjects == [stream.channel("quiet-core")]
                with pytest.raises(nats.errors.TimeoutError):
                    await manager.jetstream().publish(stream.channel("quiet-core"), b"denied", timeout=.3)
                with pytest.raises(nats.errors.TimeoutError):
                    await private.publish(stream.channel(ACCOUNT), b"denied", timeout=.3)
                with pytest.raises(nats.errors.TimeoutError):
                    await manager.jetstream(timeout=.3).stream_info(stream.STREAM)
                await stream.publish(manager.jetstream(), event("gap"))
                await stream.publish(manager.jetstream(), event("connected"))
                await stream.publish(manager.jetstream(), event("heartbeat"))
                await stream.publish(manager.jetstream(), event("heartbeat"))
                assert (await other.stream_info(stream.STREAM)).state.messages == 0
                assert (await private.stream_info(stream.STREAM)).state.messages == 4
                sub = await private.pull_subscribe_bind(stream=stream.STREAM, consumer=stream.DURABLE)
                with closing(ShadowStore(tmp_path / "real.sqlite", ACCOUNT, UID)) as store:
                    for _ in range(4):
                        m, = await sub.fetch(1, timeout=1)
                        await stream.handle_delivery(store, m)
                    assert store.get("sequence") == "4"
                state = await private.consumer_info(stream.STREAM, stream.DURABLE)
                assert state.ack_floor.stream_seq == 4
            finally:
                await asyncio.gather(*(c.close() for c in clients))
    asyncio.run(run())
