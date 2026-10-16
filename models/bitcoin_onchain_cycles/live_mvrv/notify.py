"""Optional Telegram outbox. Never log secrets or mark proposals as fills."""
import os

import requests


def _api(method, parameters, session=requests):
    """Never expose token-bearing URLs, exception strings or response bodies."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN missing; configure it locally, never in chat")
    try:
        response = session.post(f"https://api.telegram.org/bot{token}/{method}",
                                json=parameters, timeout=(10, 20))
        if response.status_code != 200:
            raise RuntimeError(f"Telegram request rejected (HTTP {response.status_code}); check token, recipient/Start and rate limits")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Telegram request rejected; response details hidden")
        return payload.get("result")
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram request failed; credential-bearing details hidden") from None


def discover(session=requests):
    """Find private chat IDs without printing message bodies or consuming updates."""
    bot = _api("getMe", {}, session)
    if not isinstance(bot, dict) or bot.get("is_bot") is not True:
        raise RuntimeError("Telegram bot identity response invalid")
    hook = _api("getWebhookInfo", {}, session)
    if not isinstance(hook, dict):
        raise RuntimeError("Telegram webhook status unavailable")
    if hook.get("url"):
        raise ValueError("This bot has an existing webhook; use a dedicated MVRV bot or supply your known chat ID. Webhook was NOT removed.")
    updates = _api("getUpdates", {"timeout": 0, "limit": 100}, session)
    if not isinstance(updates, list):
        raise RuntimeError("Telegram updates response invalid")
    chats = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        msg = update.get("message")
        chat = msg.get("chat") if isinstance(msg, dict) else None
        if isinstance(chat, dict) and chat.get("type") == "private" and type(chat.get("id")) is int:
            chats.add(chat["id"])
    return dict(bot_username=bot.get("username"), private_chat_ids=sorted(chats),
                instruction="Send Start / tes mvrv to this bot, then set your own numeric TELEGRAM_CHAT_ID. No messages/updates were acknowledged or printed.")


def test_notification(when, session=requests):
    """Explicit operator test. No SQLite/account/signal evaluation is performed."""
    if os.getenv("TELEGRAM_ENABLED", "false").strip().lower() != "true":
        raise ValueError("Set TELEGRAM_ENABLED=true to authorize this test message")
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    try:
        chat_id = int(chat)
    except ValueError:
        raise ValueError("Set your numeric TELEGRAM_CHAT_ID first") from None
    text = ("TEST — BTC MVRV 0/7 + four-year P5/P95\n"
            f"Timestamp UTC: {when}\n"
            "Notifikasi lokal berhasil mencapai Telegram.\n"
            "BUKAN sinyal trading. Tidak ada order atau perubahan saldo.")
    result = _api("sendMessage", {"chat_id": chat_id, "text": text}, session)
    if (not isinstance(result, dict) or type(result.get("message_id")) is not int
            or not isinstance(result.get("chat"), dict) or result["chat"].get("id") != chat_id):
        raise RuntimeError("Telegram test response did not confirm the configured recipient; do not assume delivery")
    return dict(status="telegram_api_accepted", chat_id=chat_id, message_id=result["message_id"],
                purpose="test_only_no_ledger_change", next_step="Confirm the TEST message appears on your phone")


def deliver(c, when, session=requests):
    if os.getenv("TELEGRAM_ENABLED", "false").strip().lower() != "true":
        return 0
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise ValueError("Telegram enabled but credentials missing")
    count = 0
    for row in c.execute("SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY rowid LIMIT 20").fetchall():
        if row["id"].startswith("action:"):
            action = c.execute("SELECT status FROM actions WHERE id=?", (row["id"][7:],)).fetchone()
            if action is None or action[0] != "pending":
                with c:
                    c.execute("UPDATE outbox SET delivered_at=? WHERE id=?", ("superseded:" + when, row["id"]))
                continue
        _api("sendMessage", {"chat_id": chat, "text": row["message"]}, session)
        with c:
            c.execute("UPDATE outbox SET delivered_at=? WHERE id=?", (when, row["id"]))
        count += 1
    return count
