# Release validation — 2026-10-03

## Production preparation — 2026-10-16

- Prepared production Compose/env/first-rollout script, separate volume and authenticated account-bound ledger. Execution defaults OFF and the deployment script refuses an armed first rollout. No production credential was read/used, no order was submitted, no server was accessed, and no existing container/ledger/env was changed.
- Shared live/legacy offline suites: **213 passed**, one existing undefined-XIRR warning. Added coverage for explicit production capital with no funding default, production opt-in/acknowledgement, per-order cap before durable intent, native-fee setting guard, environment isolation, OFF expiry without backfilled orders, idempotent fake production fills and branch isolation, old-error recovery, notification separation, persistent inventory/UNKNOWN halts, safe diagnostics and timestamp after slow key loading.
- Compose production configuration parsed with dummy credentials using `config --quiet`. Bash syntax check passed; the deployment script was NOT executed.
- Docker build/container acceptance for this release is **NOT verified**: local Docker Desktop Linux daemon was unavailable. Prior image/Testnet acceptance does not certify this modified production image. Build and read-only startup/phone/restart/backup acceptance remain server gates.
- Frozen config unchanged: 0/7 and 1460-day P5/P95, initial 50:50, BTCUSDT Spot, no CVDD/leverage/contributions/rollover. See `PRODUCTION_DEPLOY.md` for credentials, capital, accounting limits and later activation review. No real-money activation was authorized or performed.

Contract: `btc-mvrv-dual-v1`, initial 50:50 fixed 0/7 and four-year P5/P95.

## Verified

- New engine and Telegram setup: 31 passing tests covering accounting, causality, full indicator warmup, source gaps/revisions, pending fills, duplicate prevention, rollback, recovery, notifications, combined drawdown, private-chat discovery, secret-safe errors and database-free test commands.
- Installed new + preserved legacy regression suites: 127 tests passed. One existing XIRR warning deliberately reports an undefined return for a cash-flow pattern with no bracketed root; it is not an engine failure. Tests were run with bytecode/cache output disabled and temporary output in the writable workspace.
- Six branch equity/fill paths match the existing independent research engine across fresh starts 2014-07-18, 2021-01-01 and 2024-04-20, ending 2026-10-02. This is implementation parity, not a new untouched OOS test.
- Current Coin Metrics API fetch accepted full origin history through 2026-10-02. At evaluation 2026-10-03, Z = 1.0068994192, four-year P5 = -0.1332234749, P95 = 2.7770888948. Both newly funded cash accounts returned WAIT. Telegram was disabled; no real order was placed.
- Compose configuration parses successfully. Image context is self-contained and excludes secret/runtime/research files.
- Docker acceptance now PASSED on Docker Desktop 4.93.0 / Engine 29.8.1 (Linux amd64). The earlier Desktop startup failure was no longer present; no endpoint deletion or factory reset was performed.
- Image build succeeded. Base digest: `python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81`; original acceptance image ID: `sha256:021bb050b91547630b3ee3eab489a3178825aa54d66d7639cacba692bbcb13f4`. The build sent about 56 KB of context; image size reported 381,630,516 bytes. A subsequent Telegram CLI/shared delivery refactor was rebuilt as `golden-goose/btc-mvrv-dual:v1`, image ID `sha256:3b768393596620a8b34c2f6a422b1eff0d11d706f2fba2ccf2064230bc962061`; that image passed the real discovery/test commands below. The resident-worker/restart measurements describe the original acceptance image, not a second full acceptance run. Future builds may resolve a newer base/transitive dependency version and should be retested.
- Actual resident worker fetched through 2026-10-02 inside Docker, stayed healthy, and returned WAIT for both fresh $500 cash accounts. Measured idle memory about 80 MiB, CPU 0.01%; this is one local observation, not a VPS capacity guarantee.
- Force-recreating the worker preserved accounts, accepted raw history/vintages, decisions, actions, fills, marks and all metadata except its expected heartbeat update. Matching ledger SHA-256: `983fed590af5c35ee3ccc2aa8ee97607dc6fd9119754eb34ffbe7e6587fbdeac`. No duplicate decisions/fills were introduced.
- Separate network-disabled synthetic probe passed seed/resume/verify across three disposable containers sharing a new test volume: two pending actions survived, both filled once, repeated evaluation did not duplicate fills, and post-fill state survived another container. Both live-reference and synthetic SQLite online backups matched ledger contents and passed integrity checks.
- Non-root UID 1000, read-only root filesystem, WAL database and secret/test/data exclusions were checked inside the image. Telegram remained disabled with empty credentials. Local acceptance services were separate from the intraday server.
- Telegram local setup: the dedicated bot identity and one private recipient were verified. Telegram accepted one explicitly labelled TEST message (message ID 4) and returned the expected recipient; the operator confirmed that the message appeared in the bot chat. The disposable CLI container used no database volume and changed no ledger or account balance. The recipient/enabled settings were temporary Docker overrides; the operator must persist them in the local `.env`. Chat receipt is confirmed, but OS push/banner behavior was not separately tested. See `TELEGRAM_SETUP.md`.

