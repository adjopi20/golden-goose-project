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
  -> AI decision stub
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
copy .env.example .env
docker compose up -d --build
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
PAPER_FEE_BPS=10
PAPER_SLIPPAGE_BPS=2
PAPER_TRAILING_ENABLED=true
PAPER_TRAILING_R_MULTIPLE=1.0
```

Fees are charged on entry and exit notional. Slippage worsens entry and exit
fills. Trailing moves the stop by a fixed R distance from the best price since
entry and never widens the original stop.

## Current Limits

- AI live calls are disabled unless `AI_LIVE_CALLS_ENABLED=true`.
- Trigger rules are observe-only; they are logged but do not gate AI yet.
- Binance order execution is intentionally absent.
- Session windows are configurable in `.env`; defaults use WIB labels for live paper testing.

## AI Provider

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
