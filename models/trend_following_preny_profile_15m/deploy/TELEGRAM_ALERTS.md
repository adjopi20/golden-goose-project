# Separate Telegram bot for ETH pilot operational alerts

Shared implementation: `live_engine/ops_alerts.py`. Model-specific targets and
deployment live here. No trading code, entry, sizing, execution policy, key or
running executor is changed. Independent Compose project `golden-goose-eth-alerts`
reads the executor/strategy volumes RO, writes only its own alert SQLite volume,
and has NO Binance API/PEM, Docker socket, trading-write mount or remote-command
receiver. MVRV bot/service remains separate and unchanged.

## What is detected

| Condition | Alert policy |
| --- | --- |
| Worker heartbeat older than60s | Next observer poll |
| Source heartbeat older than180s | Next observer poll |
| Metadata/DB identity changed | Next observer poll |
| New entries blocked for review | Next observer poll |
| Database unreadable | Persists30s |
| Error worker or unexpected disarmed state | Persists60s |
| SENDING/UNKNOWN/cancel/error unresolved | Persists30s |
| Entry-fill ledger without confirmed STOP | Persists10s |
| Exit deadline passed, lifecycle not CLOSED | After15s past deadline |

Poll interval15s, plus read/HTTP time: thresholds are not instant deadlines.
Fast successful mitigation is quiet. Initial actionable incident alerts are
deduplicated; persistent incidents remind after30 minutes. Valid recovery produces
one recovery message. Failed DB reads never fabricate recovery. Persisted outbox
survives restart, retries with backoff/global Telegram `retry_after`, and suppresses
obsolete queued incidents. A crash after Telegram accepts but before receipt is
persisted can duplicate a notification; it cannot duplicate an order because this
service has no order capability. One queued message is attempted per poll.

Messages contain fixed operational explanations/UTC/event ID, not raw error
strings, API keys, signed URLs, balances, bot token or private keys. Detailed
diagnosis remains in the local execution journal/logs. This watches ledger states,
NOT exchange positions independently; a stale ledger is explicitly alerted.

**Limitations:** it does not identify every possible bug or silent bad model
decision. It does not repair/restart/cancel/trade or automatically pause entry.
Whole-VPS/power/network loss can prevent Telegram delivery. Monitoring that fault
requires an independent external heartbeat monitor; local Docker health alone
cannot notify when its host is dead. Notification failures retain outbox/log state
and make watcher health fail, but cannot notify through the same unavailable path.

## 1. Separate bot and server-only secrets

