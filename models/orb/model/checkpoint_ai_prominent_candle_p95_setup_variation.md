# ai_prominent_candle_p95_setup_variation

Status: frozen setup-evaluation variation, not a proven edge.

This checkpoint preserves the latest AI-assisted setup evaluation update from the May20-May21 review.

It is a setup-evaluation variation, not a parameter variation.

## Identity

```text
checkpoint_name: ai_prominent_candle_p95_setup_variation
base_method: strictBias_breakout_bothBias_meanReversion
decision_workflow: AI-assisted candle-by-candle session walk
execution_workflow: deterministic raw aggTrade replay
latest_validation_run: models/orb/runs/20260630_054_may20_may21_updated_rationale_no_csv/
```

## What Changed

The previous AI-assisted benchmark was too strict about reward geometry when a dominant candle had already changed the session structure.

This variation adds a must-enter class:

```text
prominent candle / armed-breakout setup
```

This class applies when a closed 1m candle materially changes structure or profile context and is prominent relative to the active observation window.

## Required Levels

Build before evaluating entries:

```text
previous NY high/low: 09:30-17:30 NY
overnight high/low: 17:30-01:30 NY
pre-NY high/low: 01:30-09:30 NY
prior-24h POC/VAH/VAL: previous 09:30 NY to current 09:30 NY
current ORB profile: 09:30-09:45 NY
```

Watched levels:

```text
ORB profile high/low/POC
previous NY high/low
overnight high/low
pre-NY high/low
prior-24h POC/VAH/VAL
```

## Candle-by-Candle Evaluation

After ORB formation, observe each closed 1m candle until `04:00 WIB`.

For every candle, record:

```text
OHLC position
body size
body-to-range strength
close location
delta
buy percentage
p95 individual bubbles
interaction with watched levels
```

Put extra focus on candles that touch, pierce, close beyond, reclaim, or reject watched levels.

## Prominent Candle Rule

A candle becomes prominent when it is clearly stronger than the active comparison set.

Comparison set:

```text
from NY open to the candidate candle
or
from the most recent structural touch/sweep to the candidate candle
```

Prominence evidence:

```text
body is among the largest in the comparison window
body closes near the directional extreme
range expands through or away from a watched level
OHLC starts making directional progress
delta supports the move or counter-delta is absorbed
p95 bubbles show aggression, reload, or absorption near the relevant level
```

If this candle arms a breakout/reclaim and the following candle does not meaningfully reject it, the setup is must-enter unless invalidated by obvious chop or immediate structural failure.

## Order Flow

Use already-closed 1m candles only.

```text
long continuation: confirmation-window delta should be positive
short continuation: confirmation-window delta should be negative
counter-delta is valid only when price progress proves absorption
```

Delta is:

```text
sum(buy_volume - sell_volume)
```

## Smart-Money Bubble Standard

Use individual p95 bubbles as the default smart-money marker.

Do not require p99 for this variation.

Use bubbles individually, not only clustered:

```text
aggressive buy bubble into breakout = support for long
aggressive sell bubble into breakdown = support for short
opposite-side bubble that fails to move price = absorption evidence
repeated bubbles near one level = possible protection/reload level
```

Deep-trade bubbles are evidence, not standalone triggers.

## Stop Logic

Initial SL is placed at the closest level that invalidates the setup.

For prominent-candle setups, refine the stop using p95 bubbles when available:

```text
long: below the relevant protected buy bubble / absorption shelf
short: above the relevant protected sell bubble / absorption shelf
```

Constraint:

```text
long stop should not be below the prominent candle low unless broader structure requires it
short stop should not be above the prominent candle high unless broader structure requires it
```

The purpose is not to overfit a tiny stop. The stop must still represent real invalidation.

## Entry Timing

Do not enter inside the signal candle.

Entry can occur only after the signal/confirmation evidence is closed.

Examples:

```text
prominent breakout candle closes
next candle shows absorption or no meaningful rejection
entry at the next raw aggTrade after the chosen entry timestamp
```

If the immediate follow-up candle is unresolved, wait.

Example:

```text
May20 second long was cleaner at 23:42 after 23:39-23:40 pullback failed.
Entering at 23:39 was optional and not required.
```

## Existing Models Retained

This variation still keeps:

```text
trend-following continuation
ORB/profile retest and reclaim
pre-session structural reclaim/rejection
prior-session structural reclaim/rejection
both-bias structural mean reversion
```

Mean-reversion remains optional unless the structural evidence is clean.

## Exits

Use the same main benchmark execution parameters unless testing a separate parameter variation:

Trend-following:

```text
TP1 = 4R, close 50%
no pre-TP protection
runner trail activates only after TP1
runner trail distance = 50% of entry-to-TP1 distance
no fixed TP2
```

Mean-reversion:

```text
TP1 = prior-24h POC, close 50%
TP2 = prior-24h VA target
```

## Observation Window

New setup discovery continues until:

```text
04:00 WIB
```

Open trades are not force-closed at `04:00 WIB`.

Open trades are managed naturally until:

```text
SL
TP1 + runner trail
fixed TP2 for mean reversion
```

## Backtest Artifact Preference

For future manual/AI backtests:

```text
write findings in markdown
do not write CSV artifacts unless explicitly requested
```

The deterministic replay can use in-memory setup rows.

## Latest Validation

Artifact:

```text
models/orb/runs/20260630_054_may20_may21_updated_rationale_no_csv/outputs/findings.md
```

Result:

| Scope | Trades | Wins | Gross R | Cost Drag | Net R | Final Equity |
|---|---:|---:|---:|---:|---:|---:|
| May20-May21 2026 | 3 | 3 | +11.36R | -6.96R | +4.40R | $1,233.93 |

Assumptions:

```text
initial equity = $1,000
risk = 5% current equity per trade
fee = 0.0400% per fill
slippage = 0.0500% per fill
```

Accepted examples:

```text
May20 21:19 long: prominent profile breakout, must-enter class
May20 23:42 long: delayed structural reclaim, optional/clean after pullback resolved
May21 22:15 long: prominent ORB-high expansion
```

Rejected example:

```text
May21 00:15 VAH continuation: optional scalp-like continuation, not must-enter class
```

## Usage Rule

Use this checkpoint when the test objective is:

```text
AI-assisted candle-by-candle review
p95 individual bubbles
prominent candle / armed-breakout must-enter class
markdown-only backtest artifacts
```

Do not merge this into older p99 checkpoints until larger sample testing proves it survives.
