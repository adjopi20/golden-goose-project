# Live trading

Declared live submodels are ETHUSDC Pre-NY 15m, BTC MVRV fixed 0/7, and BTC MVRV
four-year P5/P95. This describes stage placement, not activation/server verification.

- live_model/pre_ny_eth/strategy.py: frozen decisions, indicators/profile/session;
  contract.json pins its strategy and engine. create_execution calls the shared
  ETH Futures lifecycle with explicit external account configuration and database.
- live_model/btc_mvrv/strategy.py: both related BTC branches share one strategy;
  model_config.json is pinned. This is a Telegram signal monitor, not a portfolio
  or exchange executor. Dedicated signal DB only; funded/trading DBs are rejected.
- HYPE, BNB and LTC paper packages are in paper_trading/models, not this stage.

From the project root, import only (no service is started):

```python
from live_trading.live_model import load_model, verify_release
verify_release()
eth = load_model('pre_ny_eth')
btc = load_model('btc_mvrv_fixed')  # same dual notifier as btc_mvrv_p5_p95
```

BTC has its existing run/once/report/health/backup CLI through
python -m live_trading.live_model.btc_mvrv.strategy. Running it can fetch/send;
no command was run during restructuring. Account state and Telegram credentials
are external. Model imports never open a database or contact a service.

Shared collector/adapters, monitoring, persistence and execution live under
utils/engine. Packages pin shared_engine_20261103_v3 (52 sources). The same public
feed can serve independent paper/live consumers; checkpoints and account state
must remain separate. Promotion moves the operational model package only and
starts a fresh live account, retaining the research copy.

The original six offline candidates/release are now research history at
model_development/models/legacy_frozen_candidates. Original frozen dependencies
still serve compatibility callers; current packages use pinned utils/engine.
Old deployment configurations are compatibility sources, not evidence of new
package activation or end-to-end service conversion. No Docker build/deployment,
account/state move, exchange order or Telegram message was performed.


## HYPEUSDT Burst Breakout

The live package uses setup B with a 24H profile and P85 baseline, static
3m/12m flow, and rm-c. It moves the native stop at 1R, 2R, 4R and later
doubling milestones. The 48-hour clock starts at the first exchange fill.
Sizing uses 1% of the strategy allocation's running equity, adverse quote
padding of 5 bps, and the research 5x notional ceiling. Venue quantity is
rounded down. It uses the account's configured leverage. Entry requires a
dedicated one-way, single-asset USD-M account, HYPEUSDT isolated margin,
auto-add margin disabled, and the approved available USDT allocation.
A first start needs 25 completed hours of native trade history.

Build the exact reviewed Git commit with
`docker build -f utils/engine/deploy/Dockerfile.burst-live --build-arg CODE_VERSION=<commit> -t <image> .`.
The Compose file is `live_trading/deploy/hype-burst.compose.yaml`. Set its
image, allocation, state directory, secret directory and arming environment
outside the checkout. The read-only secret directory holds `api-key`,
`ed25519.pem` and `passphrase` (empty for an unencrypted key).
Use `--preflight` without `--armed` to validate the dedicated account without
sending orders. A public stream smoke can use `--duration 20` without
credentials. Use a separate disposable database for smoke tests.

The persistent SQLite journal contains hourly decisions, order-flow assessments,
entry skips, sizing, exchange order and fill IDs, stop updates, reasons for
closure, commissions, funding and net realized P&L. Export it read-only with
`--database /data/hype.sqlite --report`. Container output contains startup,
feed recovery and heartbeat events. Public-feed disruptions block stale
entries. Venue-native protection remains during reconnects.

The portfolio command uses the same container and HYPE journal and adds
INJUSDT 24H B 1m/5m rm-d 48H and UNIUSDC 12H B 1m/5m rm-c 48H.
Run the first portfolio preflight while the account is flat and the previous
HYPE worker has released the journal. The three symbols require isolated margin
with auto-add off in one-way, single-asset USD-M mode. Set
`UNI_BURST_ALLOCATION_USDC` in the external Compose environment. The approved
HYPE allocation becomes the starting USDT pool. Realized HYPE and INJ P&L,
commission and funding change the same sizing pool, with 1% stop risk per entry
and up to one position per symbol. UNI sizes from its USDC pool.

INJ places a reduce-only 10% limit at +1R and trails the stop to breakeven at
the 1R print. Quantity must fit the venue lot and the risk budget. Keep and
export each journal separately via `--report`: `hype.sqlite`, `injusdt.sqlite`,
and `uniusdc.sqlite`. The first successful portfolio preflight binds the journals
and API identity to the pools. Use disposable copies of the journals for a
preflight rehearsal. Returning to the standalone image after portfolio fills
requires account and journal reconciliation.
