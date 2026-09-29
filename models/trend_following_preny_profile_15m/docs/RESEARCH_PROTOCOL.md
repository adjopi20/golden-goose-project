# Trend-following Pre-NY Profile 15m — research protocol v1

Recorded: 2026-09-27. Owner: user. Status: model-specific research protocol, reusable across datasets for this model, not a project-wide trading protocol.

Scope: trend-following, completed 15-minute candles/delta, Pre-NY volume profile, entry evaluation 09:00–12:00 America/New_York. Keep this model's protocols, contracts and deployment notes under `models/trend_following_preny_profile_15m/`. Only genuinely shared tools (for example backtest, sweep, stress test and Kelly) belong outside model folders. Reusing this protocol for another model requires an explicit copy/adaptation, not silently applying its rules globally.

## Objective and three components

Find the simplest economically plausible hypothesis that survives increasingly hostile attempts to falsify it.

1. **Entry and exit:** what makes a candidate eligible, when orders may be sent, and what ends the position.
2. **Risk and reward:** structural invalidation/stop, target geometry, partial allocation, holding horizon and execution costs.
3. **Position sizing:** how much equity is exposed to that geometry. A sizing signal is not automatically an entry filter.

These interact. Keep two components fixed while first measuring a change to the third. Then verify the chosen combination end-to-end. A narrower stop changes R and stop-out probability; sizing changes money-weighted results, not the underlying probability of a winning trade under a fixed linear fill/cost model.

## Research workflow

Hypothesis → causal observation → shared execution backtest → chronological validation → sizing/exit comparison → stress/Monte Carlo → versioned model contract → shadow/paper trading → explicit live approval → monitoring.

- State the economic mechanism and a way it could fail before inspecting outcomes.
- Record data venue, product, symbol, coverage, timezone, warm-up and missing-data policy. A different quote pair or exchange is not identical input.
- Freeze candidate population, feature timestamps, training boundaries, controls and a small parameter grid. Keep rejected/no-entry sessions visible.
- Use information available at the decision timestamp. No future regime labels, full-sample quantiles, best-in-episode entries or MFE in eligibility.
- Compare an entry selector with the same entry timing, stop, exit, cost and sizing. Report winners/tails removed as well as losers avoided. No replacement candidate after rejection unless separately specified and replayed.
- Prefer chronological held-out blocks with overlapping holding periods purged/embargoed. Repeatedly inspected data is development history, even if a script calls it test. Asset-specific rules are allowed but count as additional selections; do not hide failed experiments.
- Report all tried configurations. Use paired trade/date comparisons, uncertainty intervals and calendar-block resampling where relevant; random thinning at equal frequency is a useful entry-filter control. Resampling cannot create new market regimes.
- For sizing, compare fixed-base and train-only risk-matched controls. Higher return from simply taking more risk is not an edge improvement.
- Stress higher fees, spread/slippage, stop gaps, missed tail winners, weaker gross wins, sequence clustering and outages. Separate realized-equity DD from mark-to-market DD.
- Freeze a contract before prospective paper observation. No automatic promotion to live from PF > 1, a p-value, or a favorable Monte Carlo chart. Register all post-freeze changes as new versions.

## Entry library: TF Pre-NY Profile 15m

These are **selectors/annotations on a common baseline**, not independent structural ORB submodels. They are saved for reuse, not all activated together.

Common baseline: Pre-NY profile 01:00–09:00 New York; completed 15m candles/delta; entry window 09:00–12:00; long above VAH, short below VAL; two consecutive outside closes with directional delta/body/progress support; POC stop; next-day 08:59 exit trigger. See the exact [shared contract](../contracts/COMMON_V1.md).

