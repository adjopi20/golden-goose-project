# Live trading

Seven registered submodels trade Binance USD-M Futures. BTC MVRV fixed 0/7 and
four-year P5/P95 share one signal-only service and send their own indicator
updates. Pre-NY ETH is retired from live. Research copies remain in
model_development/models.

| Binance account group | Trading workers | Collateral |
| --- | --- | --- |
| Burst | HYPEUSDT, INJUSDT, UNIUSDC | USDT and USDC |
| Quiet core | HYPEUSDT Stable, ETHUSDC Stable, UNIUSDC Stable | USDT and USDC |
| Quiet tail | HYPEUSDT Tail | USDT |

Each trading worker starts at 100 of its quote asset in its own geometric
ledger. Position risk follows the package, including initiative sizing where
applicable. Actual broker collateral, locked margin, leverage and exchange
filters cap order quantity. A strategy's equity changes only through its
own realized P&L, fees and funding. Funding the account does not rewrite
the strategy ledger.

One public market collector publishes through JetStream. One execution
container per strategy consumes its symbol and manages its native orders.
Strategies sharing a symbol use separate Futures accounts, which gives
each stop one owner. The notifier is a separate Compose project with no
trading credentials and uses read-only journal mounts. It can report a
worker or collector outage while the host and notifier remain up. An outage
of the host itself requires a watchdog outside that host.

From the project root, package import and verification are offline.

```python
from live_trading.live_model import load_model, verify_release
verify_release()
btc = load_model('btc_mvrv_fixed')
```

## Burst Breakout

The selected packages use one strategy and one pinned contract per asset.

| Asset package | Profile and flow | Exit | Filter | Base risk | Sizing policy |
| --- | --- | --- | --- | ---: | --- |
| `burst_breakout/hypeusdt` | B, 16H, 3m/12m | RM-E 48H | F P1 | 1.00% | Initiative 1.5x and one protected add up to 80% at +2R |
| `burst_breakout/injusdt` | B, 24H, 5m/10m | RM-E 48H | F P15 | 0.90% | Initiative 1.5x, single entry |
| `burst_breakout/uniusdc` | B, 12H, 1m/5m | RM-A 48H | F P10 | 1.00% | Initiative 1.5x, single entry |

The contracts pin the backtest source, model settings, sizing tags, engine
release and distinct journal identities. They journal the closed-window
efficiency score, threshold and intended risk at each confirmation. The
threshold uses the last 100 observed confirmations and begins filtering after
30. Replay can populate that history. Orders require a fresh confirmation.

Each live worker owns one symbol. HYPE and INJ use one USDT admission lock
and track realized P&L and fees in separate journals. Pending HYPE adds defer
new INJ entries until reconciliation. RM-E takes 10% at +1R and keeps the
initial POC stop until +2R. HYPE then allows one add on a fresh print after
the +1R stop is confirmed and TP1 has filled. The add uses the position's
existing native close-position stop and 48-hour deadline.

The existing `python -m live_trading.burst_runtime` supports shadow mode and
coordinated single-symbol execution. Compose and the notifier point to the new
journal names in the asset contracts. The journals for the previous versions
remain historical. Before cutting over, reconcile existing open positions,
exchange-native stops and account identity. Export any compatible journal
read-only with `--database <path> --report`.


## Quiet Breakout HYPEUSDT

The live registry declares `quiet_breakout_stable` and
`quiet_breakout_tail_dependent`. Both use the HYPEUSDT asset strategy.
Each variant's folder contains its pinned policy, execution identity, and
source backtest receipt reference.

```text
live_model/quiet_breakout/hypeusdt/
├── strategy.py
├── contract.json
├── stable/model_config.json
└── tail_dependent/model_config.json
```

| Variant | Profile | Entry filters | Exit |
| --- | --- | --- | --- |
| `stable` | 24 hourly candles | Volume P75 and path P5 | RM-E, 48h |
| `tail_dependent` | 16 hourly candles | Volume P75, stop distance P85 and path P5 | RM-A, 48h |

Both use a closed 1H breakout of the full profile range, 3m/12m static
flow confirmation and at most one entry per UTC day. Risk is 1% of
current allocation equity, or 1.5% when initiative confirms without
exhaustion. The 5× notional cap applies at either risk level.