## Binance adapter addition — 2026-10-03

- New isolated read-only/Testnet adapter plus all live/legacy regression suites: **153 passed**, including 26 new exchange tests; the same existing undefined-XIRR warning remains. Tests use simulated HTTP/venue fixtures, no Binance keys or network orders.
- Covered production POST block, Testnet opt-in, signature/secret-safe errors, exact UTC Binance close, order sizing/filter/dust handling, refused old DB conversion, 50:50 capital binding/unallocated cash, native BTC/USDT fees, average cost/realized PnL, branch-isolated compounding, partial/canceled fills, atomic rejects, stale response protection, unknown-order restart without repost, execution time window, and DB-free status.
- Separate Testnet Compose parses with blank example credentials. The isolated `golden-goose/btc-mvrv-dual:binance-testnet-v1` image builds successfully with a roughly 73 KB context and the same base digest as above. A network-disabled disposable container verified module imports, UID 1000, exclusion of .env/secrets/tests, and production POST rejection. Authenticated Testnet status/actual fills/restart acceptance have NOT yet been performed for this addition; old resident-worker acceptance does not establish Testnet execution acceptance.
- No production POST implementation, no change to current `.env`/alerts ledger, no service restart, no deposit, no server deployment. Existing paper/alerts calculations retain their defaults; optional Testnet-only execution context supplies a Binance USDT mark and exchange dust threshold.
- Known conservative limits: BTCUSDT prototype only; native BTC/USDT commissions supported, nonzero third-asset fees halt; external inventory changes and unknown POST outcomes require operator investigation. See `BINANCE_ADAPTER.md` for acceptance steps and remaining production gates.

## Ed25519 addition — 2026-10-03

- Staged live/legacy regression suites: **164 passed**, including 11 new synthetic Ed25519 tests; the existing undefined-XIRR warning remains. Local cryptography version was 48.0.1. Docker built successfully with cryptography 50.0.2; a network-disabled disposable container verified encrypted synthetic signing, UID 1000, secret/test exclusions and production POST blocking. Image config digest: `sha256:fcf8f7a68c629f7f92925832c32bfb7a455f991794fde695247ce635f38e55cc`. Both Ed25519 and optional passphrase Compose overrides parse using dummy paths and blank example credentials; actual private-key bind/access remains to be tested by the operator.
- Covered exact encoded-payload signature verification, unchanged HMAC behavior, encrypted PEM/passphrase-file loading, interactive read-only prompting, unattended fail-closed behavior, wrong/missing/non-Ed25519 keys, secret-safe failures and production POST rejection before key loading.
- User-provided private PEM contents and API credentials were not read or copied. Authenticated Testnet acceptance remains outstanding. Separate opt-in read-only key/passphrase mounts are documented in `BINANCE_ED25519.md`; no existing worker is restarted by this change.

## Testnet contract hardening — 2026-10-03

