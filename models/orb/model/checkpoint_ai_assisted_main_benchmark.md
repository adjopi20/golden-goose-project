# AI-Assisted Main ORB Benchmark

Status: active research benchmark, not a proven edge.

Use this as the main model before testing variants.

```text
method = strictBias_breakout_bothBias_meanReversion
decision workflow = AI-assisted frozen-logic session walk
execution workflow = deterministic raw aggTrade replay
mechanical scanner = not the strategy, unless explicitly testing scanner automation
```

## Parameters

Trend-following:

```text
TP1 = 4R
close 50% at TP1
no pre-TP protection
runner trail activates only after TP1
runner trail distance = 50% of entry-to-TP1 distance
no forced 04:00 close
max hold = force exit at first raw aggTrade at/after next overnight end
```

Mean-reversion:

```text
both-bias structural MR allowed
TP1 = prior-24h POC
TP2 = prior-24h VA target
no forced 04:00 close
max hold = force exit at first raw aggTrade at/after next overnight end
```

Portfolio assumption:

```text
initial_equity = 1000 USD
risk_per_trade = 5% of current equity
compounding = enabled
fee = 0.0400% per fill
slippage = 0.0500% per fill
next overnight end = PRE_NY_START_TIME / 01:30 New York time
```

## Workflow

AI agent:

```text
reads frozen logic
walks session candle-by-candle
labels setup: take / reject / wait
writes reason, levels, delta, bubbles, invalidation
```

Deterministic engine:

```text
validates no-lookahead
executes entry/SL/TP/trailing on raw aggTrade
applies fee/slippage
computes PnL
blocks invalid trades
```

Paper-trading/research log must include:

```text
decision_time
known levels
bias
candidate setup
reason to take/reject
entry
SL
TP logic
delta/bubble evidence
what would invalidate the trade
```

## Current Replay

Artifact:

```text
models/orb/runs/20260630_051_ai_assisted_main_benchmark_may25_may31_5pct/
```

Scope:

```text
AI/hand-walked accepted setup sample only
May 25-May 31 2026
```

Result:

| Metric | Value |
|---|---:|
| Initial equity | $1,000.00 |
| Final equity | $2,293.74 |
| Profit | $1,293.74 |
| Total return | +129.37% |
| Max drawdown | 24.66% |
| Trades | 27 |
| Net winners | 14 |
| Win rate | 51.85% |
| Gross R | +39.23R |
| Cost R | -18.56R |
| Net R | +20.67R |
| Avg net R | +0.77R |

Important limitation:

```text
This is not a May 1-May 31 AI-walk result.
May 1-May 24 still need session-by-session AI-assisted labeling before they can be included honestly.
```
