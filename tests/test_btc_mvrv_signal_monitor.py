"""Offline regressions for the pinned operational BTC signal monitor."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
import requests

from live_trading.live_model.btc_mvrv import strategy as monitor
from utils.engine.monitoring import mvrv_notify as notify
from utils.engine.state.mvrv import connect, get_meta, message, set_meta
from utils.model_registry import verify_model


NOW = "2026-11-17T03:00:00+00:00"
NEXT = "2026-11-18T03:00:00+00:00"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must use synthetic HTTP responses")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:synthetic-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def raw(end="2026-11-16"):
    dates = pd.date_range(monitor.ORIGIN, end)
    index = np.arange(len(dates))
    frame = pd.DataFrame(dict(time=dates.strftime("%Y-%m-%d"), PriceUSD=100 + index / 10,
                              CapMrktCurUSD=1e7 + index * 10000,
                              CapMVRVCur=1.2 + .3 * np.sin(index / 50)))
    frame.loc[frame.time == "2026-11-09", "CapMVRVCur"] = 1.3937829711692236
    return frame


def contents(c, table):
    return [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_numeric_drift_preserves_history_and_recovers_normal_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "fetch", lambda *args: raw())
    with closing(connect(tmp_path / "signals.sqlite")) as c:
        monitor.checked_cycle(c, NOW)
        saved, decisions = contents(c, "raw"), contents(c, "decisions")
        with c:
            set_meta(c, "error", "Historical revision detected")
            message(c, "signal-error:2026-11-18", "previous data error")
        incoming = raw("2026-11-17")
        incoming.loc[incoming.time == "2026-11-09", "CapMVRVCur"] = 1.3937829935
        monkeypatch.setattr(monitor, "fetch", lambda *args: incoming)
        result = monitor.checked_cycle(c, NEXT)
        assert contents(c, "raw")[:-1] == saved
        assert contents(c, "raw")[-1][0] == "2026-11-17"
        assert contents(c, "decisions")[:2] == decisions
        assert len(contents(c, "decisions")) == 4
        assert result["last_data_date"] == "2026-11-17" and result["error"] == ""
        assert result["unsent_notifications"] == 1
        assert result["orders_enabled"] is False
        for key in ("signal-error:2026-11-18", "signal-status:2026-11-16"):
            assert c.execute("SELECT delivered_at FROM outbox WHERE id=?", (key,)).fetchone()[0].startswith("superseded:")
        assert c.execute("SELECT COUNT(*) FROM vintages").fetchone()[0] == 2
        archived = json.loads(c.execute("SELECT payload FROM vintages ORDER BY id DESC LIMIT 1").fetchone()[0])
        assert next(row for row in archived if row["time"] == "2026-11-09")["CapMVRVCur"] == 1.3937829935
        for table in ("accounts", "actions", "fills", "marks"):
            assert not contents(c, table)


@pytest.mark.parametrize("metric,change", [("CapMVRVCur", 1e-6), ("PriceUSD", 1e-8), ("CapMrktCurUSD", 1e-8)])
def test_material_revision_archived_without_partial_history_or_decisions(tmp_path, monkeypatch, metric, change):
    monkeypatch.setattr(monitor, "fetch", lambda *args: raw())
    with closing(connect(tmp_path / "signals.sqlite")) as c:
        monitor.checked_cycle(c, NOW)
        saved = {name: contents(c, name) for name in ("raw", "decisions", "outbox")}
        incoming = raw("2026-11-17")
        incoming.loc[incoming.time == "2026-11-16", metric] *= 1 + change
        monkeypatch.setattr(monitor, "fetch", lambda *args: incoming)
        with pytest.raises(monitor.DataError, match="Historical revision"):
            monitor.checked_cycle(c, NEXT)
        for name in ("raw", "decisions"):
            assert contents(c, name) == saved[name]
        assert contents(c, "outbox")[:1] == saved["outbox"]
        assert get_meta(c, "last_data_date") == "2026-11-16"
        assert c.execute("SELECT COUNT(*) FROM vintages").fetchone()[0] == 2
        assert get_meta(c, "error").startswith("Historical revision")


@pytest.mark.parametrize("ratio", [.25, 1.3937829711692236, 10.0])
@pytest.mark.parametrize("multiple,accepted", [(.99, True), (1.01, False)])
def test_ratio_tolerance_is_bounded_at_different_scales(tmp_path, ratio, multiple, accepted):
    original = raw("2010-07-19")
    original.loc[1, "CapMVRVCur"] = ratio
    with closing(connect(tmp_path / "signals.sqlite")) as c:
        with c:
            monitor.merged_history(c, original, NOW)
        saved = contents(c, "raw")
        incoming = original.copy()
        incoming.loc[1, "CapMVRVCur"] += multiple * (ratio * 5e-8 + 1e-10)
        if accepted:
            with c:
                merged = monitor.merged_history(c, incoming, NEXT)
            assert merged.CapMVRVCur.iloc[1] == ratio
        else:
            with pytest.raises(monitor.DataError, match="Historical revision"), c:
                monitor.merged_history(c, incoming, NEXT)
        assert contents(c, "raw") == saved


def test_stale_source_still_rolls_back_and_is_archived(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "fetch", lambda *args: raw())
    with closing(connect(tmp_path / "signals.sqlite")) as c:
        monitor.checked_cycle(c, NOW)
        saved = contents(c, "raw"), contents(c, "decisions")
        with pytest.raises(monitor.DataError, match="Expected UTC data"):
            monitor.checked_cycle(c, NEXT)
        assert (contents(c, "raw"), contents(c, "decisions")) == saved
        assert c.execute("SELECT COUNT(*) FROM vintages").fetchone()[0] == 2


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def success():
    return Response(200, {"ok": True, "result": {"message_id": 11, "chat": {"id": 42}}})


@pytest.mark.parametrize("failure,category", [
    (Response(502, "synthetic-secret response body"), "HTTP_502"),
    (requests.Timeout("https://api.telegram.org/bot123:synthetic-secret/sendMessage"), "TIMEOUT"),
    (requests.ConnectionError("synthetic-secret connection details"), "CONNECTION_ERROR"),
    (requests.RequestException("synthetic-secret request details"), "REQUEST_ERROR"),
    (Response(200, ValueError("synthetic-secret malformed JSON")), "INVALID_RESPONSE"),
    (Response(200, requests.exceptions.JSONDecodeError("synthetic-secret", "private body", 0)), "INVALID_RESPONSE"),
    (Response(200, {"ok": False, "description": "synthetic-secret API body"}), "API_REJECTED"),
    (Response(200, {"ok": True}), "UNCONFIRMED_DELIVERY"),
    (Response(200, {"ok": True, "result": {"message_id": 11, "chat": {"id": 43}}}), "UNCONFIRMED_DELIVERY"),
])
def test_safe_delivery_failure_preserves_outbox_then_recovers(tmp_path, monkeypatch, failure, category):
    calls = []
    replies = iter([failure, success()])

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(notify.requests, "post", post)
    with closing(connect(tmp_path / "signals.sqlite")) as c:
        with c:
            message(c, "signal-error:2026-11-19", "data failure notification")
        monitor.notify(c, monitor.utc("2026-11-19T03:00:00+00:00"))
        assert get_meta(c, "notification_error") == category + ": Telegram delivery failed; outbox retained"
        assert "synthetic-secret" not in get_meta(c, "notification_error")
        assert c.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None
        assert len(calls) == 1
        monitor.notify(c, monitor.utc("2026-11-19T03:01:00+00:00"))
        assert get_meta(c, "notification_error") == ""
        assert c.execute("SELECT delivered_at FROM outbox").fetchone()[0] == "2026-11-19T03:01:00+00:00"
        assert calls[0] == calls[1]


def test_unexpected_exception_text_is_never_persisted(tmp_path, monkeypatch):
    def bad(*args):
        raise RuntimeError("https://api.telegram.org/bot123:synthetic-secret/sendMessage")

    monkeypatch.setattr(monitor, "deliver", bad)
    with closing(connect(tmp_path / "signals.sqlite")) as c:
        monitor.notify(c, monitor.utc(NOW))
        assert get_meta(c, "notification_error") == "RuntimeError: Telegram delivery failed; outbox retained"


def test_existing_run_loop_retries_delivery_after_sixty_seconds(tmp_path, monkeypatch):
    path = tmp_path / "signals.sqlite"
    with closing(connect(path)) as c, c:
        message(c, "signal-error:2026-11-19", "data failure notification")
    waits, calls = [], []

    class Stop:
        def is_set(self):
            return len(waits) >= 2

        def wait(self, seconds):
            waits.append(seconds)

        def set(self):
            pass

    def post(*args, **kwargs):
        calls.append(True)
        return Response(502, "synthetic-secret") if len(calls) == 1 else success()

    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(monitor.threading, "Event", Stop)
    monkeypatch.setattr(monitor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(monitor, "refresh_due", lambda *args: False)
    monkeypatch.setattr(sys, "argv", ["strategy", "run", "--database", str(path)])
    monitor.main()
    assert waits == [60, 60] and len(calls) == 2
    with closing(connect(path)) as c:
        assert get_meta(c, "notification_error") == ""
        assert c.execute("SELECT delivered_at FROM outbox").fetchone()[0]


def test_operational_strategy_and_release_pins_verify():
    package = verify_model(monitor.__file__)
    root = Path(__file__).resolve().parents[1]
    release = json.loads((root / package["engine_release"]["path"]).read_bytes())
    assert release["release"] == "shared_engine_20261119_v11"
    for source, expected in package["dependencies"].items():
        assert hashlib.sha256((root / source).read_bytes()).hexdigest() == expected
