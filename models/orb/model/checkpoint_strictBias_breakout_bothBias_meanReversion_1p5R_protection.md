# strictBias_breakout_bothBias_meanReversion_1p5R_protection

Status: frozen research checkpoint. Isolated third variant.

This checkpoint preserves:

```text
strict ORB-bias trend-following
both-bias structural mean-reversion
trend TP1 = 3R
pre-TP1 protection at +1.5R
tighter post-TP1 runner trail
```

Do not merge this into `strictBias_breakout_bothBias_meanReversion`. The older checkpoint remains the stronger May25-May31 result after fees/slippage.

## Identity

```text
checkpoint_name: strictBias_breakout_bothBias_meanReversion_1p5R_protection
scope_tested: AVAXUSDC May 25-31 2026 NY sessions
source_run: models/orb/runs/20260630_044_may25_may31_corrected_exit_protection_variants/
comparison_run: models/orb/runs/20260630_045_may25_may31_old_vs_corrected_exit_compare/
```

## Entry Model

Trend-following keeps strict ORB bias.

Mean-reversion can trade either direction at structural extremes if the setup passes the refined both-bias MR gates:

```text
pre-session structural level interaction
closed 1m candle confirmation
delta confirmation
p99 individual bubble evidence
prior-24h POC/VAL/VAH target geometry
```

## Exits

Trend-following:

```text
TP1 = 3R, close 50%
pre-TP1 protection activates only after price reaches +1.5R
pre-TP1 protection moves stop to entry
runner trail activates only after TP1
runner trail distance = 30% of entry-to-TP1 distance
no fixed TP2
raw aggTrade prices execute TP/SL/trail
```

Mean-reversion:

```text
TP1 = prior-24h POC, close 50%
after TP1, remaining 50% protected at entry
TP2 = prior-24h VAL for shorts
TP2 = prior-24h VAH for longs
```

## Cost Assumptions

```text
USDC taker fee = 0.0400% per fill
slippage = 0.0500% per fill
total drag = 0.0900% per fill
```

## May25-May31 Result

| Trades | Gross Wins | Net Wins | Gross R | Cost Drag R | Net R | Avg Net R |
|---:|---:|---:|---:|---:|---:|---:|
| 28 | 17 | 15 | +34.63 | 17.19 | +17.44 | +0.62 |

## Comparison Against Older Both-Bias Checkpoint

| Method | Trades | Net R | Avg Net R |
|---|---:|---:|---:|
| strictBias_breakout_bothBias_meanReversion | 27 | +22.09 | +0.82 |
| strictBias_breakout_bothBias_meanReversion_1p5R_protection | 28 | +17.44 | +0.62 |

The older 4R/no-preTP-protection exit remains ahead on May25-May31. This checkpoint is frozen for continued comparison, not promoted as the preferred method.

## Known Weakness

This variant reduces some open risk through +1.5R protection, but it gives up continuation capture versus the old 4R trend exit.

Main observed issue:

```text
tighter TP1/trail improves protection but lowers total R on May25-May31
both-bias MR still adds churn on May29 and May31
```

## Usage Rule

Use this checkpoint when the test objective is:

```text
strict trend-following bias
both-bias structural mean-reversion
trend TP1 at 3R
pre-TP1 protection at +1.5R
tighter 70:30 runner trail
fee/slippage-aware comparison
```

Do not use this checkpoint as evidence that 1.5R protection is superior until it survives more sessions and Monte Carlo / robustness analysis.