In Telegram open official [BotFather](https://t.me/BotFather), `/newbot`, create a
NEW operational bot, and save its token privately. Open that bot and press Start
or send `/start`. Do not send tokens/secret files to ChatGPT or GitHub.

On new VPS as root:

```bash
nano /opt/golden-goose-secrets/telegram-alerts.env
```

Enter this in the editor (replace token locally; chat0 is only a discovery
placeholder, NEVER an alert destination):

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=YOUR_NEW_BOT_TOKEN
TELEGRAM_CHAT_ID=0
```

```bash
chmod 600 /opt/golden-goose-secrets/telegram-alerts.env
```

No Binance key/private-key path belongs in this file. No need to change ownership
of any existing key. The root-owned env file is read by Compose, not mounted into
the image. Never print expanded Compose output, env files, or Telegram URLs.

## 2. Publish seven code/config files from laptop

PowerShell; verify only these are staged (no `git add .`):

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$files = @(
 'live_engine/ops_alerts.py',
 'live_engine/Dockerfile.alerts',
 'live_engine/Dockerfile.alerts.dockerignore',
 'tests/test_ops_alerts.py',
 'models/trend_following_preny_profile_15m/deploy/pilot.alerts.json',
 'models/trend_following_preny_profile_15m/deploy/pilot.alerts.yaml',
 'models/trend_following_preny_profile_15m/deploy/TELEGRAM_ALERTS.md'
)
git add -- $files
git diff --cached --stat
git commit -m "Add isolated read-only Telegram operational watcher"
git push origin deploy/orb-live-agent
```

## 3. Build ONLY watcher, discover your chat

New VPS/root. Reinitialize variables in each new shell or after `sudo su`.
No executor Compose file is included; never use broad `up`/`down` here.

```bash
cd /root/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_ALERTS_COMMIT=$(git rev-parse HEAD)
alerts=models/trend_following_preny_profile_15m/deploy/pilot.alerts.yaml
tgenv=/opt/golden-goose-secrets/telegram-alerts.env
unset TELEGRAM_ENABLED TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID

docker compose --env-file "$tgenv" -f "$alerts" config --quiet &&
docker compose --env-file "$tgenv" -f "$alerts" build ops-alerts &&
docker compose --env-file "$tgenv" -f "$alerts" run --rm --no-deps -T ops-alerts \
  --config /app/config/alerts.json --action discover
```

This returns candidate private chat IDs without showing message bodies or
acknowledging/deleting updates. An existing webhook is not removed. Select YOUR
private chat ID; if empty, send `/start` to the NEW bot then retry discovery.
Save the correct numeric ID into `TELEGRAM_CHAT_ID` with the same `nano` command.
You may reuse your own private chat ID from MVRV, but NOT its bot token.
The `unset` above prevents old shell exports overriding the dedicated env file;
it does not change any running MVRV container.

## 4. Explicit TEST, confirm receipt on phone, then start

```bash
docker compose --env-file "$tgenv" -f "$alerts" run --rm --no-deps -T ops-alerts \
  --config /app/config/alerts.json --action test
```

Expect `telegram_api_accepted:true`. Verify the labelled TEST appears in your
correct private bot chat, and phone notifications are enabled. API acceptance
alone is not confirmation of phone push. This command opens no monitoring/trading
DB and sends no trade/order. Do not rely on alerts until receipt is confirmed.

Then run ONLY the new watcher:

```bash
docker compose --env-file "$tgenv" -f "$alerts" up -d --no-deps --no-build ops-alerts
sleep 20
docker compose --env-file "$tgenv" -f "$alerts" ps
docker compose --env-file "$tgenv" -f "$alerts" logs --tail=30 ops-alerts
docker compose --env-file "$tgenv" -f "$alerts" exec -T ops-alerts \
  python -m live_engine.ops_alerts --config /app/config/alerts.json \
  --database /data/alerts.sqlite --action status
```

Expected healthy, fresh heartbeat, empty delivery_error/watcher_error, pending0
when healthy, active[]. Silence while healthy is intentional. If real conditions
are active, inspect them; do not force clear the outbox or alter a real position
to test a message. Never stop a live executor to manufacture an alert.
The dedicated volume `golden-goose-eth-alerts_alert_data` preserves conditions,
notifications, delivery receipts and retry timestamps, separate from trading.

## Validation and remaining acceptance

147 focused offline tests passed (28 watcher tests +119 existing pilot tests).
Coverage includes delivery/recipient failures, redaction,429/global retry,
outbox restart/duplicate control, repeated incident generations, source identity,
RO reads, unknown order/STOP/deadline alerts, false-recovery prevention, and no
trading DB use for TEST. Compose validated using dummy credentials, no container
started locally. Read-only checks against the active server's executor/source
snapshots returned valid/no conditions. No Telegram token was obtained or message
sent by the assistant; no watcher deployed yet. Image build and actual TEST/phone
receipt remain operator acceptance steps.

Official transport reference: [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage),
[rate-limit response parameters](https://core.telegram.org/bots/api#responseparameters).

## 2026-10-19 startup/healthcheck correction

Operator deployed a5a7579 and Telegram accepted TEST message5. Phone receipt still
requires operator confirmation. Initial status ran before the state DB existed;
later read-only status showed owner ops_alerts, no internal error, pending0 and
active[]. Zero restarts and ~30 MiB memory ruled out a crashing/OOM watcher in
that snapshot. However Docker healthchecks repeatedly exceeded5s under0.1 CPU.

Read-only measurement on that same container found a cold full-module import
took12.807s while internal health was true. Matched in-memory measurements took
11.508s before versus2.292s after delaying requests/HTTP imports until actual
Telegram actions. No running source/container was changed for those measurements.
Health/status now use only stdlib; regression tests block HTTP imports in fresh
processes. Status before DB creation explicitly reports starting, not a vague
OperationalError. Resource quotas, alert thresholds, Telegram transport behavior
and trading are unchanged. 150 focused tests passed after the correction.

Publish only these three changed files from laptop:

```powershell
git add -- live_engine/ops_alerts.py tests/test_ops_alerts.py models/trend_following_preny_profile_15m/deploy/TELEGRAM_ALERTS.md
git diff --cached --stat
git commit -m "Keep operational healthchecks lightweight under CPU quota"
git push origin deploy/orb-live-agent
```

After pull on new VPS, recreate ONLY watcher; do not touch executor/paper services:

```bash
cd /root/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_ALERTS_COMMIT=$(git rev-parse HEAD)
alerts=models/trend_following_preny_profile_15m/deploy/pilot.alerts.yaml
tgenv=/opt/golden-goose-secrets/telegram-alerts.env
unset TELEGRAM_ENABLED TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
docker compose --env-file "$tgenv" -f "$alerts" build ops-alerts &&
docker compose --env-file "$tgenv" -f "$alerts" up -d --no-deps --no-build ops-alerts
```

Wait30–60 seconds, then use the ps/status commands above. Require Docker healthy,
fresh watcher heartbeat, no watcher_error/delivery_error, and inspect any active
conditions. Persisted outbox/state remains in the existing alert volume. The
corrected image has not been deployed by the assistant; post-rebuild Docker health
is an operator acceptance check, not inferred from the in-memory benchmark.

## 2026-10-19 follow-up: complete cold CLI budget

After deploying 4f35300, internal status remained healthy with no pending or active
alerts and zero restarts, but Docker healthchecks still intermittently timed out.
Three complete cold CLI checks on the running 0.1-CPU container succeeded in
5.786s, 5.998s and 9.697s (including docker exec overhead). The earlier in-memory
module benchmark did not include the whole Python startup and CLI path.

Allow 15s for the checker to finish instead of 5s. CPU remains 0.1, and internal
health still requires a heartbeat less than 90s old with no watcher/delivery error.
This is a Compose-only runtime adjustment, not a trading or Telegram logic change.
The deployment regression test locks both timeout and CPU quota.

Publish only pilot.alerts.yaml, tests/test_ops_alerts.py and this document. After
pulling the change, reuse the already-built 4f35300 image; no rebuild is needed:

```bash
cd /root/golden-goose-project
git pull --ff-only origin deploy/orb-live-agent
export GG_ALERTS_COMMIT=4f35300e6fcfb8d7b1a9f5f3c5129122600bcff8
alerts=models/trend_following_preny_profile_15m/deploy/pilot.alerts.yaml
tgenv=/opt/golden-goose-secrets/telegram-alerts.env
unset TELEGRAM_ENABLED TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
docker compose --env-file "$tgenv" -f "$alerts" config --quiet &&
docker compose --env-file "$tgenv" -f "$alerts" up -d --no-deps --no-build ops-alerts
```

Check ps/status after 60-90 seconds. Only the watcher is recreated; its persisted
outbox and all trading services remain unchanged. Do not call the healthcheck
stable until post-change checks succeed. Confirm the TEST on your phone separately.
