# strictBias_breakout_meanReversion

Status: frozen research checkpoint. Isolated from `strictBias_breakout_bothBias_meanReversion`.

This checkpoint preserves the strict-bias method. Use this as the fallback/reference model if later both-bias mean-reversion variants fail on larger samples.

Do not merge rules from the both-bias checkpoint into this file.

## Identity

```text
checkpoint_name: strictBias_breakout_meanReversion
source_logic: setup_logic_v2_aggregated_quality_deep_trade.md
scope_tested: AVAXUSDC May 26-31 2026 NY sessions
primary_run: models/orb/runs/20260630_036_latest_logic_may26_may31_consolidated/
fee_slippage_run: models/orb/runs/20260630_040_fee_slippage_may26_may31/
```

## Core Rule

ORB breakout establishes directional bias.

Trend-following and mean-reversion both respect that original ORB bias.

Mean reversion does not flip direction. It is still a bias-aligned trade model that fades an opposite-side structural sweep back toward prior-24h value.

```text
trend following: continuation in ORB bias direction
mean reversion: structural sweep/reclaim/rejection, still in ORB bias direction
```

## Required Levels

Build before evaluating entries:

```text
previous NY high/low: 09:30-17:30 NY
overnight high/low: 17:30-01:30 NY
pre-NY high/low: 01:30-09:30 NY
prior-24h POC/VAH/VAL: previous 09:30 NY to current 09:30 NY
current ORB profile: 09:30-09:45 NY
```

## Entry Quality

Entry requires:

```text
closed 1m confirmation
candle quality
price acceptance/rejection around watched level
order-flow confirmation or counter-delta absorption
individual deep-trade bubble evidence as context only
```

Deep-trade bubbles are not standalone triggers.

## Exits

Trend-following:

```text
TP1 = 4R, close 50%
no 1R protection
runner trail activates only after TP1
runner trails by 1R behind best raw aggTrade price since TP1
no fixed TP2
```

Mean-reversion:

```text
TP1 = prior-24h POC, close 50%
after TP1, remaining 50% protected at entry
TP2 = prior-24h VAL for shorts
TP2 = prior-24h VAH for longs
```

## Observation Window

New setup discovery continues until `04:00 WIB`.

Open trades are not force-closed at `04:00 WIB`; they are managed naturally to SL, TP, or trail.

## May 26-31 Result

Gross result:

| Trades | Wins | Total R | Avg R |
|---:|---:|---:|---:|
| 14 | 11 | +28.59 | +2.04 |

Fee/slippage-adjusted result:

Assumptions:

```text
USDC taker fee = 0.0400% per fill
slippage = 0.0500% per fill
total drag = 0.0900% per fill
```

| Trades | Gross R | Cost R | Net R | Net Avg R |
|---:|---:|---:|---:|---:|
| 14 | +28.59 | -10.91 | +17.68 | +1.26 |

## Trade List

Source:

```text
models/orb/runs/20260630_036_latest_logic_may26_may31_consolidated/outputs/findings.md
```

This checkpoint intentionally has fewer trades than the both-bias variant. Its main advantage is cleaner average R and less churn.

## Usage Rule

Use this checkpoint when the test objective is:

```text
strict ORB bias
trend-following plus bias-aligned mean-reversion
lower trade count
fallback/reference behavior
```

Do not add counter-bias MR trades from the combined variant here.
