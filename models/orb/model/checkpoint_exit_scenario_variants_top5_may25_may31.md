# ORB Exit Scenario Variants Top 5 - May25-May31

Status: frozen research comparison set. Not a proven edge.

This file freezes the five best deterministic compounded exit scenarios from the May25-May31 2026 AVAXUSDC research pass.

Source run:

```text
models/orb/runs/20260630_048_may25_may31_deterministic_compounded_paths/
```

Simulation basis:

```text
initial_equity = $1,000
risk_per_trade = 10% of current equity
pnl_per_trade = current_equity * 10% * net_R
sequence = actual chronological May25-May31 trades for each scenario
fees/slippage = included in net_R
fee = 0.0400% per fill
slippage = 0.0500% per fill
```

No randomization was used for this freeze.

## Top 5 Scenario Ranking

| Rank | Scenario Variant | Method Family | Trend TP1 | Pre-TP Protection | Runner Trail | Trades | End Equity | Profit | Max Drawdown | Note |
|---:|---|---|---:|---|---|---:|---:|---:|---:|---|
| 1 | `bothBiasMR_TP1_4R_protect_1p5R_trail30` | strict trend + both-bias MR | 4R | move stop to entry at +1.5R | 30% of entry-to-TP1 distance after TP1 | 26 | $7,131.93 | +$6,131.93 | 43.4% | best ending equity |
| 2 | `bothBiasMR_TP1_5R_protect_1p5R_trail30` | strict trend + both-bias MR | 5R | move stop to entry at +1.5R | 30% of entry-to-TP1 distance after TP1 | 25 | $4,999.32 | +$3,999.32 | 43.4% | higher TP1, lower realized return than 4R |
| 3 | `bothBiasMR_TP1_4R_noPreTP_trail1R_old` | strict trend + both-bias MR | 4R | none | old 1R trail after TP1 | 27 | $4,203.36 | +$3,203.36 | 43.4% | old combined checkpoint behavior |
| 4 | `strict_TP1_4R_noPreTP_trail1R_old` | strict bias only | 4R | none | old 1R trail after TP1 | 15 | $3,581.27 | +$2,581.27 | 38.8% | cleanest lower-drawdown top path |
| 5 | `bothBiasMR_TP1_3R_trail30_family` | strict trend + both-bias MR | 3R | none / +1R / +1.5R / +2R all tied in this sample | 30% of entry-to-TP1 distance after TP1 | 28 | $3,071.70 | +$2,071.70 | 50.0% | tied realized result across tested 3R protection levels |

## Variant Definitions

### `bothBiasMR_TP1_4R_protect_1p5R_trail30`

Entry logic:

```text
Trend-following keeps strict ORB bias.
Mean-reversion can trade both directions at structural extremes.
MR requires structural interaction, closed 1m confirmation, delta, p99 bubble evidence, and prior-24h profile geometry.
```

Trend exits:

```text
TP1 = 4R, close 50%
before TP1, protect at entry once price reaches +1.5R
after TP1, trail remaining 50% by 30% of entry-to-TP1 distance
```

Research read:

```text
Best ending equity in the deterministic May25-May31 compounded path.
Higher return than old combined path in this specific deterministic sequence.
Still carries 43.4% realized max drawdown at 10% risk per trade.
```

### `bothBiasMR_TP1_5R_protect_1p5R_trail30`

Trend exits:

```text
TP1 = 5R, close 50%
before TP1, protect at entry once price reaches +1.5R
after TP1, trail remaining 50% by 30% of entry-to-TP1 distance
```

Research read:

```text
Second-best ending equity.
Looser TP1 than 4R did not improve realized May25-May31 return.
Keep as an upside-continuation comparison variant.
```

### `bothBiasMR_TP1_4R_noPreTP_trail1R_old`

Trend exits:

```text
TP1 = 4R, close 50%
no pre-TP1 protection
after TP1, trail remaining 50% by 1R behind best raw aggTrade price
```

Research read:

```text
This is the old combined checkpoint behavior.
It remains strong, but in the deterministic compounded path it ranked below 4R + 1.5R protection + trail30.
```

### `strict_TP1_4R_noPreTP_trail1R_old`

Entry logic:

```text
Trend-following and mean-reversion both respect strict ORB bias.
No both-direction MR expansion.
```

Trend exits:

```text
TP1 = 4R, close 50%
no pre-TP1 protection
after TP1, trail remaining 50% by 1R behind best raw aggTrade price
```

Research read:

```text
Best lower-drawdown top path in the deterministic compounded ranking.
Lower ending equity than bothBiasMR variants, but cleaner path and fewer trades.
```

### `bothBiasMR_TP1_3R_trail30_family`

Trend exits:

```text
TP1 = 3R, close 50%
after TP1, trail remaining 50% by 30% of entry-to-TP1 distance
tested pre-TP protection levels: none, +1R, +1.5R, +2R
```

Research read:

```text
All tested 3R protection levels produced the same realized May25-May31 ending equity in this sequence.
The family ranked below 4R and 5R variants.
Do not treat 3R as safer by default; realized max drawdown was worse at 50.0%.
```

## Current Research Takeaway

The current optimum zone from this deterministic path is still around:

```text
Trend TP1 = 4R
pre-TP protection = +1.5R if using the bothBiasMR family
post-TP runner trail = 30% of entry-to-TP1 distance
```

But the cleaner conservative comparison remains:

```text
strict_TP1_4R_noPreTP_trail1R_old
```

## Usage Rule

When future tests compare exit parameters, include these five variants as named scenarios.

Do not overwrite the older isolated method checkpoints:

```text
strictBias_breakout_meanReversion
strictBias_breakout_bothBias_meanReversion
strictBias_breakout_bothBias_meanReversion_1p5R_protection
```

This top-5 file is a comparison catalog, not a replacement for those checkpoints.
