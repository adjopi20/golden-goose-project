# Trend-Following ORB Agent Rules

Status: live paper-trading judge rules for one model only.

This agent evaluates only trend-following ORB continuation setups.
Do not evaluate mean reversion, sweep absorption, structural-low bounces, or POC/VAH/VAL reversal setups here.

## Allowed Setup Types

- ORB high breakout continuation.
- ORB low breakdown continuation.
- Reclaim continuation after price loses and then reclaims an important level.
- Failed reclaim continuation when price rejects an important level and continues away.

## Required Decision Boundary

- Use only closed 1-minute candles and already observed raw aggTrade/orderflow evidence.
- Entry can only be after the signal/confirmation candle closes.
- Stop must be known at decision time.
- No future wick, future low, future high, or future target behavior may define the trade.

## Trend Long

Take only when price closes above or reclaims an important level with directional acceptance.

Valid supporting evidence:

- close above ORB high, pre-NY high, overnight high, previous NY high, or prior-24h value level;
- retest holds above reclaimed level;
- orderflow supports continuation;
- there is clean room before nearby opposing structure.

Stop:

- below the nearest known invalidation level below entry;
- valid anchors: reclaimed level, ORB low, pre-NY low, overnight low, previous NY low, or signal candle low;
- reject if the stop requires a future candle extreme.

## Trend Short

Take only when price closes below or rejects/reclaims lower from an important level with directional acceptance.

Valid supporting evidence:

- close below ORB low, pre-NY low, overnight low, previous NY low, or prior-24h value level;
- retest fails under broken level;
- orderflow supports continuation;
- there is clean room before nearby opposing structure.

Stop:

- above the nearest known invalidation level above entry;
- valid anchors: broken level, ORB high, pre-NY high, overnight high, previous NY high, or signal candle high;
- reject if the stop requires a future candle extreme.

## Reject

Reject if:

- the setup is mean reversion or sweep absorption;
- the setup is only a p99 bubble without directional acceptance;
- the stop is not known at decision time;
- the target room is poor before major opposing structure;
- the candle is only a wick through the level without acceptance;
- the trade needs a hindsight stop to survive.

## Output

For TAKE, return strict JSON with:

```json
{
  "decision": "TAKE",
  "entry_model": "trend",
  "direction": "long",
  "entry": 0.0,
  "stop_loss": 0.0,
  "reason": "...",
  "invalidation": "..."
}
```
