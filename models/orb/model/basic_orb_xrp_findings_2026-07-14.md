# Basic ORB XRP Research Findings

Status: observational research, not an approved trading rule or proven executable edge.

## Research Definition

- Instrument: XRPUSDT aggTrades.
- Time zone: America/New_York.
- Opening range: frozen 15-minute range and volume profile.
- Tested ORB starts: 08:30, 09:00, and 09:30.
- First breakout observation windows: 30, 45, and 60 minutes.
- Stop/risk references: opposite range extreme, POC, or opposite value-area boundary.
- Outcome horizon: until the stop reference is invalidated or 04:30 NY.
- Expansion observations: 1R, 2R, 4R, 8R, and greater than 8R before invalidation.
- No fee, slippage, execution, sizing, or portfolio simulation is included.

## Full-Sample Baseline

For 2020-02-01 through 2026-06-30, using the basic 09:30 ORB, a 30-minute breakout window, and the opposite range extreme as risk:

- Sessions: 2,342.
- Breakout samples: 2,131.
- Resolved 1R win rate: 52.29%.
- Gross resolved expectancy: +0.0458R.
- Long: 50.15% resolved win rate, +0.0030R expectancy.
- Short: 54.20% resolved win rate, +0.0841R expectancy.

This is evidence of a small gross statistical tendency, not sufficient evidence of a net edge after trading costs.

## Period Findings

### 2024-11-01 through 2025-01-31

The strongest 1R consistency came from the 08:30 ORB with POC risk.

| Configuration | Samples | 1R | 2R | 4R | 8R | Average MFE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 08:30, 45m, POC | 81 | 61.73% | 39.51% | 18.52% | 8.64% | 2.81R |

Direction split for that configuration:

- Long: 57.14% reached 1R, 14.29% reached 4R, 8.57% reached 8R, average MFE 2.29R.
- Short: 65.22% reached 1R, 21.74% reached 4R, 8.70% reached 8R, average MFE 3.20R.

The strongest long fat-tail pocket was instead the 09:30 ORB with POC risk:

- Long samples: 48.
- 1R: 58.33%.
- 2R: 43.75%.
- 4R: 25.00%.
- 8R: 16.67%.
- Average MFE: 5.12R.

Working interpretation: during this markup/euphoria-like period, earlier 08:30 breakouts offered better short-horizon consistency, while 09:30 long breakouts contained the larger expansion tail.

### 2025-02-01 through 2025-06-30

The strongest family was the 09:00 ORB with POC risk. The 45-minute version produced:

| Configuration | Samples | 1R | 2R | 4R | 8R | Average MFE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 09:00, 45m, POC | 147 | 57.14% | 42.18% | 25.85% | 14.29% | 4.27R |

Its direction split was highly asymmetric:

- Long: 40.30% reached 1R, 25.37% reached 4R, 10.45% reached 8R, -0.194R 1R expectancy.
- Short: 71.25% reached 1R, 26.25% reached 4R, 17.50% reached 8R, +0.425R 1R expectancy, average MFE 5.25R.

Working interpretation: this period behaved like a post-euphoria/markdown environment in which short ORB continuation dominated both consistency and large expansion.

### 2025-08-01 through 2026-03-31

The best tested result was only approximately 53.95% resolved win rate and +0.079R gross expectancy using 09:30, 45 minutes, and POC risk. Results outside the POC family were mostly near random or negative.

Working interpretation: this was a weak or choppy ORB environment. The evidence does not support aggressive ORB deployment in this period.

## Current Pattern Hypothesis

The opening range appears to be a meaningful structural reference, but there is no universal `breakout = trade` rule. ORB behavior appears conditional on:

1. Broad market cycle or regime.
2. Trade direction relative to that regime.
3. ORB formation time.
4. Stop geometry, especially POC versus wider structural stops.
5. Whether the objective is 1R consistency or a 4R/8R expansion tail.

The most useful emerging hypothesis is not that ORB always works. It is that market cycle changes which ORB time and direction contain the favorable expansion distribution.

## What Is Not Proven

- Selecting the best configuration separately inside each historical period is retrospective selection and may be overfit.
- POC stops can be very tight, so fees and slippage may consume a large fraction of one R.
- The observed results are gross and do not establish positive executable expectancy.
- The period labels above are working interpretations, not independently frozen regime labels.
- Results are XRP-specific until replicated on other assets without retuning.

## Required Next Validation

1. Freeze one ORB configuration before looking at the next test period.
2. Attach independently generated, slow market-cycle labels without allowing those labels to alter trades.
3. Compare long and short 1R, 2R, 4R, and 8R reach rates within each regime.
4. Use chronological out-of-sample or walk-forward evaluation.
5. Apply realistic fees and slippage using actual stop distance.
6. Promote a regime relationship into a filter only if it remains stable across multiple periods and preferably multiple assets.

## Source Artifacts

- `models/orb/runs/basic_orb_observation/140726_xrp_1/summary.json`
- `models/orb/runs/basic_orb_observation/140726_xrp_sweep_2_nov24-jan25/`
- `models/orb/runs/basic_orb_observation/140726_xrp_sweep_2_feb25-jun25/`
- `models/orb/runs/basic_orb_observation/140726_xrp_sweep_1_aug25-mar26/`
- `models/orb/scripts/basic_orb_1r_observation.py`
- `models/orb/scripts/basic_orb_1r_sweep.py`