Use `python -m live_trading.live_model.quiet_breakout.hypeusdt.strategy --help` for
the worker options. With the existing Burst live image, select the Quiet
module as the container entrypoint. Configure one variant per worker
with its own fresh journal and external USDT allocation and credentials.
The journals are `quiet_breakout_stable.sqlite` and
`quiet_breakout_tail_dependent_position_addition.sqlite`. In the coordinated portfolio, HYPE Stable and HYPE Tail use different
USD-M Futures accounts in one-way, single-asset mode with isolated margin
and auto-add off. Each can share collateral with a different-symbol worker. Use a separate directory for a
disarmed smoke journal. Use the existing read-only exchange preflight
before arming. A fresh journal warms up from the public feed and uses the
first 30 baseline confirmations to initialize the rolling filter history.

For `tail_dependent`, the P85 score is the absolute distance from the
confirmation close to the initial POC stop divided by the confirmation
close. The score must be at most the 85th percentile of prior confirmed
setups. The P5 score is the path efficiency over the 12-minute confirmation
window. It must be at least the 5th percentile. Each threshold uses the
prior 100 baseline confirmations, with the first 30 used for warm-up.
The same P85 cap is checked against the executable quote and the actual
average fill. Native order fills, fees and funding can differ from the
historical print replay.



## Quiet Breakout ETHUSDC

The live registry declares `quiet_breakout_ethusdc_stable` under
`live_model/quiet_breakout/ethusdc/`. Its pinned policy comes from
`ethusdc_combined_v01_p85_after`. The setup uses a 24H profile,
hourly breakout volume P75, static 3m/12m flow, stop distance P85,
path efficiency P5, and one entry per UTC day. Risk is 1% of current
allocation equity, or 1.5% for initiative without exhaustion. The
notional cap is 5× equity. RM-E takes 10% at +1R, holds the POC stop
until +2R, and applies the shared trailing ladder. Full exit is due
24 hours after the first fill.

Use `python -m live_trading.live_model.quiet_breakout.ethusdc.strategy --help`
for the worker options. Select this module as the entrypoint in the
existing Burst live image. Configure `--allocation-usdc`, the external
Ed25519 credentials and a fresh `quiet_breakout_ethusdc_stable.sqlite`
journal. The dedicated Binance USD-M Futures account uses one-way,
single-asset mode and isolated margin with auto-add off. Run the
read-only exchange preflight before arming. In the coordinated portfolio ETHUSDC runs on the Quiet core account. Keep shadow journals in a
separate directory. The package is prepared offline.

## Quiet Breakout live portfolio selection

| Submodel | Tags | Journal |
| --- | --- | --- |
| ETHUSDC Stable | `stabil` | `quiet_breakout_ethusdc_stable.sqlite` |
| HYPEUSDT Stable | `stabil` | `quiet_breakout_stable.sqlite` |
| HYPEUSDT Tail Dependent | `tail_dependent`, `position_addition` | `quiet_breakout_tail_dependent_position_addition.sqlite` |
| UNIUSDC Stable | `stabil`, `position_addition` | `quiet_breakout_uniusdc_stable_position_addition.sqlite` |

The tags record the selected variant. UNI with addition had four positive
quarters out of five in the development replay. Both addition variants
pin the completed replay for that policy. UNI uses profile 16H, volume
P75, flow 1m/5m, path P5, stop distance P85, RM-E and full exit 48h.
Initial sizing is 1% of allocation equity or 1.5% for initiative without
exhaustion.

An add is eligible when the stop first locks at least +1R after a move of
at least +2R. The worker waits for a later fresh print, confirms the native
close-position stop and current position, and sizes one market add from
the executable quote. Quantity is capped at 80% of original quantity,
80% of original initial price risk and remaining notional and margin.
UNI requires its original TP1 to be filled. Both legs use one native
close-position stop and one exit clock. The live worker polls and
reconciles exchange orders, so its fill timing can differ from the
research next-print replay. If a fill breaches the planned cap, the
worker requests a reduce-only close of the position.

For HYPE use the existing worker with `--variant tail_dependent`. For UNI
use `python -m live_trading.live_model.quiet_breakout.uniusdc.strategy --help`.
The coordinated portfolio uses the account groups listed above and a fresh
external journal per worker. The legacy standalone command uses a dedicated
account. See the deployment README for the coordinated entry point.
