# Local Telegram setup and acceptance

Commands added 2026-10-03: `telegram-discover` and `telegram-test`. They return before opening any SQLite trading database: no capital initialization, signal evaluation, pending trade creation or fill. Only the test command sends a clearly labelled TEST message. Provider acknowledgement is not the same as checking the phone notification.

## Create a dedicated bot

In the official `@BotFather`, use `/newbot`, or `/mybots` then your existing bot's API Token menu. The Telegram Login Widget/domain menu is unrelated; cancel it instead of submitting a website. Treat the bot token like a password. Do not share it in chat, terminal command arguments, screenshots or Git. Revoke through BotFather if leaked.

Copy `.env.example` to `.env` ONLY if `.env` does not already exist. In this directory's `.env` put unquoted values:

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=YOUR_TOKEN_HERE
TELEGRAM_CHAT_ID=
```

Leave other existing settings unchanged. Open your own bot's chat, tap Start and send `tes mvrv`. This grants the private chat permission and gives the bot an update from which to discover your recipient ID. Do not send that message to BotFather instead.

## Discover the recipient without printing messages

From this `live_mvrv` directory in PowerShell:

```powershell
docker build -t golden-goose/btc-mvrv-dual:v1 .
docker run --rm --read-only --tmpfs /tmp:size=32m --cap-drop ALL --security-opt no-new-privileges --env-file .env --entrypoint python golden-goose/btc-mvrv-dual:v1 -m live_mvrv.main telegram-discover
```

Output contains the bot username and private chat IDs, not tokens, user names or message bodies. Select your own chat ID; do not guess among multiple recipients. If the list is empty, verify the returned bot username matches the chat you opened, send a NEW text to that bot and rerun discovery. Telegram keeps updates for a limited time, and another consumer may have acknowledged them already. Discovery does not send an offset acknowledgement, remove a webhook or change holdings. If an existing webhook is reported, use a dedicated bot or supply a verified known chat ID; do not dismantle another app's webhook.

Save the numeric recipient into `.env` as `TELEGRAM_CHAT_ID`. Do not use the bot's own ID, its username, a phone number or an unverified third-party recipient.

## Send one explicit test

```powershell
docker run --rm --read-only --tmpfs /tmp:size=32m --cap-drop ALL --security-opt no-new-privileges --env-file .env --entrypoint python golden-goose/btc-mvrv-dual:v1 -m live_mvrv.main telegram-test
```

This short-lived container has no trading volume mounted and no worker started. It requires `TELEGRAM_ENABLED=true`. The response confirms Telegram accepted the message for the configured chat ID. Verify the TEST arrives in your phone chat and that the OS/Telegram notification settings permit a push notification. No received message means phone delivery is not accepted yet. Repeating the test deliberately sends another TEST, not a trade or an outbox replay.

HTTP 401: check token. HTTP 403: check that this recipient started/unblocked this bot. HTTP 429: allow the rate limit to cool down before retry. Error output hides token-bearing URLs and raw response bodies. Do not paste your `.env`, a full bot API URL or plain `docker compose config` output into support chat.

## Enable daily operational alerts later

The resident worker uses the same tested `sendMessage` request path. BUY/SELL proposals, paper/manual fill notices, missed planned closes, data errors and recovery use its durable outbox. WAIT does not produce a trade alert; no daily-summary push is implemented. Network failure retains outbox entries for retries. Ambiguous network retries can duplicate a message, never a ledger fill. These are one-way alerts, not Telegram buy/sell buttons or remote trading commands.

For manual trading mode choose `MODE=alerts` BEFORE first funding. Receiving a proposal does not change balances; actual fills must be explicitly recorded with the existing CLI. Testing Telegram does not change paper/alerts mode. Deploy using the scoped commands in `SERVER_RUNBOOK.md` after local delivery passes.

Sources: [Telegram bot tutorial](https://core.telegram.org/bots/tutorial), [getUpdates](https://core.telegram.org/bots/api#getupdates), [sendMessage](https://core.telegram.org/bots/api#sendmessage).
