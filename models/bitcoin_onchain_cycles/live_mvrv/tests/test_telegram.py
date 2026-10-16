import json
from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from live_mvrv import main, notify


class Fake:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], kwargs["json"]))
        status, body = next(self.responses)
        class Response:
            status_code = status
            def json(self): return body
        return Response()


def credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-example")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")


def test_discovery_redacts_personal_message_and_never_acknowledges(monkeypatch):
    credentials(monkeypatch)
    session = Fake([(200, {"ok": True, "result": {"is_bot": True, "username": "mvrv_test_bot"}}),
                    (200, {"ok": True, "result": {"url": ""}}),
                    (200, {"ok": True, "result": [{"message": {"text": "private text", "chat": {"type": "private", "id": 42}}},
                                                   {"message": {"chat": {"type": "group", "id": -10}}}]})])
    result = notify.discover(session)
    assert result["private_chat_ids"] == [42]
    assert "private text" not in json.dumps(result)
    assert all("offset" not in parameters for _, parameters in session.calls)


def test_existing_webhook_not_deleted(monkeypatch):
    credentials(monkeypatch)
    session = Fake([(200, {"ok": True, "result": {"is_bot": True}}),
                    (200, {"ok": True, "result": {"url": "https://private-hook.invalid"}})])
    with pytest.raises(ValueError, match="NOT removed"):
        notify.discover(session)
    assert len(session.calls) == 2


def test_explicit_test_message_is_not_trade_signal(monkeypatch):
    credentials(monkeypatch)
    session = Fake([(200, {"ok": True, "result": {"message_id": 1, "chat": {"id": 42}}})])
    assert notify.test_notification("2026-10-03T00:00:00+00:00", session)["status"] == "telegram_api_accepted"
    method, parameters = session.calls[0]
    assert method == "sendMessage" and parameters["chat_id"] == 42
    assert "BUKAN sinyal trading" in parameters["text"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_api_error_does_not_print_token_or_body(monkeypatch, status):
    credentials(monkeypatch)
    session = Fake([(status, {"ok": False, "description": "secret-example private response"})])
    with pytest.raises(RuntimeError) as error:
        notify.test_notification("now", session)
    assert "secret-example" not in str(error.value)
    assert "private response" not in str(error.value)


def test_connection_error_redacted(monkeypatch):
    credentials(monkeypatch)
    class Bad:
        def post(self, *a, **kw): raise requests.ConnectionError("secret-example URL")
    with pytest.raises(RuntimeError) as error:
        notify.test_notification("now", Bad())
    assert "secret-example" not in str(error.value)


def test_disabled_test_does_not_send(monkeypatch):
    credentials(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    session = Fake([])
    with pytest.raises(ValueError, match="authorize"):
        notify.test_notification("now", session)
    assert not session.calls


def test_wrong_recipient_confirmation_fails(monkeypatch):
    credentials(monkeypatch)
    session = Fake([(200, {"ok": True, "result": {"message_id": 1, "chat": {"id": 43}}})])
    with pytest.raises(RuntimeError, match="recipient"):
        notify.test_notification("now", session)


@pytest.mark.parametrize("action", ["telegram-discover", "telegram-test"])
def test_setup_cli_never_opens_database(monkeypatch, action, capsys):
    monkeypatch.setattr(sys, "argv", ["main", action])
    monkeypatch.setattr(main, "connect", lambda *a: pytest.fail("Trading DB must not be opened"))
    monkeypatch.setattr(main, "discover", lambda: {"private_chat_ids": [42]})
    monkeypatch.setattr(main, "test_notification", lambda *_: {"status": "telegram_api_accepted"})
    main.main()
    assert json.loads(capsys.readouterr().out)
