# BTC MVRV Dual Engine — operational contract v1

Production transport addendum (2026-10-16): `PRODUCTION_DEPLOY.md` documents the separately gated Binance Spot worker and real-account ledger. The original paper/alerts modes below remain available. Frozen indicator/threshold/50:50 strategy rules are unchanged; production defaults OFF and was not activated during preparation.

Frozen 2026-10-03. Selected by operator: **0/7 + four-year P5/P95**.
Status: ready for local paper/alert acceptance testing, NOT proven investment edge or real-order automation.

## Model ownership and capital

One shared engine, two isolated USD/BTC ledger accounts. Initial total assigned capital C is split once into C/2 cash each, zero BTC. The initial budget is the capital assigned to this system, not all household savings. Later wealth weights drift: no periodic rebalance, cross-account transfer, contributions, leverage, shorts or borrowing. Each account reinvests its own net proceeds. Changes to mode, capital or frozen parameters require a new explicitly funded database/contract; a restart never resets an existing account.

The two specific strategy contracts are `FIXED_0_7.md` and `P5_P95.md`. No CVDD, CDD, NUPL, rollover or LLM. Price stop, trailing stop, time stop and halving-forced exit are deliberately absent to preserve the tested rules. Data failure pauses new decisions, not liquidates an existing position. A manual operational pause or cancelled proposal changes the forward realized path and is logged rather than called an exact backtest replication.

## Shared data definition

Coin Metrics daily `PriceUSD`, `CapMrktCurUSD`, `CapMVRVCur`. UTC day labels must be unique, complete and contiguous from **2010-07-18**. Preserve this origin: merely downloading the last four years changes Z's denominator.

`realized_cap = CapMrktCurUSD / CapMVRVCur`.

`Z = (market_cap - realized_cap) / expanding_population_std(market_cap)`; pandas `ddof=0`, minimum two observations. The first undefined Z is excluded; first valid Z date is 2010-07-19. Percentiles operate on this Z series, not on a rolling recalculation of its standard-deviation denominator.

Raw responses are archived before acceptance. Previously accepted observations remain immutable. Overlapping revisions above numerical serialization tolerance are rejected and require review; no automatic overwrite of history or old decisions. Full warmup, finite positive inputs and contiguous dates are mandatory. No forward-filling gaps or defaulting unavailable metrics to zero. Provider coverage/access can change; outage must fail closed, not switch indicator formulas.

## Decision state machine

1. Accept only the complete prior UTC day's observation for live proposals. Never evaluate unfinished/future days or silently use stale cached data.
2. Evaluate each account/date once. Use account state, not holdings from a hypothetical historical simulation.
3. A pending proposal blocks another proposal for that account.
4. If no material BTC is held and available cash exceeds $0.0000001, an entry condition proposes spending full account cash, including fee budget.
5. If BTC is held and the exit condition passes, propose selling all BTC attributed to this account.
6. Otherwise retain current position. No fresh threshold crossing is necessary. Repeated qualifying days are not repeated all-in purchases.
7. Decimal money/units, nonnegative balance constraints, atomic SQLite changes and unique fill/action IDs protect accounting. No account can spend another's cash/BTC.

Reference fees are **10bps per side**. Buy units = cash / (1 + fee) / fill price, rounded down to 18 decimal places in paper mode; sell proceeds = BTC * fill price * (1 - fee). Do not claim that these fees include extra slippage/spread, taxes or stablecoin conversion; all are excluded in the reference path. Cash yield is zero.

## Timing and modes

UTC row D is conventionally marked at D+1 midnight. Historical research proxy executes a D signal at the following row's close (D+2 midnight). Live jobs additionally know actual data availability and evaluation timestamps.

The first daily attempt is at or after **01:00 UTC / 08:00 WIB**, not a guaranteed provider publication time. The worker retries hourly until a fresh complete row is accepted, then waits for the next day. Its heartbeat updates every minute. There is no heavy tick feed.

