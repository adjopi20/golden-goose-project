# ORB Setup Logic V2 - Aggregated Quality + Individual Deep-Trade Checkpoint

Status: frozen research checkpoint. Promising, not a proven edge.

This file supersedes `setup_logic_v2_retest_orderflow.md` and `setup_logic_v2_dual_entry_models.md` for new ORB research iterations. Keep the older files as historical checkpoints.

Validated checkpoint window so far:

```text
AVAXUSDC May 28, May 29, May 30, and May 31 2026 NY sessions
run: models/orb/runs/20260629_033_aggregated_logic_may28_may31_deep_threshold_compare/
result: 9 trades, 8 wins, +26.90R before fees/slippage
```

Do not call this a final edge until it survives larger-sample, fee/slippage-adjusted, out-of-sample testing.

## Core Principle

Bias is not entry.

The ORB breakout establishes directional bias. Entry requires price to return to, reject, reclaim, or accept around a known level with enough quality.

The logic now has two entry models:

```text
trend following: trade continuation in the ORB bias direction
mean reversion: fade an opposite-side structural sweep back toward prior-24h value, still in the ORB bias direction
```

Mean reversion does not flip the original ORB bias. It is a different entry model, not a new bias.

## Data And Time Rules

- Anchor sessions to `09:30 NY`.
- Report timestamps in WIB.
- Candle/setup evaluation uses 1-minute bars only.
- Confirmation-window delta uses already-closed 1-minute candles only.
- Raw aggTrade prices execute entry, stop, TP1, fixed TP2, and trailing.
- No nanosecond/millisecond bars are used for setup logic.
- No future candles, future profile values, or future trade prints may be used in setup evaluation.

## Pre-Session Levels

Build these before the current NY session:

```text
previous NY high/low: 09:30-17:30 NY
overnight high/low: 17:30-01:30 NY
pre-NY high/low: 01:30-09:30 NY
prior-24h POC/VAH/VAL: previous 09:30 NY to current 09:30 NY
```

These are the structural levels watched for continuation, rejection, reclaim, and mean-reversion entries.

## Current ORB Profile

After `09:30-09:45 NY`, build the current ORB profile:

```text
profile_low
VAL
POC
VAH
profile_high
```

The ORB profile gives the first directional context and the main trend-following retest levels.

## Setup Evaluation Workflow

Use this order every time:

```text
1. Build pre-session structural levels.
2. Build current ORB profile.
3. Determine ORB directional bias from breakout/acceptance.
4. Watch ORB profile levels and pre-session structural levels.
5. Generate a candidate only after closed 1-minute confirmation.
6. Check candle quality.
7. Check order-flow interpretation.
8. Check individual deep-trade bubble evidence.
9. Enter only if the full setup is coherent.
```

Keep scanning after every trade outcome. A TP1, TP2, runner exit, or stopped trade does not end the session review.

Active observation for new NY-session setup discovery continues until `04:00 WIB` unless a specific test defines a different cutoff. The observer must keep watching for new structural sweeps, reclaims, rejections, and absorption continuations after earlier winners.

`04:00 WIB` is only the cutoff for finding new setups. Do not force-close an already-open trade at `04:00 WIB`. If a trade entered before the cutoff is still open, keep managing it with its existing SL, TP1, fixed TP2, or trailing rules until it closes naturally.

## Candle Quality Gate

Reject candidates with weak candle quality even when raw delta agrees.

Check:

```text
displacement
body strength
close location
net progress through the watched level
acceptance/rejection after the level interaction
absence of small alternating chop
whether aggressive flow moved price or got absorbed
```

Important rejection rule:

```text
same-side delta + no price progress = likely absorption, not confirmation
```

Example: May 28 22:00 long was rejected even though two-bar delta was `+600.77`, because price stalled, the 22:00 candle itself had negative delta, and p95 bubbles were active both ways without acceptance higher.

## Order-Flow Interpretation

Compute confirmation-window delta from already-closed 1-minute candles:

```text
delta = sum(buy_volume - sell_volume)
buy_pct = sum(buy_volume) / sum(volume)
```

There are two valid flow modes.

### Same-Side Confirmation

Long:

```text
delta > 0
price accepts/reclaims higher
deep-trade evidence is supportive or not contradictory
```

Short:

```text
delta < 0
price accepts/rejects lower
deep-trade evidence is supportive or not contradictory
```

### Counter-Delta Absorption

Long:

```text
delta < 0
large/aggressive sells hit the market
price refuses to move lower
price reclaims/accepts higher after the absorption
```

Short is mirrored:

```text
delta > 0
large/aggressive buys hit the market
price refuses to move higher
price rejects/accepts lower after the absorption
```

Example: May 28 23:13 long. The evaluated window was 23:06-23:12, not only the 23:12 candle. Sellers were aggressive, but price stair-stepped higher and closed near the high. This made the trade a counter-delta absorption continuation.

