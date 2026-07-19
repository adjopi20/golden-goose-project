# Strategy: AVAXUSDC ORB Variant 009 v1.0

Status: frozen for real-time paper observation only; not approved for real-money execution.

## Overview

- **Asset class**: Binance spot crypto, AVAXUSDC only
- **Timeframe**: Completed 1-minute candles; 15-minute opening range
- **Style**: Trend-following opening-range breakout continuation
- **Edge hypothesis**: AVAX directional expansion after the New York opening range can persist when price acceptance, normalized volume, directional delta, and large-trade flow agree.

## Entry Rules

All conditions use AND logic unless an OR is stated explicitly.

- Build the ORB from 09:30-09:45 America/New_York; evaluate entries from 09:45 inclusive to 10:15 exclusive.
- Long candidates touch and close above the ORB high; shorts touch and close below the ORB low.
- Candidate body/range is at least 0.35. Long close position is at least 0.55; short close position is at most 0.45.
- Current candle delta supports direction. Prior 15-minute directional delta/volume is at least 0.05.
- The first direct price inefficiency fixes direction. Direct displacement requires body/range at least 0.65, directional close position at least 0.70 for longs or at most 0.30 for shorts, and either range/30-candle median range at least 1.5 or absolute delta/volume at least 0.85.
- Reject after the opposite ORB extreme has been touched.
- Require direct displacement OR a completed breakout-retest continuation.
- Current completed-candle volume is at least 2.0 times the median positive volume of up to 30 preceding completed candles.
- Supportive p95 bubble quantity divided by opposing p95 bubble quantity is at least 1.0 when opposing bubbles exist. No bubbles on either side pass the current implementation.
- Only one position may be open.

**Entry execution**:

- Order type: simulated market entry at the completed signal-candle close
- Slippage: 5 bps adverse simulation
- Max entry time: immediately after the qualifying candle completes

## Exit Rules

### Stop Loss

- **Method**: Opposite ORB extreme
- **Parameters**: ORB low for longs; ORB high for shorts
- **Hard stop**: reject stop distance below 0.15% or above 2.50% of requested entry

### Take Profit

- **Method**: Fixed risk multiple
- **Parameters**: 1R
- **Partial exits**: close 50% at 1R; retain 50% as runner

### Trailing Stop

- **Method**: Initial-risk trail behind best observed price
- **Parameters**: 1R distance
- **Activation**: at the 1R TP1 event

### Time Stop

- **Trigger**: first raw trade at or after 01:30 America/New_York on the next session day
- **Rationale**: prevent unresolved positions carrying indefinitely

### Signal Exit

- **Condition**: none
- **Overrides**: not applicable

### Exit Priority

1. Initial/protected stop
2. TP1 and protection update
3. Runner trailing stop
4. Next-day time stop

## Position Sizing

- **Method**: Fixed fractional
- **Risk per trade**: 5% of current paper equity
- **Calculation**: `position_size = (paper_equity * 0.05) / abs(filled_entry - stop)`
- **Max position**: no notional cap implemented; paper-only limitation
- **Min position**: none implemented

## Risk Parameters

- **Max concurrent positions**: 1
- **Max correlated positions**: 1
- **Daily loss limit**: none implemented; paper only
- **Weekly loss limit**: manual review after three consecutive losing weeks
- **Max drawdown halt**: manually stop and review at 25% from paper-equity peak
- **Correlated exposure limit**: AVAXUSDC only

## Filters

### Market Filters

- **Regime filter**: none
- **Volatility filter**: stop distance between 0.15% and 2.50%
- **Correlation filter**: none

### Token Filters

- **Volume filter**: completed signal-candle expansion at least 2.0
- **Liquidity filter**: fixed Binance AVAXUSDC market; no separate depth threshold
- **Age filter**: not applicable
- **Holder filter**: not applicable
- **Concentration filter**: not applicable

### Time Filters

- **Active hours**: 09:45-10:15 America/New_York
- **Day filter**: every calendar day
- **Event filter**: none

## Performance Criteria

### Continue Paper Trading

- At least 30 real-time paper trades before promotion review
- Rolling profit factor above 1.0
- Win rate within 25% of the 62.2% historical baseline
- Drawdown below the 25% manual halt

### Review Required

- Any headline metric degrades more than 25% from the historical baseline
- Three consecutive losing weeks
- Material Binance market-structure or fee change

### Retire Strategy

- Rolling 30-day profit factor below 1.0 for two consecutive reviews
- Three consecutive losing months
- The 25% drawdown halt triggers twice in 30 days
- The observed continuation edge no longer exists

## Backtest Results

### In-Sample / Exploratory Historical Benchmark

- **Period**: 2024-03-21 to 2026-06-30
- **Total return**: 54.9% compounded paper equity ($1,000 to $1,549.18)
- **Sharpe ratio**: not computed
- **Max drawdown**: 20.4%
- **Win rate**: 62.2%
- **Profit factor**: 1.339R
- **Trade count**: 74
- **Average trade duration**: not recorded in the frozen summary

### Out-of-Sample

- **Period**: not completed
- **Total return**: unavailable
- **Sharpe ratio**: unavailable
- **Max drawdown**: unavailable
- **Win rate**: unavailable
- **Profit factor**: unavailable
- **Trade count**: 0
- **Degradation from IS**: unavailable

### Paper Trade Results

- **Period**: begins with v1.0 deployment
- **Total return**: pending
- **Comparison to OOS**: OOS unavailable; compare initially with the frozen historical benchmark

## Dependencies

- **Data source**: public Binance AVAXUSDC aggTrade and 1-minute kline websocket streams
- **Indicators**: repository OHLCV aggregation, volume profile, delta, and p95 order bubbles
- **Execution**: deterministic `orb_live_agent` paper broker; no exchange-order route exists
- **Risk**: fixed-fraction paper sizing and stop-distance risk gate

## Notes

- Runtime profile: `apps/orb_live_agent/profiles/avaxusdc_variant009_paper.conf`
- Historical source: `apps/orb_live_agent/data/backtests/avaxusdc/20260709_sweep_4_avaxusdc/variant_009`
- A fresh log directory needs 24 hours of websocket warm-up. Discard any paper decisions from the first 24 hours because the prior-24-hour profile is incomplete.
- Removing the 2.50% maximum admitted two losing trades, reduced total return from 11.00R to 8.90R, and increased drawdown from 20.4% to 26.2%.
- Historical results were selected retrospectively and are not walk-forward/OOS proof. Paper observation is required before real-money consideration.

## Change Log

- **v1.0 2026-07-19**: Frozen AVAXUSDC-only variant_009 for deterministic real-time paper observation.
