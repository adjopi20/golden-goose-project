# strictBias_breakout_bothBias_meanReversion

Status: frozen research checkpoint. Isolated from `strictBias_breakout_meanReversion`.

This checkpoint preserves the combined method:

```text
frozen strict-bias trend-following
plus refined both-bias mean-reversion scanner
```

Do not merge this into the strict-bias checkpoint unless larger-sample tests prove it is superior.

## Identity

```text
checkpoint_name: strictBias_breakout_bothBias_meanReversion
scope_tested: AVAXUSDC May 26-31 2026 NY sessions
primary_run: models/orb/runs/20260630_039_refined_mr_rules_may26_may31/
fee_slippage_run: models/orb/runs/20260630_040_fee_slippage_may26_may31/
```

## Core Rule

Trend-following keeps strict ORB bias.

Mean-reversion can trade either direction at structural extremes if price confirms rejection/reclaim with candle quality, delta, and p99 bubble evidence.

```text
trend following: continuation in ORB bias direction only
mean reversion: both directions allowed at pre-session structural extremes
```

Example:

```text
ORB bias is long.
If price accepts above structural high: long continuation can be valid.
If price rejects structural high: short mean-reversion can be valid.
```

Mirror for structural lows.

## Required Levels

Build before evaluating entries:

```text
previous NY high/low: 09:30-17:30 NY
overnight high/low: 17:30-01:30 NY
pre-NY high/low: 01:30-09:30 NY
prior-24h POC/VAH/VAL: previous 09:30 NY to current 09:30 NY
current ORB profile: 09:30-09:45 NY
```

Mean-reversion targets:

```text
TP1 = prior-24h POC
short TP2 = prior-24h VAL
long TP2 = prior-24h VAH
```

## Refined MR Candidate Rules

Candidates are generated from structural high/low extremes only.

Short MR from structural high:

```text
price sweeps watched pre-session high
p99 bubble evidence shows buy absorption or sell participation
two closed 1m candles make lower lows and weak closes
confirmation-window delta is net selling
prior-24h POC is below entry
```

Long MR from structural low:

```text
price sweeps watched pre-session low
p99 bubble evidence shows buy support or sell absorption
two closed 1m candles show acceptance higher
confirmation-window delta is net buying
prior-24h POC is above entry
```

This checkpoint uses p99 bubbles for the refined MR gate.

Deep-trade bubbles are still evidence only. They do not override candle acceptance.

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
| 24 | 15 | +35.71 | +1.49 |

Fee/slippage-adjusted result:

Assumptions:

```text
USDC taker fee = 0.0400% per fill
slippage = 0.0500% per fill
total drag = 0.0900% per fill
```

| Trades | Gross R | Cost R | Net R | Net Avg R |
|---:|---:|---:|---:|---:|
| 24 | +35.71 | -15.19 | +20.52 | +0.86 |

## Known Weakness

This checkpoint increases total R on May 26-31, but it also increases trade count and reduces average R.

Main risk:

```text
repeated same-side MR attempts can create churn
May 31 added several losing long MR attempts before one winner
```

This checkpoint must survive more sessions before replacing the strict-bias checkpoint.

## Usage Rule

Use this checkpoint when the test objective is:

```text
strict trend-following bias
both-bias structural mean-reversion
higher trade count
p99 bubble/delta/candle-quality MR filter
```

Do not use this checkpoint as proof that both-bias MR is final.
