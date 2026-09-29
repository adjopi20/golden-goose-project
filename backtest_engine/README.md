# Backtest Engine

Reusable, strategy-agnostic backtest infrastructure lives here.

Current scope:

- `ExitPolicy`: normalized fixed, trailing-only, and partial-plus-trailing
  configuration independent of any trading model.
- `build_exit_policy_grid`: deterministic Cartesian parameter grids for exit
  sweeps; it does not inspect outcomes or select a winner.
- `calculate_shared_backtest_metrics`: cost-aware returns, R metrics, equity,
  drawdown, loss streaks, holding time, quarterly stability, and trade frequency.
- `write_shared_backtest_result`: standard `trades.jsonl`, optional order and
  decision ledgers, plus `summary.json`.
- `replay_aggtrades`: generic next-trade execution replay for any model that
  exports the normalized signal JSONL contract below. It owns position sizing,
  initial-stop/fixed-target/partial-TP1/time exits, fees, order ledger, and equity curve. It does not
  import a strategy or assume any profile/session time.

The normalized signal contract requires `sample_id`, `session_day` (ISO date),
`direction` (`long`/`short`), `entry_reference`, `stop`, and epoch-millisecond
`entry_eligible_timestamp_ms`, `entry_deadline_timestamp_ms`, and
`force_exit_timestamp_ms`. `strategy` and `route` are optional attribution.
Each signal file is replayed as an independent portfolio. Use an observation
directory containing sibling `signals.jsonl` files or repeat `--signals
LABEL=PATH` for other models.

Example, two independent BTC profile windows:

```powershell
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe -m backtest_engine.replay_aggtrades `
  --input "storage/btcusdc/BTCUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet" `
  --observation-dir "models/trend_following_preny_profile_15m/runs/btcusdc_2026-08_v2" `
  --start-date 2026-08-01 --end-date 2026-08-30 `
  --output-dir "models/trend_following_preny_profile_15m/runs/btcusdc_2026-08_v2_backtest_shared" `
  --initial-equity 1000 --risk-fraction 0.005 --fee-bps 4 `
  --max-leverage 5 --max-entry-delay-seconds 300
```

The shared replay supports initial-stop, optional fixed-target and signal-specified time
exits. It uses the next aggregate-trade price as a fill proxy, not order-book
bid/ask, market impact, or funding. The supplied aggTrades file ends before
the next-day time exit for 2026-08-31, so the example stops at August 30.

## Integrated raw-trade reward and configuration sweeps

Use this same CLI with `--target-r 1 1.5 2` to replay all three rewards for
every supplied signal group in one raw-data pass. Each group/config has its
own equity and position. Omitting the flag preserves the old stop/time exit.
No observation rerun or strategy-specific backtest script is needed.

For other combinations, use `--grid path/to/grid.json` instead of `--target-r`:

```json
{"target_r": [1, 1.5, 2], "risk_fraction": [0.005], "fee_bps": [4]}
```

Every key is a list; the Cartesian product is tested. Supported keys are
`target_r`, `initial_equity`, `risk_fraction`, `fee_bps`, `max_leverage`, and
`max_entry_delay_ms`, `tp1_r`, and `tp1_fraction`. Omitted keys inherit the normal CLI settings. `null`
in `target_r` includes the original stop/time-exit control. Invalid, duplicate,
or unknown parameters fail explicitly. No automated outcome-based selection.

Fixed target is anchored to the **actual entry fill** and the supplied initial
stop: `entry + side * target_r * abs(entry - stop)`. Crossing the target queues
a full market exit on the next aggregate trade (not a guaranteed limit fill).
The original hard stop and time cutoff remain active. Net realized R includes
fees and price movement between trigger and fill, so it need not equal target R.
Pending stop/target orders execute before checking the next tick's time cutoff.

Output: `comparison.csv`, `sweep_manifest.json`, `complete.json` (success only),
and one folder per group/config with the standard trades, orders, decisions,
summary and equity curve. Drawdown here is **closed-trade equity drawdown**,
not intraposition mark-to-market drawdown. Do not analyze an incomplete run.

For partial TP1 plus a time-exit remainder, use this grid:

```json
{"tp1_r": [1, 1.5, 2], "tp1_fraction": [0.1, 0.2, 0.5]}
```

This tests nine combinations. TP1 fills on the next print after crossing;
the remainder retains the original stop and signal-specified force-exit time.
No breakeven or trailing is implied. Supply both TP1 fields, and do not combine
them with a full fixed target. Risk sizing compounds from current equity after
each complete trade. Partial PnL is consolidated at final exit for the closed-
trade equity curve. `--plot-equity` adds an equity/drawdown PNG per portfolio.

The raw replay implements fixed-target, partial-TP1/time, and stop/time exits.
The trailing `ExitPolicy` grid below is a shared configuration interface
used by older adapters; trailing is not yet executable through this raw CLI.
Future exit modes should extend this engine, not introduce another model-specific
backtest. Funding, bid/ask spread, queue position and market impact are not modeled.

Strategy signal generation and model-specific stop/entry/exit rules do not
belong in this package. The shared replay reads stop and time-exit parameters
from each signal without depending on ORB or Macro Regime code.

Example grid:

```python
from backtest_engine import build_exit_policy_grid

policies = build_exit_policy_grid(
    modes=("fixed", "trailing_only", "partial_trailing"),
    fixed_targets_r=(1, 2, 4),
    tp1_r_values=(1, 2),
    tp1_fractions=(0.25, 0.5),
    trail_activation_r_values=(1, 2, 4),
    risk_trail_distances_r=(2, 3, 4),
    atr_trail_multipliers=(3, 5, 8),
    protection_floor_r_values=(None, 0, 1),
)
```

The model-specific replay adapter must apply every policy to the same frozen
entries and price paths. Selection belongs to chronological walk-forward
validation, never to the full-sample leaderboard.

The first adapter is
`models/orb/scripts/pre_ny_submodels/sweep_pre_ny_stateful_portfolio_exits_cp001.py`.
It exposes fixed targets, TP1 levels and fractions, trail activation, R-distance
and ATR-distance trails, optional protection floors, and trailing-only mode.

The latest integrated ORB adapter is
`models/orb/scripts/pre_ny_submodels/sweep_pre_ny_integrated_portfolio_exits_cp004.py`.
It reads the frozen `routed_candidates.jsonl`, requires an explicit Inside-Value
stop policy, ignores observer outcome fields, and can calculate trailing distance
from causally completed five-minute ATR bars. It reuses the same simulator and
metrics instead of maintaining a second execution engine.

The CP004 adapter currently performs one-minute candle exit research with fixed
fees. Exact aggregate-trade fills, spread, market impact, funding, and multi-asset
walk-forward selection remain separate execution-validation steps; its leaderboard
must not be treated as a deployable result by itself.