## Individual Deep-Trade Bubble Layer

Deep-trade bubbles are an evidence layer, not a standalone trigger.

Build individual order bubbles from raw aggTrades using thresholds calculated from the prior 24h ending at current NY open.

Default research threshold:

```text
p95 qty or p95 notional
```

Comparison threshold:

```text
p99 qty or p99 notional
```

Current interpretation:

```text
p95 = better for seeing individual reloads and repeated large participation, but noisier
p99 = cleaner when it appears, but too sparse for this sample
```

Do not cluster bubbles for this checkpoint. Keep each bubble individually visible so repeated large prints at a price level can be inspected.

Deep-trade read:

```text
supportive: bubbles agree with candle acceptance/rejection
supportive absorption: bubbles hit one side but price accepts the other way
neutral: no threshold bubbles or mixed evidence
rejecting/misleading: bubbles appear but price does not accept in that direction
```

Rule:

```text
bubbles without candle acceptance do not justify entry
```

## Trend-Following Entries

Valid trend-following setup types:

```text
prominent ORB breakout
ORB profile-high/profile-low retest
pre-session structural reclaim/rejection
structural pullback continuation
counter-delta absorption continuation
```

Trend-following exits:

```text
TP1 = 4R
close 50% at TP1
no protection at 1R
runner trail activates only after TP1
runner trails by 1R behind best raw aggTrade price since TP1
no fixed TP2
no candle-close climax exit
```

Runner trailing:

```text
long: runner_stop = max(previous_runner_stop, best_raw_trade_price_since_TP1 - 1R)
short: runner_stop = min(previous_runner_stop, best_raw_trade_price_since_TP1 + 1R)
```

## Mean-Reversion Entries

Mean reversion uses the ORB bias direction, but fades an opposite-side sweep into pre-session structural levels.

Short mean reversion:

```text
ORB bias is short
price sweeps prior highs
price fails to accept above those highs
price rejects back below the watched structural level
closed-candle order flow supports short continuation or shows buy absorption
```

Long mean reversion is mirrored around prior lows.

Mean-reversion exits for this checkpoint:

```text
TP1 = prior-24h POC, close 50%
after TP1, protect remaining 50% by moving SL to entry
TP2 = prior-24h VAL for shorts
TP2 = prior-24h VAH for longs
```

For now, use fixed TP2 for mean reversion. Do not use the mean-reversion trailing variant unless running an explicit comparison.

## Stop Logic

Stops must be known before entry.

Use the closest structure that invalidates the specific setup:

```text
long reclaim/retest: below sweep/retest/absorption low plus buffer
short rejection/retest: above sweep/retest/rejection high plus buffer
prominent breakout: beyond the failed-breakout/failed-breakdown structure
mean reversion: beyond the swept structural extreme plus buffer
```

SL refinement is not the priority yet. Entry quality is the priority.

## Latest Validation

Artifact:

```text
models/orb/runs/20260629_033_aggregated_logic_may28_may31_deep_threshold_compare/outputs/comparison_findings.md
```

Result:

```text
p95: 9 trades, 8 wins, +26.90R
p99: 9 trades, 8 wins, +26.90R
```

Per day:

```text
May 28: 2 trades, 1 win, +2.56R
May 29: 3 trades, 3 wins, +8.94R
May 30: 2 trades, 2 wins, +7.47R
May 31: 2 trades, 2 wins, +7.93R
```

The updated quality/deep-trade layer changed May 28 by rejecting the weak 22:00 long and accepting the 23:13 counter-delta absorption continuation. It did not break the May 29, May 30, or May 31 replayed setups.

Important caveat:

```text
This validation was a deterministic replay of the aggregated accepted setup list, not a fully automatic scanner that rediscovered every setup from raw data.
```

## Next Research Workflow

For each new individual session:

```text
1. Create a new isolated run folder under models/orb/runs/.
2. Build pre-session levels and current ORB profile.
3. Walk the session candle by candle as if live until 04:00 WIB for new setup discovery.
4. Keep scanning after each trade exit; do not assume the session is done after a win.
5. If a trade entered before 04:00 WIB remains open, keep managing it after 04:00 until natural SL/TP/trailing exit.
6. Record accepted setups and rejected reference setups.
7. Replay accepted setups with raw aggTrade execution.
8. Export quality evaluation, deep-trade bubbles, trades, attempted setups, summary, and findings.
9. Compare p95 and p99 individual bubbles, but keep p95 as the default evidence layer unless later tests prove otherwise.
10. Do not overwrite prior runs.
```

Next robustness tests:

```text
more individual sessions
larger contiguous sample
TP1 3R vs 4R vs 6R
delta sign versus buy_pct thresholds
SL buffer variants
fees and slippage
eventual automatic scanner after rules stop changing
```
