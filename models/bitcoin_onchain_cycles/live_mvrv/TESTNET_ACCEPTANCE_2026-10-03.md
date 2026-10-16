# Authenticated Binance Spot Testnet acceptance — 2026-10-03

Status: local virtual execution acceptance passed. NOT approval for real-money production and NOT strategy/OOS performance.

## Authorized scope

The operator approved at most 50 virtual USDT from unallocated surplus, pausing the local worker and auditing surplus changes before resume. No server, real-money endpoint, strategy threshold or branch capital was changed. The separate probe database uses explicit `PROBE_NOT_MVRV` actions.

- Strategy project: `btc-mvrv-binance-testnet`; branch capital remains 500:500 USDT.
- Probe project: `btc-mvrv-probe-20261003-acceptance01`.
- Probe ID: `f93abe76d758447390034859ee9d4cd8`.
- Probe capital: 50 USDT, split 25:25. Buy budget cap: 49.50 USDT total; actual spent: 49.17747790 USDT.
- Image config digest: `sha256:a0e65df1588832257e9415c6c236980b657f8f81def1c9f44393b53afa3b72ec`.

## Actual venue fills

All four fills belong only to the probe. Reused branch labels test attribution; they do not make these MVRV signals.

| Branch | Side | Order ID | UTC fill time | Gross BTC quantity | Gross quote USDT | Commission |
| --- | --- | --- | --- | ---: | ---: | --- |
| fixed_0_7 | BUY | 9046632 | 2026-10-03 15:33:45.536 | 0.00029000 | 24.58779500 | 0.00000029 BTC |
| percentile_4y_p5_p95 | BUY | 9046815 | 2026-10-03 15:34:31.490 | 0.00029000 | 24.58968290 | 0.00000029 BTC |
| fixed_0_7 | SELL | 9047076 | 2026-10-03 15:35:51.834 | 0.00028000 | 23.74232000 | 0.02374232 USDT |
| percentile_4y_p5_p95 | SELL | 9047077 | 2026-10-03 15:35:55.890 | 0.00028000 | 23.74232000 | 0.02374232 USDT |

All orders reached terminal FILLED status. Total commissions: 0.00000058 BTC and 0.04748464 USDT.

## Recovery and accounting checks

- First BUY response deliberately discarded after acceptance. A new process queried stable client ID `mv1_pr76b5e9097d97d51e59484d`, imported the actual fill/commission, and did not submit a replacement order.
- Second BUY initially returned a missing-order reconciliation diagnostic. A later GET-only verification found the fill. No replacement POST was sent. Intent must be preserved when venue history is temporarily unavailable.
- Repeating BUY after both fills left exactly two buy orders/two fills and unchanged balances. Fresh-process recovery passed; this is not a real network timeout or SIGKILL-mid-write test.
- SELL disposed only acquired probe BTC, rounded to the current venue grid. Each branch retained 0.00000971 BTC dust; it was not silently dropped.
- Venue BTC/USDT deltas exactly matched branch accounting including native commissions. Main/probe SQLite integrity checks returned `ok`.
- Online probe backup opened in a new container and verified against venue balances/orders. A main-ledger pre-probe online backup was retained.
- Frozen hashes of main accounts/actions/decisions/fills/marks/raw/vintages/venue ledger remained unchanged through restoration.

## Surplus restoration

Restored at `2026-10-03T15:36:57.611427+00:00` under immutable metadata key `probe_adjustment:f93abe76d758447390034859ee9d4cd8`.

| Asset | Before unallocated surplus | Probe delta | After unallocated surplus |
| --- | ---: | ---: | ---: |
| USDT | 9000.00000000 | -1.74032254 | 8998.25967746 |
| BTC | 1.00000000 | +0.00001942 | 1.00001942 |

USDT decrease is NOT a 1.7403-USDT trading loss: residual BTC dust remains, now included in surplus. Combined realized PnL on disposed BTC was approximately -0.09208 USDT using average cost. Residual dust retains approximately 1.64825 USDT cost basis and fluctuating market value. Probe activity is excluded from strategy analytics.

After resuming the updated worker:

- Container `running healthy`; direct database health command exited successfully.
- Both strategy branches still 500 USDT cash / 0 BTC; strategy fills/orders/pending zero; error empty.
- Active probe guard cleared; unsent strategy notifications zero. Telegram API delivery accepted; phone receipt of this final notification not separately confirmed.
- Accepted data date 2026-10-02; both real strategy decisions remain WAIT.
- Live plus legacy offline regression suites: 190 passed, one existing undefined-XIRR warning.

## Retained artifacts

Do not delete volumes to reset a test:

- Main volume: `btc-mvrv-binance-testnet_binance_testnet_data`.
- Main pre-probe backup: `before_probe_6185fe5b483747089652fc773ff97961.sqlite` within that volume.
- Probe volume: `btc-mvrv-probe-20261003-acceptance01_probe_data`, containing `probe.sqlite` and `after_sell_backup.sqlite`.

Code/runbook: `binance_probe.py`, `compose.binance-probe.yaml`, `BINANCE_PROBE.md`.

## Issues and limits

An initial volume permission error occurred before initialization/orders and was resolved with UID-1000 writable `/data`. Initial authenticated preflight then failed with Binance -1021 timestamp rejection; the empty ledger was checked before retry. Public-time/authenticated-read diagnostics subsequently passed (roughly -0.3s offset); the rejection was not reproduced and its cause is not established. No timestamp-window widening or automatic POST retry was added.

This does not demonstrate long-running uptime, actual partial fills/cancels, Testnet reset in the wild, real network timeout behavior, production liquidity, strategy edge or real-money readiness. Offline fixtures do not replace live venue evidence. Production mutation remains blocked. Next step: separate VPS Testnet worker with secret mounts, persistent ledger, off-server backup and monitored daily operation. Existing intraday server containers were untouched.