- Operator pasted successful authenticated Ed25519 `status` and `once`: Testnet virtual balances, two 500-USDT accounts, fresh 2026-10-02 data, both WAIT, no errors/pending/orders/fills. This is operator-provided acceptance evidence; no secret was inspected by Codex.
- Live/legacy suites: **176 passed**, with the same existing undefined-XIRR warning. Twelve added tests cover endpoint exclusions, HTTP 429/418 Retry-After including non-JSON/fallback responses, zero HTTP requests during cooldown, persisted/reloaded worker cooldown, current filters/rate-limit definitions, and daily completed-history checks for reset/reused IDs without duplicate fills or reposting.
- Strategy rules and existing ledger/credentials remain unchanged. Production POST remains unavailable. Authenticated order/fill/restart acceptance is still outstanding. Missing history is a safe pause, not definitive proof of reset; identical pre-trade balances cannot reveal a reset. See `BINANCE_ADAPTER.md` for shared-IP and retention limitations.

## Authenticated Testnet probe acceptance — 2026-10-03

This supersedes the earlier outstanding authenticated order/fill/restart checks, not the remaining real-money production gates.

- Live/legacy regression suites: **190 passed**, including 14 additional probe isolation/recovery/fee/surplus tests; the existing undefined-XIRR warning remains.
- Operator-authorized surplus-only probe completed exactly two actual Testnet buys and two sells. First response-loss recovery, repeated-command idempotency, fresh-container reconciliation and online-backup verification passed. BTC/USDT venue deltas matched native-fee accounting exactly.
- Strategy worker paused during the probe and resumed afterward. Both strategy accounts remain 500 USDT/0 BTC, no strategy fills/orders/pending/error; container/ledger health passed and notification outbox drained.
- Only surplus changed: -1.74032254 USDT and +0.00001942 BTC dust, with immutable audit. This is NOT a strategy return or cash-only loss calculation.
- Full evidence, fill IDs, backup locations and unverified scenarios: `TESTNET_ACCEPTANCE_2026-10-03.md`. Production POST remains blocked; VPS deployment and real-money authorization have not occurred.

## Portfolio reference

Initial $10,000, split once 50:50. Historical next-close proxy, 10bps fees per side, no contributions, leverage or rebalancing. Cash yields zero. These are retrospective daily reference marks, not executable return guarantees.

| Fresh start | Ending combined wealth | Combined max DD | CAGR | Calmar |
| --- | ---: | ---: | ---: | ---: |
| 2014-07-18 | $14,500,448.43 | 61.45% | 81.53% | 1.33 |
| 2021-01-01 | $39,727.17 | 53.11% | 27.12% | 0.51 |
| 2024-04-20 | $10,000.00 | 0.00% | 0.00% | undefined |

The last row never bought: zero drawdown is waiting in cash, not successful downside protection. Combined DD is calculated from summed daily equity, not averaged branch DD. P5 selection remains post-hoc and must stay frozen during forward observation.

## Remaining deployment checks

- Telegram TEST receipt: confirmed by the operator in the bot chat. Persist local enabled/recipient settings before starting the worker; OS push/banner behavior is controlled by phone notification settings. Notifications remain disabled by default in the template.
- Actual VPS resource/container inventory, backup policy and server deployment: not performed. Existing two server containers untouched.
- Real exchange execution: deliberately not implemented or authorized.

Before enabling the service on a server, build it, run a fresh paper account, restart the container and reconcile the same balances/pending IDs, test phone delivery and create an off-server backup. Do not erase an existing volume to get a clean test. Research/legacy files were preserved, not rewritten into strategy rules.

Local artifacts retained: acceptance project `btc-mvrv-acceptance-20261003`, volume `btc-mvrv-acceptance-20261003_mvrv_data` (API worker and its online backup), synthetic volume `btc-mvrv-state-test-20261003`. The acceptance worker is stopped after testing; temporary probe containers removed themselves. Do NOT restore the synthetic fixture into production or treat its 2020 transactions as real signals. No volume was removed. Repeat a probe only with a new empty acceptance volume; `tests/docker_restart_probe.py` refuses to overwrite an already seeded account.
