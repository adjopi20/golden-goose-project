# ORB Setup Logic V2 - Retest/Reclaim With Order Flow

Status: frozen working research checkpoint. Promising, not a proven edge.

Scope validated so far: AVAXUSDC May 30 and May 31 2026 only.

Purpose: preserve the current ORB setup evaluation logic after adding order-flow confirmation on top of the latest candle retest/reclaim logic.

## Core Principle

Breakout establishes directional bias. Retest, reclaim, or rejection establishes entry.

Do not chase an extended breakout unless it is a high-quality prominent breakout. If price is already far from the nearest valid structure, wait for price to return to a known level and prove acceptance or rejection there.

Bias is not entry. A valid bias can produce no trade if the later entry quality is weak.

## Time And Data

- Anchor sessions to `09:30 NY`.
- Display timestamps in WIB when reporting.
- Use 1-minute OHLCV for candle/setup evaluation.
- Use `buy_volume`, `sell_volume`, `delta`, and `taker_imbalance` when available.
- Use raw aggTrade prices for execution checks, TP/SL crossing, and runner trailing.

## ORB Profile

Build the Area A volume profile from `09:30-09:45 NY`.

Track:

```text
profile_low
VAL
POC
VAH
profile_high
```

For the first ORB move, candle behavior is evaluated after `09:45 NY`.

## Structural Levels

After ORB bias exists, store structural levels known before entry:

```text
ORB profile_high / profile_low
pre-NY high / low / VAL / VAH / POC
prior 8h block high / low
notable pre-NY swing levels that price later tests
```

For long bias, important pullback/reclaim levels are supports or reclaimed resistance.

For short bias, important pullback/rejection levels are resistances or reclaimed supports.

## Setup Types

### 1. Prominent ORB Breakout

Use only when the breakout is strong enough that waiting for a retest would likely miss the main move.

Long:

```text
body fully above profile_high
no immediate reclaim back inside profile
close extends in bias direction
range/body/volume are prominent versus recent candles
confirmation-window delta > 0
```

Short:

```text
body fully below profile_low
no immediate reclaim back inside profile
close extends in bias direction
range/body/volume are prominent versus recent candles
confirmation-window delta < 0
```

Entry is next raw traded price at or after the next 1-minute candle open.

### 2. ORB Profile Retest

Use when breakout established bias but price is already extended.

Long:

```text
price pulls back to profile_high or just above it
level is touched or swept
price closes back above the level
next candle confirms acceptance above the level
confirmation-window delta > 0
```

Short is mirrored:

```text
price pulls back to profile_low or just below it
level is touched or swept
price closes back below the level
next candle confirms acceptance below the level
confirmation-window delta < 0
```

### 3. Pre-NY Structural Reclaim/Rejection

Use when price returns to a pre-NY level after ORB bias is known.

Long:

```text
price sweeps below or into structural level
price reclaims the level
confirmation candle closes above the level
confirmation-window delta > 0
```

Short:

```text
price sweeps above or into structural level
price rejects the level
confirmation candle closes below the level
confirmation-window delta < 0
```

One candle reclaim is not enough. If candle structure says reclaim but delta disagrees, skip.

## Order-Flow Gate

Order flow is a gate, not a separate signal.

Compute confirmation-window delta from already-closed 1-minute candles:

```text
delta = sum(buy_volume - sell_volume)
buy_pct = sum(buy_volume) / sum(volume)
```

Rules:

```text
long setup requires delta > 0
short setup requires delta < 0
```

Interpretation:

```text
candle reclaim + confirming delta = aggressive flow supports the reclaim
candle reclaim + opposite/flat delta = weak reclaim or absorption risk
candle rejection + confirming delta = aggressive flow supports rejection
candle rejection + opposite/flat delta = weak rejection or trap risk
```

This improves SL quality by avoiding tight structural stops behind weak reclaim attempts.

## Stop Logic

Stops must be known before entry.

Use the closest level that invalidates the setup:

```text
long retest/reclaim: SL below sweep/retest low plus buffer
short retest/rejection: SL above sweep/retest high plus buffer
prominent breakout: SL beyond last failed-breakout / failed-breakdown structure
```

Do not define risk from future MFE/MAE.

The exact buffer is not frozen. It must be tested separately.

## Take Profit And Runner

For this V2 checkpoint:

```text
TP1 = 4R
TP1 closes 50%
no protection at 1R
runner trail activates only after TP1
runner trail distance = 1R
no fixed TP2
no candle-close climax exit
```

Runner trailing:

```text
long: runner_stop = max(previous_runner_stop, best_raw_trade_price_since_TP1 - 1R)
short: runner_stop = min(previous_runner_stop, best_raw_trade_price_since_TP1 + 1R)
```

If price follows the bias, the trail moves. If price moves against the bias, the trail does not move.

Runner exits only when raw aggTrade price crosses the trail.

## May 30 And May 31 Validation

Test artifact:

```text
models/orb/runs/20260629_025_orderflow_filtered_retest_logic_may30_may31/outputs/findings.md
```

Rules tested:

```text
raw aggTrade execution
no fees
no slippage
TP1 = 4R
50% exit at TP1
runner trail = 1R after TP1
order-flow delta gate
```

Result:

```text
May 30: 2 trades, 2 wins, +7.47R
May 31: 2 trades, 2 wins, +7.93R
Total: 4 trades, 4 wins, +15.40R
Average: +3.85R
```

Important filtered setup:

```text
May 30 22:46 long reclaim of 8.941 was skipped.
Reason: candle reclaimed the level, but confirmation-window delta was -4.11 and buy volume was only 49.72%.
Interpretation: the reclaim lacked aggressive buyer support.
```

## Not Frozen Yet

Do not treat these as final:

```text
exact flow-window length
exact TP1 = 4R
exact SL buffer
fee/slippage-adjusted expectancy
out-of-sample performance
```

Next useful tests:

```text
compare TP1 = 3R / 4R / 6R
compare delta > 0 versus buy_pct > 55% / 60%
test buffer sizes around structural SL
run across a larger sample before calling it an edge
```
