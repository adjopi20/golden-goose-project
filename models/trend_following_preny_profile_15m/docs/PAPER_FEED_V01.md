# Local public-feed paper worker — 2026-09-29

Status: implemented for local validation; **not production/live-order approved**.
No account credentials, private API, real orders, server provisioning or Docker
daemon were added. Trading contracts and 0.25%/0.5% gross-price-risk tiers remain
unchanged. Expenses remain additional to the target price risk.

Validation: 48 focused unit/regression tests passed (paper execution, bridge,
feed, model rules, C2 and indicator context). Public REST returned the three
configured markets and a three-candle sample; a bounded WebSocket smoke test
received all three markets. No full warmup, continuous session, heavy backtest,
or real order was run by the assistant.

## Boundaries

- Reusable public adapter/aggregation/paper ledger: `live_engine/`.
- Model preparation, profile, evaluation and operating instructions: this folder.
- One Binance WebSocket subscription per market on a combined connection.
- New routed Binance `/market/stream` endpoint (migration confirmed in current
  official documentation). Legacy unrouted URL connected but delivered no prints
  in the bounded smoke test; routed URL delivered all three markets.
- Native ETHUSDC/BNBUSDC USDC-margin markets and HYPEUSDT USDT-margin market were
  verified from public exchangeInfo. No silent conversion to HYPEUSDC.
- One SQLite database per venue, one OS-locked collector process. Independent
  model equity accounts, 1,000 virtual units each from the existing config.

## What is persisted

Completed one-minute OHLC/base-volume/delta, a current partial minute, last feed
cursor, price/quantity buckets for the Pre-NY profile, frozen profiles, per-close
evaluation evidence/WAIT/not-ready decisions, and existing paper intents/fills/
positions/equity. **No permanent per-tick archive.** Old profile price buckets are
removed after a later profile freezes; frozen profile summaries remain. Minute
history is retained to keep indicator initialization stable, not truncated and
reseeded silently. Add archival/incremental-indicator parity work if long-term
history growth becomes material; this is not an unlimited-storage claim.

Feed batches commit around once per second or 1,000 events. The shared paper
backtester is still the single fill implementation. At a new quarter-hour,
completed-bar evaluation occurs before consuming its first new print. The
initial partial minute is excluded. Empty minutes are not fabricated. Gap repair
must complete before subsequent data is accepted; failed commits roll back both
market checkpoints and paper fills. A failed first candidate cannot be replaced
by a later convenient candidate in that session.

## Warmup and data limitations

`warmup` downloads 25 days of **native Binance one-minute klines**, not raw ticks,
for EMA/ADX/ATR and same-clock volume references. Base taker-buy volume gives
delta = 2 × buy − total. Warmup is labeled separately from aggregate-trade bars.
The same research indicator functions are reused at scheduled evaluation closes;
they currently scan compact minute history, not raw trade files. First-seed
differences versus a much longer historical EMA series are possible. Full live
session parity remains a deployment gate.

Exact Pre-NY volume profile cannot be reconstructed from candles. On a fresh
collector, wait for a **complete future 01:00–09:00 New York** window. A partial
profile does not trigger a trade. Run continuously before 01:00 NY. Storage uses
UTC milliseconds and model clocks use America/New_York with DST.

REST aggTrades supports only the last 48 hours. The adapter rate-limits repair
requests and checks consecutive market aggregate IDs. A gap outside that window
or one that cannot be repaired stops the worker rather than inventing trades.
Data recovery can reconstruct existing paper positions, but old candidate signals
are logged, not retrospectively filled. Candidate processing >300s after close
or arriving with a next print >5s old is an operational missed candidate. These
are separate from alpha rejection and must be reported as such.

HYPE's last recorded C1 artifact expired on 2026-09-27. Without a valid causal
replacement, HYPE data is collected but its first candidate is `NOT_READY`.
Do not extend old artifact dates or import another venue's thresholds silently.
This operational block is not evidence that the HYPE entry rule failed.

Lighter public ETH/BNB/HYPE markets were found (IDs 0/25/24). Maker-ask trade
normalization is implemented and tested. Its public recentTrades limit is 100,
and recovery/historical taker-side data is not yet certified. Therefore this
worker explicitly rejects a Lighter config; **Lighter paper execution is pending**.

## Commands (run from repository root)

Keep the active SQLite database outside OneDrive and outside Git:

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH="."
$db = Join-Path $env:LOCALAPPDATA "GoldenGoose\paper\binance.sqlite"

# Once, before first collection. Public candles; no keys/order access.
.\.venv\Scripts\python.exe -m models.trend_following_preny_profile_15m.paper_worker `
  --database $db --action warmup
if ($LASTEXITCODE -ne 0) { throw "Warmup failed; do not start worker." }

# First start: collect data + record decisions, no simulated positions yet.
.\.venv\Scripts\python.exe -m models.trend_following_preny_profile_15m.paper_worker `
  --database $db --action run
```

Use Ctrl+C for a committed stop. Resume with `--action run`, **not another
warmup**. Keep the terminal/laptop awake and internet connected. The script is
not yet a managed daemon. In another terminal, set the same `$db`, then:

```powershell
.\.venv\Scripts\python.exe -m models.trend_following_preny_profile_15m.paper_worker `
  --database $db --action status
```

After inspecting complete profile/feature evidence, opt in to simulated trades
by stopping and restarting with `--action run --paper`. This never sends real
orders. Earlier collection-only candidates remain locked, so switch before the
next session. HYPE additionally needs `--c1-calibration <validated-json>`.
Do not create placeholder thresholds to make the readiness warning disappear.

Backups use the existing `live_engine.runtime --action backup --backup-to <new
file>` command with the same config/database, not an OS copy of an active DB.

## Remaining deployment gates

1. Full-session profile/feature/signal parity and sustained memory/throughput.
2. In-date HYPE causal calibration, and Lighter native-history/recovery support.
3. Docker/Compose, persistent volumes, logging rotation, alerts, watchdog and
   restart practice on the server. Paper worker precedes these—not a claim that
   server deployment is finished.
4. Real order adapter, tick/lot rounding, margin, native stops, reconciliation,
   funding and exchange account allocation. All remain outside this paper step.

Next-print paper fills do not model actual matching or liquidation. No funding
is added. Fee remains the frozen 4bps proxy, not a claim about actual venue fees.

## Public API sources checked

- [Binance routed WebSocket connections](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)
- [Binance USD-M market-data REST](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Lighter WebSocket trade schema](https://apidocs.lighter.xyz/docs/websocket-reference)
- [Lighter recentTrades](https://apidocs.lighter.xyz/reference/recenttrades)
