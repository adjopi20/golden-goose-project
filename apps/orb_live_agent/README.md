# ORB Live Agent

Paper-only live agent shell for AVAXUSDC ORB monitoring.

This app records market state first. It does not place real Binance orders.

## Flow

```text
Binance aggTrade websocket
  -> raw JSONL trade log
  -> closed 1m candles with delta
  -> session extremes
  -> fixed previous-24h profile for the NY session
  -> NY first-15m volume profile
  -> order bubbles
  -> trigger observation log
  -> trend-following decision service
  -> risk gate
  -> paper broker
```

Existing repo code reused:

- `indicator.ohlcv.aggregate_trades_to_ohlcv`
- `indicator.volume_profile.build_volume_profile`
- `indicator.deep_trade.build_order_bubbles`

## Local Run

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH=".;apps/orb_live_agent/src"
python -m orb_live_agent.main
```

## Docker Run

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project\apps\orb_live_agent
docker compose up -d --build
```

Docker uses the frozen AVAXUSDC `variant_009` paper profile at
`profiles/avaxusdc_variant009_paper.conf`. It uses deterministic algorithmic
entries, cannot place real Binance orders, and writes isolated logs under
`apps/orb_live_agent/data/live_paper/avaxusdc_variant009_v1/`.

```powershell
docker compose logs -f orb-live-agent
```

Logs are written under `apps/orb_live_agent/data/`.

## Session Windows

Defaults are New York local time:

```text
Previous/current NY: 09:30 <= time < 17:30
Overnight:           17:30 <= time < 01:30
Pre-NY:              01:30 <= time < 09:30
```

`SESSION_TIMEZONE=America/New_York` makes the WIB equivalent shift automatically
with US daylight saving time. AI setup observation is allowed only during
`09:30 <= time < 17:30` New York time. Paper positions are still managed after
`17:30` until SL/TP/trailing closes them.

Volume-profile horizons:

```text
previous_24h_profile_for_session:
  09:30 previous NY day <= time < 09:30 current NY day
  frozen at NY open

ny_first_15m_profile:
  09:30 <= time < 09:45 current NY day
```

## Paper Execution

Paper execution is parameter-driven, not AI-decided:

```text
PAPER_FEE_BPS=4
PAPER_SLIPPAGE_BPS=5
PAPER_MIN_STOP_RISK_PCT=0.0015
PAPER_MAX_STOP_RISK_PCT=0.025
PAPER_TP1_R=4
PAPER_TP1_FRACTION=0.5
PAPER_RUNNER_TRAIL_TP1_FRACTION=0.5
PAPER_EXIT_MODE=tp1_trail
PAPER_TRAIL_ACTIVATION_R=4
PAPER_TRAIL_DISTANCE_R=2
PAPER_PROTECTION_ENABLED=true
PAPER_PROTECTION_ACTIVATION_R=1
PAPER_PROTECTION_STOP_R=0
PAPER_MAX_HOLD_EXIT_TIME=01:30
```

Fees are charged on entry and exit notional. Slippage worsens entry and exit
fills. The AI proposes entry model, direction, entry price, stop, and rationale.
This live agent is trend-following only, so `entry_model` must be `trend`.
The paper execution engine ignores AI take-profit output. Trend trades close
50% at 4R, then trail the remaining 50% by half the entry-to-TP1 distance.
`PAPER_EXIT_MODE=trail_only` skips TP1 and starts a full-position trail at
`PAPER_TRAIL_ACTIVATION_R`. `PAPER_TRAIL_DISTANCE_R` is measured in initial R.
Protection can be disabled, activated at another R multiple, or lock profit with
`PAPER_PROTECTION_STOP_R` (`0` means breakeven).
If neither stop nor TP/trailing resolves the trade, the broker force-exits at
the first raw aggTrade at or after `PAPER_MAX_HOLD_EXIT_TIME` on the next NY day.
The risk gate rejects trend trades with stop risk below `PAPER_MIN_STOP_RISK_PCT`
or above `PAPER_MAX_STOP_RISK_PCT`.

## Overnight ORB Backtest

Use a separate cache/output folder because the ORB anchor changes:

```powershell
$env:ORB_SESSION_START_TIME="17:30"
$env:ORB_ENTRY_START_TIME="17:45"
$env:PAPER_MAX_HOLD_EXIT_TIME="09:29"
```

This keeps the same ORB strategy logic but builds the 15-minute ORB from
`17:30 <= time < 17:45` and force-exits unresolved positions before the next NY
open.

## Historical Replay Backtest

Historical replay uses the same live state, trigger, decision, risk, and
paper-broker modules. It does not manually pick candidates.

```powershell
cd C:\Users\adjop\OneDrive\Documents\golden-goose-project
$env:PYTHONPATH=".;apps/orb_live_agent/src"
python -m orb_live_agent.backtest_replay `
  --input storage/avaxusdc/parquet/AVAXUSDC-aggTrades-2026-06/AVAXUSDC-aggTrades-2026-06.parquet `
  --start-date 2026-06-02 `
  --end-date 2026-06-08
```

Replay logs are JSONL files under `apps/orb_live_agent/data/backtests/`.
DeepSeek decisions are cached by exact request-body hash in `decision_cache.jsonl`.

## Current Limits

- AI live calls are disabled unless `AI_LIVE_CALLS_ENABLED=true`.
- AI calls are gated by trigger observations and required context availability.
- Binance order execution is intentionally absent.
- Session windows are configurable in `.env`; defaults use New York local time.

## AI Provider

For algorithm-only backtests with no API calls:

```text
AI_PROVIDER=algorithm
AI_LIVE_CALLS_ENABLED=false
```

Keep this while observing market-data logs:

```text
AI_PROVIDER=deepseek
AI_LIVE_CALLS_ENABLED=false
```

When `AI_LIVE_CALLS_ENABLED=false`, the key can be present in `.env` but the app
will not call the model.

Only turn this on after trigger frequency is reviewed:

```text
AI_LIVE_CALLS_ENABLED=true
```
