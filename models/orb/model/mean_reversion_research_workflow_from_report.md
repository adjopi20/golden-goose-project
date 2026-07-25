# Mean Reversion Research Workflow From Report

Source: `C:\Users\adjop\Downloads\Mean Reversion Trading Strategy Edge Research Report.pdf`

Status: research workflow, not a proven edge.

## Report Takeaway

The report does not support raw Bollinger/RSI as a reliable standalone edge. It ranks mean-reversion methods by four gates:

1. Plausible economic mechanism.
2. Positive expectancy after realistic fees, spread, slippage, borrow/funding, and impact.
3. Out-of-sample robustness with multiple-testing control.
4. Deployability at the intended liquidity and execution size.

The strongest report-native strategy is factor-neutral residual mean reversion in liquid equities. The second is cointegrated ETF/futures pairs with a dynamic hedge ratio. For this repo's AVAXUSDC/ORB work, the closest honest adaptation is not factor stat-arb; it is structural single-asset mean reversion around session levels and volume-profile anchors, tested as a weaker tactical model.

## Strategy Families To Test

### A. Report-Native Residual Stat-Arb

Use only if the dataset is cross-sectional: equities, ETFs, futures basket, or several liquid crypto assets.

- Build returns for each instrument.
- Regress each instrument against common factors or PCA factors.
- Trade the standardized residual, not raw price.
- Keep beta/factor/sector exposure near neutral.
- Penalize turnover and cost directly.
- Validate signal monotonicity by residual-z bucket.

Default windows:

- Factor exposure: 126 to 252 bars.
- Residual stats: 60 bars.
- Idiosyncratic volatility: 20 to 60 bars.
- Entry: residual z or s-score around 1.25 to 3.0.
- Exit: residual z back near 0.25 to 0.75.

### B. Report-Native Kalman/Cointegrated Pairs

Use only when there is a real economic relationship between instruments.

- Pre-screen pairs by economic linkage and liquidity.
- Require rolling cointegration/stationarity checks.
- Estimate dynamic hedge ratio with Kalman filter or rolling OLS.
- Trade spread z-score, not the outright asset.
- Stop by z-score break, structural break, or time stop.

Default windows:

- Pair formation: 252 to 504 bars.
- Hedge/signal warm-up: 60 to 120 bars.
- Entry: absolute z around 1.75 to 2.25.
- Exit: absolute z below 0.25 to 0.75.
- Time stop: 2x to 3x estimated half-life.

### C. Repo-Local AVAXUSDC Structural Mean Reversion

Use this for current ORB/NY work. It is tactical and must be reported separately from trend following.

Premise:

- First classify session as tradeable versus non-tradeable.
- ORB/bias context stays authoritative; mean reversion does not flip bias by itself.
- Trade only structural sweeps of prior completed session highs/lows or prior-24h profile extremes.
- Enter only after closed 1m reclaim/rejection plus delta/orderflow confirmation.
- TP1 is prior-24h POC.
- TP2 is prior-24h VAH for longs or VAL for shorts.
- Stop goes beyond the sweep extreme plus a fixed buffer.

Existing repo pieces to reuse:

- `indicator.ohlcv.aggregate_trades_to_ohlcv` for 1m candles.
- `indicator.volume_profile.build_volume_profile` for POC/VAL/VAH.
- `models/orb/scripts/find_mr_structural_candidates.py` as the current candidate-scanner starting point.
- `apps/orb_live_agent/src/orb_live_agent/paper_broker.py` for mean-reversion TP handling.
- `apps/orb_live_agent/src/orb_live_agent/execution_engine.py` for fee/slippage-aware paper execution.

## Backtest Algorithm

For each walk-forward segment:

1. Load raw aggTrades or cross-sectional bars.
2. Build only closed 1m-or-higher candles. Do not use sub-minute candles.
3. Freeze all reference levels at the time they would be known:
   - prior completed sessions,
   - prior-24h POC/VAL/VAH,
   - rolling residual or pair statistics ending at `t-1`.
4. Generate candidates without looking past the signal candle.
5. Simulate next-candle or next-trade fill with fee and slippage.
6. Reject trades with invalid stop geometry or stop risk outside configured limits.
7. Track every rejected candidate and every accepted trade.
8. Export full distribution, not only winners:
   - by setup family,
   - by date/month/regime,
   - by signal-strength bucket,
   - by holding time,
   - before costs,
   - after base costs,
   - after 2x and 3x cost stress.

## Statistical Tests

Run these before calling anything an edge:

- Stationarity: ADF or equivalent on residual/spread, not raw price.
- Half-life: reject if too slow versus intended holding period.
- Bucket monotonicity: larger absolute residual/sweep score should not perform worse randomly.
- Walk-forward: tune on train only, report every OOS fold.
- Bootstrap/permutation: trade-order bootstrap and randomized-entry baseline.
- Regime split: trend day, range day, high-volatility day, low-liquidity day.
- Cost stress: base, 2x, and 3x fee/spread/slippage.
- Capacity sanity: expected alpha must exceed spread + slippage + impact at target size.

## Acceptance Gates

Promote to candidate only if all are true:

- OOS net expectancy is positive after base costs.
- OOS net expectancy remains acceptable after 2x cost stress.
- No single month or small cluster explains most profit.
- Max drawdown and loss streak are survivable at the intended risk fraction.
- Result distribution is stable across train/OOS folds.
- Randomized-entry or naive Bollinger/RSI baseline does not explain the result.
- For AVAXUSDC, performance is reported separately from trend-following ORB.

If any gate fails, label it as rejected research, not partial edge.

## Minimal Implementation Plan

1. Keep this as the spec.
2. Extend the existing MR scanner only if the current fields are insufficient.
3. Add one reusable backtest runner that accepts:
   - input parquet,
   - date range,
   - strategy family,
   - fee bps,
   - slippage bps,
   - output folder.
4. Write markdown findings plus JSONL trades/decisions only when machine-readable inspection is needed.
5. Do not create one-off `generate_<date>_candidates.py` files.

Skipped for now: equities/PCA/Kalman implementation, because this checkout currently has AVAXUSDC ORB infrastructure and no cross-sectional equity/futures dataset.