**Paper mode:** reserve a future eligible close strictly after proposal evaluation. If publication is late, move the planned close forward; never use a past price. Record the reference price timestamp separately from the later recording/publication timestamp. This is a preplanned close-price proxy, not evidence of an exchange fill. If downtime skips the designated close, expire the pending paper action and assess current conditions; do not backfill missed orders.

**Alerts mode:** send the proposal only. Balances never change merely because an alert was sent. The operator records actual filled units, USD reference price, USD fee and actual fill timestamp with `confirm-fill`. Partial fills may keep the action pending; an explicit completion/cancellation releases it. Duplicate fill IDs cannot debit twice, and an ID with changed transaction details is rejected. Negative balances, future or pre-proposal fill timestamps are rejected. This v1 manual ledger is USD/BTC accounting only; non-USD quote/fee-currency accounting requires a separate tested adapter, not an undocumented conversion.

No Binance trading credentials, withdrawal credentials or real order endpoint. Optional future Binance pricing/execution adapter is a new operational variant requiring explicit approval and tests. No real order execution is provided in this release.

## Notifications and persistence

One separate SQLite database in `/data`, WAL + FULL synchronous writes. Accounts, accepted raw history, source vintages, decisions, pending actions, fills, daily marks, errors and Telegram outbox persist across restart. No exposure of database to intraday engines. Online backups use SQLite backup, not copying an active WAL database file alone.

Telegram is optional. Credentials only in ignored runtime `.env`, not image/Git/logs. Outbox delivery retries retain the same ID; ambiguous transport can duplicate a message, never a fill. Superseded action proposals are not delivered as fresh buy/sell alerts. Health errors and recovery are logged/notified. Notification outage does not erase a valid pending action or commit invented fills.

## Portfolio reporting and limits

Branch equity = its cash + its BTC * the common dated Coin Metrics USD mark. Combined equity = sum of branches. Compute combined DD from the combined curve, not mean branch DD. Report open/closed balances distinctly; marked BTC and realized cash are comparable on the same date, not a final-cycle winner claim.

Daily paper marks are durable snapshots. In alerts mode, operator intraday fills enter the next accepted daily mark; current balances are separately valued at the last reference price and explicitly labelled, not presented as current executable quotes. Historical replay metrics use 365-day crypto annualization, rf=0, population SD Sharpe and all-day downside-RMS Sortino for research parity. Waiting in cash contributes to elapsed CAGR time.

Initial 50:50 funding does NOT cap portfolio BTC exposure to 50%. Both can be invested together. Historical DD above 50% remains possible; full allocated capital is at risk. Same-asset rules are not asset diversification. Neither fixed extremes nor P5 are guaranteed to produce an entry in every future halving cycle. Very few independent cycle trades support the historical evidence; daily observations are not independent trades. P5 selection is post-hoc and must now remain frozen during forward observation.

## Runtime acceptance / review / retirement

- Continue paper observation only while data/fill/account reconciliation passes; never auto-promote based on historical ending wealth.
- Review on provider revision, persistent data age/gap, missed planned fill, imbalance, configuration change or disparity from offline parity. Reject malformed inputs immediately.
- No automatic price-based retirement/stop has been inserted; changing market-risk rules requires a new contract/backtest.
- Before real-money use: explicitly approve a venue/currency/fill model, capital/risk limits, independent forward evidence and operational recovery. This release is not that approval.

## Legacy and deployment

Production entrypoint: `python -m live_mvrv.main run` from the self-contained `live_mvrv` image. Legacy accumulation CLI, paper/CVDD experiments and research stay available but are not imported into this runtime. Deploy this directory as a separate Compose project/container, with one dedicated volume. No Chromium, GPU, exposed inbound port or extra database container.

Docker health status does not itself restart an unhealthy process. Application retries handle transient failures; fatal exit uses restart policy. A halted source must generate persisted errors/alerts and be investigated. Freshness is daily, not the intraday services' 180-second limit. Existing two server containers are untouched by local installation.

Change log: v1 on 2026-10-03 — frozen two-model branch selection and initial 50:50 funding; no strategy optimization during implementation.
