# Deployment options — 2026-09-27

Scope: planning only; no server purchase, account connection or live order. Primary documentation checked on the date above; fees, APIs and product availability can change.

## Recommendation for this project

Use the existing Python strategy logic on a small Linux VPS, with a shared data/strategy layer and separate Binance/Lighter execution adapters. This is an architectural recommendation based on the existing code and desired multiple venues, not proof that custom software is inherently more reliable than MultiCharts.

Start with one active order-sending service, a persistent ledger, one shared feed per venue/product, and configuration per model. Reuse calculations; do not duplicate raw ingestion for every model. Do not introduce Kubernetes or active-active order senders for three models. Separate historical sweeps from the live host. A VPS eliminates dependence on an awake laptop, not software or exchange failure.

## MultiCharts versus DigitalOcean

| Topic | MultiCharts | Python on DigitalOcean |
|---|---|---|
| Main advantage | Existing charting/automation/broker tooling | Reuse current feature/strategy code and add venue adapters |
| Main work | Port/bridge exact profile, delta and state semantics; prove parity | Build/test execution lifecycle, monitoring and recovery |
| Multiple models | Platform supports automation and portfolio tooling, edition-dependent | Natural configuration-based workers with shared feed and account coordinator |
| Binance | Official release notes confirm maintained integration | Official futures APIs available |
| Lighter | Native supported connector not verified in this audit | Official SDK/API supports a custom adapter |
| Cash cost | Paid automation plan plus always-on host and any required data; current paid quote not reliably exposed in fetched page | Example Basic VPS $12/mo (1 vCPU/2 GiB) or $24/mo (2 vCPU/4 GiB), before backup/tax/storage/monitoring |
| Hidden cost | Integration limits, porting and validation time | Engineering, security, API maintenance and on-call responsibility |

DigitalOcean's published Basic table includes $24/month for 2 vCPU/4 GiB. Treat that as an initial paper-host budget, not a measured capacity guarantee. Benchmark trade ingestion/reconnect bursts; shared CPU is variable. See [official Droplet pricing](https://www.digitalocean.com/pricing/droplets).

MultiCharts' [purchase page](https://www.multicharts.com/purchase/) lists algorithmic features by plan, but this audit could not verify the dynamically rendered paid license price. [Release 7](https://www.multicharts.com/traders-blog/multicharts-16-release-7/) confirms Binance API integration maintenance. This does **not** certify USDC-margined contracts, our aggressor-delta feed, every conditional order, or a Lighter connector. Ask the vendor for those exact capabilities before buying. Do not assume a generic Binance logo guarantees this model's parity.

For this specific user, Python/VPS is the more feasible route because the research is already Python and the two-venue requirement is material. MultiCharts remains reasonable if its exact connectors are verified and the user values a packaged GUI over reusing the present code.

## Binance and Lighter algorithmic feasibility

**Binance USDⓈ-M:** official signed order APIs, conditional/algo orders and user/account streams are documented. The current API exposes `CONTRACT_PRICE` and `MARK_PRICE` triggers; do not silently replace the trade-price triggers used by research with mark-price triggers. See [futures trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade) and [general information](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info).

USDC-margined trading must use the correct market metadata, margin asset, size/tick filters and account permissions. HYPE's historical contract is HYPEUSDT: USDC collateral preferences do not make it a tested HYPEUSDC instrument. Recheck available products and commission rates for the actual account rather than treating historical 4 bps as today's fee quote.

**Lighter:** API-based trading is feasible through its official SDK, signed requests and WebSockets, with a testnet. API keys/nonces are account-specific and must survive concurrency/restart correctly. See [getting started](https://apidocs.lighter.xyz/docs/get-started), [Python SDK](https://github.com/elliottech/lighter-python), and [WebSocket reference](https://apidocs.lighter.xyz/docs/websocket-reference).

The [official account-type table](https://apidocs.lighter.xyz/docs/account-types) lists Standard maker/taker fees as 0%, taker latency 300 ms, and cancel/modify latency 300 ms. Premium is a paid alternative, not the same zero-fee account. [Official API limits](https://apidocs.lighter.xyz/docs/rate-limits) list Standard at 60 requests/minute; reads and order traffic must be budgeted. A 15m strategy is plausibly compatible, but multiple models/recovery bursts still need rate-limit tests. Advanced order products may have different charges; verify the actual order type and account fee schedule before relying on zero.

Zero maker/taker commission is not zero total cost: spread, price impact, slippage, funding, transfers/withdrawals and operational/venue risk remain. It does not justify immediately increasing trade frequency or changing the frozen entry rules. Available ETH/BNB/HYPE markets, contract metadata, reduce-only partials, trigger basis, native stop behavior and minimum quantities must be checked in the adapter/test environment before approval. This audit establishes API feasibility, not per-symbol execution certification.

## Fair two-venue study

Keep research-model identity separate from execution-venue identity. Two different experiments:

1. **Execution comparison:** one frozen signal stream and matched decision timestamps sent to two independent paper adapters. Record signal-to-ack/fill latency, fill versus decision mid, spread/impact, fees, funding, rejections, partial fills and uptime. A common signal isolates execution differences. Cross-venue price/stop basis must be explicit; do not claim the original backtest validates it.
2. **Full venue-native model comparison:** build each profile, delta and indicator from that venue's own trades. Same formulas, different signals are allowed. This tests feed plus strategy plus execution, not fees alone. Lighter has no validated historical parity dataset in this checkpoint.

Do not mix the two in one performance table. Start with shadow logging on both venues while validating Binance's researched-feed parity. Keep separate ledgers and allocated capital; two copies of the same position are correlated exposure, not diversification. Shared-account risk allocation is not established by the current three independent-account tests.

## Minimal reliability contract to implement next

- Unique stable `model_id / venue / session / intent_id`, idempotent submission, query-before-retry on unknown status. Exactly one active writer per account/order namespace.
- Reconcile exchange orders/positions/fills at startup and reconnect; persist state before acknowledging actions. Never resubmit yesterday's entry after a restart.
- Exchange-native protective stop when supported; verify acknowledgment. Coordinate partial and remaining stop size with reduce-only semantics; prevent orphaned/reversing exits.
- Explicit partial-lot rounding and dust handling. Paper test 10% partials against small account minimum orders.
- UTC clock sync and DST-aware NY calendar; time-exit watchdog and alerts when flattening fails. No claim of exactly 08:59 fill under outage.
- Trade-only least-privilege credentials, no unnecessary withdrawal permission, supported IP restrictions, secret storage outside Git/logs, signed-request nonce management.
- Stale/gap detection, backfill before signals, rate-limit backoff, monitoring independent of the trader process, backup/recovery drill. Log theoretical signals even if an operational interlock blocks execution.
- Record actual fee/funding and mark-to-market equity; compare with theoretical ledger daily. Keep data-health controls separate from alpha/indicator filters.

Next deliverable should be a small paper adapter and parity/recovery test plan, not a new indicator, a new strategy, or a new backtest engine. Live activation requires separate explicit approval after operational criteria and capital limits are agreed.