| Selector | Operational meaning | Interpretation / caveat |
|---|---|---|
| Baseline | First supported baseline candidate | Control; not an unconditional NY-open entry |
| C1: delta × weak result | Reject high directional delta imbalance AND weak ATR-normalized displacement, using past-training medians | Exclusion signal; missing research features keep baseline; no later replacement |
| C2: initiative | Above-reference directional aggressive volume + positive delta + good candle result | Inclusion label; not proof of informed traders |
| C2: possible absorption | Above-reference directional aggression + positive delta + weak result | A descriptive label, **not intrinsically an avoid/enter rule**; ETH deliberately includes this group |
| C2: support persisting | At least two consecutive supported outside-value bars | Includes directional VWAP and initiative/path support; no absorption/fading/divergence on those bars |
| C2: path of least resistance | Below-reference total volume + positive directional delta + good result | Research annotation, not a separately selected current model |
| C2: participation fading | Three-bar declining directional delta, aggressive volume and progress | Research annotation; not an extra active veto unless the chosen definition already uses it |
| C2 OR I/A/P | Initiative OR possible absorption OR support persisting | ETH chosen entry; deduplicate one candidate, not three trades |
| C2 OR I/P | Initiative OR support persisting | Separate researched union; excludes absorption-only group |
| MA stack aligned | Long: close > SMA20 > EMA200; short: close < SMA20 < EMA200 | Completed 15m indicators, not daily MA |
| MA mixed | Neither aligned nor opposed stack under the existing feature definition | Research comparator; not current selection |
| EMA200 distance | sign × 100 × (signal close / EMA200 − 1) ≥ threshold | Studied examples 0%, 0.1%, 0.5%; entry-selector use is different from sizing use |
| ADX strength | Completed 15m Wilder ADX(14) ≥ threshold | Examples 25, 30; has strength but no direction |

Also retain the earlier six plug hypotheses: VWAP-side persistence, value-edge acceptance, relative volume × weak result, directional delta × weak result, ATR activation, fee/stop burden. Their source definitions remain in `evaluate_plugs.py`; do not substitute similarly named new formulas.

No bubbles, P99, large-trade clusters or new microstructure indicators in this model. Historical ATR normalizers used by C1/C2 are **1h ATR14**, despite the 15m decision clock. Changing that is a new experiment.

## Position-sizing library (separate experiments)

| Family | Hypothesis | Control/guardrail |
|---|---|---|
| Fixed fractional | Risk a constant fraction of current equity | Reference 0.5%; compounding, not fixed money per trade |
| Volatility-based | Lower exposure when causal realized/EWMA volatility rises | Existing capped volatility study; do not automatically lever low volatility above baseline; compare risk-matched |
| EMA200-based | Directionally supported local trend may justify full rather than half risk | Tested full 0.5% vs half 0.25%, separate from ADX |
| ADX-based | Stronger trend conditions may justify full rather than half risk | Same bounded sizing; ADX does not choose direction |

Current research implementation uses baseline size when a sizing indicator is unavailable; that is an explicit historical fallback, not permission to trade on broken/stale live data. Risk/reward and expected stop loss are not guaranteed realized losses. Fees, gap fills and liquidation constraints matter.

Kelly is a noisy estimate, not a compulsory bet size. Estimate it with uncertainty, then stress the candidate sizes; prefer a conservative operational cap. Do not increase risk because of a few large winners.

## Exit and risk/reward parameter library

These are optional grids, not simultaneous rules:

- Fixed full-position targets: 1R, 1.5R, 2R (earlier research also 3R/4R).
- Partial + time exit: TP1 at 1R/1.5R/2R; TP1 fraction 10%/20%/50%; rest at original stop or 08:59 next day. The latest stress shortlist examined 1R/2R × 10%/20%.
- Runner research: full or partial runner, activation threshold, fixed-R distance, ATR timeframe/multiplier or exchange-native percentage callback. They are different policies, not interchangeable names.
- Breakeven/net-positive protection is optional and must specify fees, trigger and remaining-position treatment. It is **off** in the three contracts recorded here.
- Stop alternatives must be compared at identical causal entries and in %, ATR and R. No current bubble stop; POC is the present baseline.
- Multiday holding remains a separate experiment. Current three contracts are intraday/session-bound and do not authorize it.

Future reminder, not active: test C1, C2 or a C2 union plus **one** of SMA/EMA/ADX, one at a time. Looser thresholds may be investigated with a small preregistered grid. Do not combine SMA + EMA + ADX automatically.

## Evidence and software contract

Keep one candidate/context ledger with pre-entry features and separately named outcome columns. Reuse `backtest_engine` for execution, sweeps for parameter grids, and `risk_research` for sizing/Kelly/stress. Do not create a new execution engine per strategy.

Every run must record configuration, code/data fingerprint, train/test dates, calibration artifacts, completion status, exclusions and output paths. Preserve prior results. A model contract is documentation/configuration intent; it does not by itself mean a live adapter exists.

Current checkpoint: [three paper candidates and findings](../contracts/README.md). Infrastructure discussion: [deployment options](DEPLOYMENT_OPTIONS_2026-09-27.md).
