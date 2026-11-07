# Paper model packages

Each operational package contains one strategy.py and a source/engine-pinned
contract.json. Research copies stay permanently in model_development/models.
A submodel appears in paper OR live, never both. Promotion moves this whole package
unchanged; external live account state always starts fresh.

| Package | Binding | Readiness |
| --- | --- | --- |
| pre_ny_hype | Binance HYPEUSDT C1; existing paper manager | Packaged; fixed October paper calibration expired and was not renewed. Research uses independent causal calibration. |
| pre_ny_bnb | Binance BNBUSDC C2 initiative; existing ADX risk and 10% TP1 at 2R | Packaged; existing legacy paper contract, not the ETH-only Futures lifecycle. |
| burst_breakout | HYPEUSDT; 16H B, static 3m/12m, rm-e, 48H | Paper-only next-print infrastructure; source/decision parity and offline lifecycle tests pass. Not bound to live Futures execution. |
| volume_profile_reversion | LTCUSDC existing paper_v33; Model/PocPaperManager | Preserved paper behavior. Research reference v31 baseline_intersection is historically profitable; that evidence does not validate v33 fills/exit/2.5% risk. |

HYPE/BNB create_paper_manager(store, venue='binance') binds their existing account
preset to the shared manager, without starting a feed or activating evaluation.
SessionObserver/as_intention retain their input/activation-hook contracts. LTC
retains its existing Model/PocPaperManager interface. Import packages explicitly,
for example paper_trading.models.pre_ny_bnb.strategy; they do not import research
calculations. Account stores/configuration/calibration and consumer cursors are
external. Never copy paper balances/positions/pending orders into live.

Shared services are under utils/engine; current contracts pin
releases/shared_engine_20261103_v3.json and its 52 sources. Existing lifecycle
restrictions remain explicit. No calibration renewal, market backtest, cache build,
service start, account migration, notification or deployment was performed.

## Selected HYPE burst paper â€” 2026-11-07

The selected candidate is 16 prior completed 1H candles for both the quantity
profile (50 bins, 70% value area) and body/volume P85. Setup B permits the previous
close outside value area. Both burst body and volume must exceed P85; close must
be outside VAH/VAL. Static 3m/rolling-12m flow is first assessed at minute 12,
then every 3 minutes through minute 60, without an extra entry gate. A new hourly
setup freezes its own rolling profile. Profiles continue advancing during positions;
setups keep being observed while a position is open. Position/daily blocking is
checked at confirmation time, matching research; a blocked confirmation is discarded,
not queued for later entry.

Initial stop is the frozen POC. At 1R, 10% of original quantity exits on the next
print; the stop stays at POC. At 2R/4R/8R the stop locks +1R/+2R/+4R, mirrored for
shorts. The remainder exits at the first print at/after 48 hours from the actual
entry fill. Risk is 1% of running closed equity, max leverage 5x, one open position
and one filled entry per UTC day. Fees are 4 bps per executed notional; no Kelly
increase was adopted. The simulation uses the existing shared paper_execution
and PaperOrderManager; no shared engine source, old package pin or research
strategy/run was changed. Calculation snapshots in the paper strategy match the
maintained research functions; its profile primitive and engine are source-pinned.

External runtime configuration: paper_trading/config/burst_hype_16h_b_3m12m_rm_e_48h.json.
This paper account starts at 1,000 USDT. It is separate from Pre-NY HYPE and from
any future live account. Do not copy its DB into live. The retained research model
stays in model_development/models/burst_breakout. There is no live stage copy.

From the project root, start the model's single strategy module (no new
backtest/observe/evaluate runner or separate per-model service script):

```powershell
.\.venv\Scripts\python.exe -B -m paper_trading.models.burst_breakout.strategy `
  --config paper_trading/config/burst_hype_16h_b_3m12m_rm_e_48h.json `
  --database tmp/paper/burst_hype_16h_b_3m12m_rm_e_48h_v01/paper.sqlite `
  --action run
```

Use --action init for offline initialization or --action status while the process
is stopped. Status reports the account, last native trade/receipt, profile and
flow state; Ctrl+C stops the foreground process. Normal stop preserves pending,
partial trigger, stop ladder, account equity and native history transactionally.
Only one writer may open this account. No API key, trading permission, NATS or
Telegram is used. Public input reuses BinancePublic. The first partial minute is
excluded; native price/quantity accumulation builds the profile, never an OHLC
approximation of volume at price. Cold start needs native history (16 prior full
hours plus the burst hour, and the existing 24H flow baseline, which accepts at
least 90% of its reference windows). No old research cache is relabeled as current
public-feed coverage.

The runtime deliberately halts on an ID gap/reset, conflicting duplicate,
regressed time or a print older than 10 seconds. It does not silently fill through
an unverified reconnect gap. After an interrupted feed, do not force/adopt an old
cursor or reuse a funded account as a fresh one: reviewed native gap repair is
required before this same DB can continue, or use a genuinely new paper DB.
Automatic reconnect/backfill is not implemented by this package.

Validation: 24 offline tests pass (the package plus shared paper-manager regression),
including native prints -> burst -> flow -> next-print entry, both trade directions,
partial TP, POC through 1R, doubled trailing, 48H exit, daily cap, geometric sizing,
restart, malformed/stale/gapped input rollback, single-writer and failed-journal
replay. All five pre-existing operational model/engine pins still verify. A separate
30-second public-feed smoke received HYPE trades into a new diagnostic paper DB,
with no candidate, position or fill. That process ended; no persistent service was
installed or started. No historical market backtest or cache build was run.

This is the declared legacy next-print paper contract, not the supported ETH-only
Futures executor. It has no orderbook/depth/rounding, exchange ACK/reconciliation,
funding or liquidation model. Its decisions and financial mechanics can be tested
here; live order-lifecycle binding and measured exchange execution remain separate
work before promotion. Paper infrastructure checks do not establish live fill parity.
