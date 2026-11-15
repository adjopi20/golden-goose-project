"""HTTP-boundary and shared-budget regression tests with no Binance access."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from multiprocessing import get_context
from pathlib import Path
import sqlite3
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import yaml

from utils.engine.adapters.binance_execution import ACKNOWLEDGEMENT, VenueError, submission_deadline
from utils.engine.adapters.binance_public import BinancePublic
from utils.engine.adapters.binance_rest_budget import RestBudget, RestDeferred, request_weight
from utils.engine.monitoring.binance_audit import BinanceAuditReads
from utils.engine.execution.burst_futures import BurstFuturesOrders


def acquire_many(path, service):
    budget = RestBudget(path, service=service, futures=100, clock=lambda: 120)
    accepted = 0
    for _ in range(20):
        try:
            budget.acquire("GET", "/fapi/v1/aggTrades", {})
            accepted += 1
        except RestDeferred as e:
            assert e.reason in ("weight_budget", "coordinator_unavailable")
    return accepted


def test_atomic_across_spawned_processes(tmp_path):
    path = tmp_path / "shared.sqlite"
    path.touch()
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        assert sum(pool.map(acquire_many, [str(path)]*4, ["audit", "collector", "worker1", "worker2"])) == 5
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT sum(weight) FROM reservations").fetchone()[0] == 100


def pair(tmp_path, **kwargs):
    path = tmp_path / "budget.sqlite"
    path.touch()
    now = [120.]
    a = RestBudget(path, clock=lambda: now[0], service="worker", account="burst", **kwargs)
    b = RestBudget(path, clock=lambda: now[0], service="audit", account="quiet-core", **kwargs)
    return a, b, now


def response(status, **headers):
    return SimpleNamespace(status_code=status, headers=headers)


def test_weight_and_parameter_boundaries():
    assert [request_weight("GET", "/fapi/v1/klines", {"limit": n})
            for n in (1, 99, 100, 499, 500, 1000, 1001, 1500)] == [1, 1, 2, 2, 5, 5, 10, 10]
    assert request_weight("GET", "/fapi/v1/openOrders", {}) == 40
    assert request_weight("GET", "/fapi/v1/openOrders", {"symbol": "HYPEUSDT"}) == 1
    assert request_weight("GET", "/fapi/v1/ticker/bookTicker", {}) == 5
    assert request_weight("GET", "/fapi/v1/ticker/bookTicker", {"symbol": "HYPEUSDT"}) == 2
    assert request_weight("POST", "/fapi/v1/algoOrder", {}) == 0
    with pytest.raises(ValueError):
        request_weight("GET", "/unexpected", {})


def test_priority_headers_and_rolling_restart(tmp_path):
    a, b, now = pair(tmp_path, futures=100)
    t = a.acquire("GET", "/fapi/v1/time", {})
    a.observe(t, response(200, **{"X-MBX-USED-WEIGHT-1M": "70"}))
    with pytest.raises(RestDeferred, match="weight_budget"):
        b.acquire("GET", "/fapi/v1/time", {}, background=True)
    with pytest.raises(RestDeferred, match="weight_budget"):
        a.acquire("POST", "/fapi/v1/order", {}, background=True)
    a.acquire("POST", "/fapi/v1/algoOrder", {})  # protection
    a.acquire("POST", "/fapi/v1/order", {})      # lane used by reduce-only exits
    a.acquire("GET", "/fapi/v1/income", {})
    with pytest.raises(RestDeferred, match="weight_budget"):
        a.acquire("POST", "/fapi/v1/order", {})  # close is still gated at saturation
    now[0] = 179.999
    with pytest.raises(RestDeferred):
        a.acquire("GET", "/fapi/v1/time", {})
    # Simulate a new process opening the same DB after the UTC minute changed.
    now[0] = 180.
    restarted = RestBudget(a.path, futures=100, clock=lambda: now[0])
    with pytest.raises(RestDeferred, match="weight_budget"):
        for _ in range(4):
            restarted.acquire("GET", "/fapi/v1/aggTrades", {})
    now[0] = 271.
    restarted.acquire("GET", "/fapi/v1/aggTrades", {})


def test_shared_429_ban_and_account_order_counter_are_distinct(tmp_path):
    a, b, now = pair(tmp_path)
    t = a.acquire("POST", "/fapi/v1/order", {})
    a.observe(t, response(429, **{"Retry-After": "9"}), {"code": -1015})
    with pytest.raises(RestDeferred, match="venue_cooldown"):
        a.acquire("POST", "/fapi/v1/algoOrder", {})
    a.acquire("GET", "/fapi/v1/algoOrder", {})
    b.acquire("POST", "/fapi/v1/order", {})
    t = b.acquire("GET", "/fapi/v1/time", {})
    b.observe(t, response(429, **{"Retry-After": "15"}), {"code": -1003})
    for _ in range(7):
        with pytest.raises(RestDeferred, match="venue_cooldown"):
            a.acquire("GET", "/fapi/v3/positionRisk", {})
    a.acquire("GET", "/api/v3/account", {})     # independent Spot accounting
    now[0] = 135
    t = a.acquire("GET", "/fapi/v1/time", {})
    banned_until = 1_790_000_000_000
    a.observe(t, response(418), {"code": -1003, "msg": f"IP banned until {banned_until}."})
    with pytest.raises(RestDeferred, match="venue_cooldown") as e:
        b.acquire("GET", "/api/v3/account", {})
    assert e.value.delay == banned_until/1000 - now[0]
    a.flush_metrics()
    b.flush_metrics()
    with closing(sqlite3.connect(a.path)) as db:
        assert db.execute("SELECT sum(count) FROM metrics WHERE event='http_429'").fetchone()[0] == 2
        assert db.execute("SELECT sum(count) FROM metrics WHERE event='http_418'").fetchone()[0] == 1


def test_metric_failure_cannot_discard_header_or_block_reconciliation(tmp_path, caplog):
    a, b, _ = pair(tmp_path, futures=100)
    ticket = a.acquire("GET", "/fapi/v3/positionRisk", {})
    with closing(sqlite3.connect(a.path)) as db:
        db.execute("""CREATE TRIGGER offline_metrics BEFORE INSERT ON metrics
                   BEGIN SELECT RAISE(FAIL, 'test telemetry failure'); END""")
    a.observe(ticket, response(200, **{"X-MBX-USED-WEIGHT-1M": "70"}))
    a.flush_metrics()
    assert "SQLITE_CONSTRAINT_TRIGGER" in caplog.text
    with pytest.raises(RestDeferred, match="weight_budget"):
        b.acquire("GET", "/fapi/v1/time", {}, background=True)
    a.acquire("GET", "/fapi/v3/positionRisk", {})


@pytest.mark.parametrize("status", [200, 429, 418])
def test_locked_response_recovers_without_fixed_backoff_and_preserves_cooldown(tmp_path, status, caplog):
    a, b, now = pair(tmp_path)
    ticket = a.acquire("GET", "/fapi/v1/time", {})
    with closing(sqlite3.connect(a.path)) as db:
        db.execute("BEGIN IMMEDIATE")
        a.observe(ticket, response(status, **{"Retry-After": "10", "X-MBX-USED-WEIGHT-1M": "100"}))
        assert "SQLITE_BUSY" in caplog.text
        with pytest.raises(RestDeferred, match="coordinator_response_pending"):
            a.acquire("GET", "/fapi/v1/time", {})
    if status == 200:
        a.acquire("GET", "/fapi/v1/time", {})
    else:
        with pytest.raises(RestDeferred, match="venue_cooldown"):
            a.acquire("GET", "/fapi/v1/time", {})
        endpoint = "/api/v3/account" if status == 418 else "/fapi/v1/time"
        with pytest.raises(RestDeferred, match="venue_cooldown"):
            b.acquire("GET", endpoint, {})
        now[0] += 10
        b.acquire("GET", endpoint, {})


def reconcile_burst(path, service):
    budget = RestBudget(path, service=service, clock=lambda: 120)
    def acquire():
        deadline = monotonic() + 8
        while True:
            try:
                return budget.acquire("GET", "/fapi/v1/time", {})
            except RestDeferred as error:
                assert error.reason in ("coordinator_unavailable", "coordinator_response_pending")
                assert monotonic() < deadline, "budget did not recover from concurrent writes"
                sleep(.015)

    for _ in range(16):
        budget.observe(acquire(), response(200))
    acquire()  # The next admission must persist any deferred observation first.
    budget.flush_metrics()


def test_concurrent_worker_and_audit_observations_commit(tmp_path):
    path = tmp_path / "portfolio.sqlite"
    path.touch()
    labels = [f"worker{i}" for i in range(7)] + ["audit"]
    with ProcessPoolExecutor(max_workers=8, mp_context=get_context("spawn")) as pool:
        list(pool.map(reconcile_burst, [str(path)] * len(labels), labels))
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*), sum(done) FROM reservations").fetchone() == (136, 128)


def test_wrong_config_missing_db_and_lock_never_send(tmp_path):
    a, b, _ = pair(tmp_path)
    a.acquire("GET", "/fapi/v1/time", {})
    with pytest.raises(RestDeferred, match="settings_mismatch"):
        RestBudget(a.path, futures=1800).acquire("GET", "/fapi/v1/time", {})
    api = BinancePublic(budget=b)
    api.http.get = Mock()
    with closing(sqlite3.connect(a.path)) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        with pytest.raises(RestDeferred, match="unavailable"):
            api.server_time()
    api.budget = RestBudget(tmp_path / "missing.sqlite")
    with pytest.raises(RestDeferred, match="unavailable"):
        api.server_time()
    api.http.get.assert_not_called()


def test_signed_timeout_once_and_audit_cooldown_covers_other_containers(tmp_path):
    a, b, now = pair(tmp_path)
    path = tmp_path / "fake-secret"
    path.write_text("SyntheticApiCredential123456")
    http = Mock()
    http.request.side_effect = requests.ConnectTimeout("never log a signed URL")
    venue = BurstFuturesOrders(execution_enabled=True, acknowledgement=ACKNOWLEDGEMENT,
                               api_key="A"*64, hmac_secret_file=path, session=http, budget=a)
    venue.offset = 0
    params = dict(symbol="HYPEUSDT", side="BUY", positionSide="BOTH", type="MARKET",
                  quantity=".01", newClientOrderId="ggp-test-entry", newOrderRespType="RESULT")
    with pytest.raises(VenueError) as e:
        venue.submit("ENTRY", params)
    assert e.value.uncertain and "signed URL" not in str(e.value)
    http.request.assert_called_once()
    reply = requests.Response()
    reply.status_code, reply._content = 429, b'{"code":-1003}'
    reply.headers["Retry-After"] = "20"
    audit_http = Mock()
    audit_http.get.return_value = reply
    audit = BinanceAuditReads(api_key="A"*64, hmac_secret_file=path, session=audit_http, budget=b)
    audit.offset = 0
    with pytest.raises(RestDeferred):
        audit.get("/fapi/v1/allOrders", symbol="HYPEUSDT")
    with pytest.raises(RestDeferred):
        venue.submit("ENTRY", params)
    http.request.assert_called_once()
    audit_http.get.assert_called_once()
    now[0] += 20
    a.acquire("GET", "/fapi/v3/positionRisk", {})
    with submission_deadline(1), pytest.raises(RestDeferred, match="entry_validity_elapsed"):
        venue.submit("ENTRY", params)
    http.request.assert_called_once()  # Admission cannot send a stale setup.


def test_compose_all_portfolio_workers_audit_and_collector_share_mount():
    dir_pattern = "${GG_BINANCE_REST_BUDGET_DIR:"
    root = Path(__file__).resolve().parents[1]
    workers = yaml.safe_load((root / "live_trading/deploy/hype-burst.compose.yaml").read_text())
    feed = yaml.safe_load((root / "utils/engine/deploy/market_feed/compose.yaml").read_text())
    services = list(workers["services"].values())
    portfolios = [s for s in services if "--portfolio-model" in s["command"]]
    from live_trading.burst_runtime import portfolio_models
    assert {s["command"][1] for s in portfolios} == set(portfolio_models())
    assert len(portfolios) == 7
    assert sorted(s["environment"]["BINANCE_REST_ACCOUNT"] for s in portfolios) == (
        ["burst"]*3 + ["quiet-core"]*3 + ["quiet-tail"])
    configs = services + [feed["services"]["binance-collector"]]
    for config in configs:
        assert config["environment"]["BINANCE_REST_BUDGET_DB"] == "/rest-budget/binance.sqlite"
        mount = next(v for v in config["volumes"] if isinstance(v, dict) and v["target"] == "/rest-budget")
        assert mount["source"].startswith(dir_pattern) and not mount["bind"]["create_host_path"]
        assert config["group_add"] == ["${GG_BINANCE_REST_GID:-19901}"]
