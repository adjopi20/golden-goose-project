# ORB 15m Breakout Distribution Research

Status: approved design, not yet executed.

Purpose: measure the statistical distribution of expansion after a New York
first-15-minute ORB profile breakout, then identify setup features that precede
strong expansion.

This is an event study, not a trading backtest. Do not add entries, stops,
targets, position sizing, compounding, or AI judgement in this research phase.

## Source Windows

All times are New York local time.

```text
ORB profile window:
09:30 <= time < 09:45

Setup observation window:
09:45 <= time <= 10:15

Expansion horizon:
breakout timestamp -> first touch of opposite-side ORB profile extreme
or 04:30 next NY day, whichever comes first
```

The ORB profile is fixed. Build it once after the 09:30-09:45 window closes.
Do not use a rolling ORB profile.

## Frozen ORB Profile

Build a volume profile from raw aggTrades inside:

```text
09:30 <= trade time < 09:45
```

The frozen profile must include:

```text
profile_high
profile_low
poc_price
val
vah
profile_width = profile_high - profile_low
total_volume
buy_volume
sell_volume
delta
```

For breakout sample definitions:

```text
long same-side level = profile_high
long opposite-side level = profile_low

short same-side level = profile_low
short opposite-side level = profile_high
```

## Sample Definition

A session can produce at most one long sample and at most one short sample,
subject to the opposite-side exclusion rule.

Long sample:

```text
During 09:45-10:15:
1. a closed 1m candle breaks/closes above frozen ORB profile_high
2. price has not touched frozen ORB profile_low after 09:45 before that breakout
```

Short sample:

```text
During 09:45-10:15:
1. a closed 1m candle breaks/closes below frozen ORB profile_low
2. price has not touched frozen ORB profile_high after 09:45 before that breakout
```

Use closed 1m candles to define the breakout event. Use raw aggTrades after the
event to measure exact expansion and invalidation.

## Expansion Outcome

For a long sample:

```text
breakout_level = frozen ORB profile_high
invalidation_level = frozen ORB profile_low
same_side_reclaim = first raw aggTrade price <= breakout_level after breakout
opposite_orb_invalidation = first raw aggTrade price <= invalidation_level after breakout
max_expansion_price = highest raw aggTrade price before opposite_orb_invalidation or 04:30
```

For a short sample:

```text
breakout_level = frozen ORB profile_low
invalidation_level = frozen ORB profile_high
same_side_reclaim = first raw aggTrade price >= breakout_level after breakout
opposite_orb_invalidation = first raw aggTrade price >= invalidation_level after breakout
max_expansion_price = lowest raw aggTrade price before opposite_orb_invalidation or 04:30
```

Record:

```text
breakout_time
breakout_candle_open/high/low/close
breakout_candle_delta
breakout_candle_volume
same_side_reclaim_time
end_time
end_reason = opposite_orb_invalidation | time_invalidation_0430
max_expansion_time
max_expansion_price
max_expansion_abs
max_expansion_pct
max_expansion_orb_width_multiple
time_to_max_expansion_seconds
time_to_same_side_reclaim_seconds
time_to_opposite_orb_invalidation_seconds
```

## Frozen P95 Order Bubbles

Use quantity only. Do not use notional.

Build the p95 threshold once per NY session from prior 24h individual aggTrade
quantities:

```text
09:30 previous NY day <= trade time < 09:30 current NY day
```

Then freeze it for the current NY session:

```text
p95_qty = 95th percentile of qty in prior-24h source window
bubble = current aggTrade qty >= p95_qty
```

Do not use rolling p95. Do not compute p95 inside each minute.

For each breakout sample, record qty-only bubble features from known data only:

```text
p95_qty_threshold
pre_breakout_p95_bubble_count
pre_breakout_buy_bubble_count
pre_breakout_sell_bubble_count
breakout_candle_p95_bubble_count
breakout_candle_buy_bubble_count
breakout_candle_sell_bubble_count
largest_pre_breakout_bubble_qty
largest_pre_breakout_bubble_side
largest_breakout_candle_bubble_qty
largest_breakout_candle_bubble_side
largest_bubble_distance_to_breakout_level
```

Aggressive side follows Binance aggTrade semantics:

```text
is_buyer_maker = false -> aggressive buy
is_buyer_maker = true  -> aggressive sell
```

## Setup Features

Compute all setup features using only data available at or before the breakout.

ORB profile features:

```text
profile_width
profile_total_volume
profile_delta
profile_buy_volume
profile_sell_volume
poc_position_inside_profile
value_area_width
breakout_level_distance_from_poc
```

Breakout candle features:

```text
body_to_range
close_position_in_range
delta
volume
buy_volume
sell_volume
buy_volume_pct
trade_count
largest_trade_qty
largest_trade_side
upper_wick
lower_wick
```

Pre-breakout window features:

```text
09:45_to_breakout_net_delta
09:45_to_breakout_volume
09:45_to_breakout_buy_volume_pct
last_5m_net_delta
last_5m_volume
last_5m_range
last_15m_net_delta
last_15m_volume
last_15m_range
time_from_orb_end_to_breakout_seconds
```

Context features:

```text
breakout_against_previous_24h_poc
distance_to_previous_24h_poc
distance_to_previous_24h_val
distance_to_previous_24h_vah
distance_to_pre_ny_high
distance_to_pre_ny_low
distance_to_overnight_high
distance_to_overnight_low
distance_to_previous_ny_high
distance_to_previous_ny_low
```

Previous-24h profile is also frozen at NY open. Do not use rolling volume
profile values.

## Distribution Analysis

Primary grouping metric:

```text
max_expansion_orb_width_multiple
```

Suggested buckets:

```text
failed:      < 0.5x ORB width
weak:        0.5x to < 1.0x
decent:      1.0x to < 2.0x
strong:      2.0x to < 4.0x
exceptional: >= 4.0x
```

Compare setup-feature distributions across buckets:

```text
median
mean
p25 / p75
win-rate above each expansion threshold
sample count
```

Thresholds to report:

```text
expansion >= 0.5x ORB width
expansion >= 1.0x ORB width
expansion >= 2.0x ORB width
expansion >= 4.0x ORB width
```

## Required Outputs

Default output location:

```text
models/orb/runs/<run_id>_orb_15m_breakout_distribution/
```

Default artifacts:

```text
methodology.md
findings.md
sample_table.parquet
feature_table.parquet
distribution_summary.md
pattern_candidates.md
```

Do not write CSV unless explicitly requested.

## No-Lookahead Rules

```text
ORB profile is known only after 09:45.
Prior-24h p95_qty is frozen at 09:30.
Previous-24h volume profile is frozen at 09:30.
Breakout event uses closed 1m candle during 09:45-10:15.
Expansion measurement can use future raw trades only after the sample is fixed.
Setup features cannot use data after breakout.
```
