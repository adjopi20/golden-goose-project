# Shared contract TF-PRENY-15M — v1

Date: 2026-09-27. Status: research rules recorded for paper-trading candidates; not live authorization. Read with one asset contract. This specializes the research baseline; it does not modify historical outputs or runtime code.

## Location and data

- Binance futures trade data is the researched feed. Exact symbols: ETHUSDC, BNBUSDC, HYPEUSDT. Do not rename HYPE to HYPEUSDC. Another venue/product requires an explicit mapping and adapter validation.
- Timezone `America/New_York`, with DST; storage timestamps UTC epoch milliseconds.
- Profile window: session date 01:00 inclusive to 09:00 exclusive. Freeze VAH/VAL/POC at 09:00.
- Base-quantity volume across 50 equal-width bins over that window's traded low/high. POC is largest-volume bin (first/lower bin wins a tie), POC price is bin midpoint. Expand contiguous value area from POC until at least 70% volume; take larger adjacent bin, lower bin on ties. VAL/VAH are outer selected-bin boundaries. Degenerate single-price profile uses that price.
- Decisions use completed clock-aligned 15m OHLC and aggressive buy/sell quantity. Binance buyer-is-maker means aggressive sell. Delta = buy quantity − sell quantity. Do not mix quote-volume with base-volume without a new version.

## Baseline candidate, then asset selector

Observe 09:00–12:00. At each completed 15m bar:

1. Long side when close > VAH; short when close < VAL. Equality is inside value.
2. Require at least two consecutive completed closes outside the same edge. Return inside, side change, or missing whole 15m bar resets this sequence.
3. Directional cumulative delta over the outside sequence > 0 and latest directional delta > 0.
4. Latest directional body > 0 and directional close-to-close progress > 0.
5. WAIT if the last three positive directional deltas strictly decrease and the latest positive progress is smaller than preceding progress. Falling delta by itself is not a veto.
6. Emit the **first** supported candidate, then apply the asset selector. No new candidate later that session when this selector rejects it. At most one executed entry per asset/session; no pyramiding/re-entry.

Earliest candidate is 09:30 with two-bar confirmation, even though observation starts at 09:00. Entry exactly at 12:00 is permitted, later is not. No ORB, macro-regime gate, bubbles or P99 condition.

## C2 definitions (ETH and BNB)

References are per NY quarter-hour medians from the previous 20 available sessions, minimum 10 observations. They are updated only after the prior session; never include today's outcome.

For direction sign s = +1 long, −1 short:

- Directional aggressive volume = buy quantity for long, sell for short. Relative aggression = this quantity / its same-clock reference median.
- Directional body = s(close−open); progress = s(close−previous close), or current open when no previous bar is available. Relative body = abs(close−open) / median historical absolute body.
- Directional close position = (close−low)/(high−low) long; (high−close)/(high−low) short; 0.5 for zero range.
- Good result: body > 0, progress > 0, close position > 0.5, relative body ≥ 1.
- Weak result: body ≤ 0 OR progress ≤ 0 OR close position ≤ 0.5 OR [relative body < 1 and adverse wick ≥ abs(body)].
- `initiative`: relative aggression ≥ 1 AND directional delta > 0 AND good result.
- `possible_absorption`: relative aggression ≥ 1 AND directional delta > 0 AND weak result. This means possible absorption of setup-side aggressors, not automatically a beneficial shelf or smart money.
- `path_of_least_resistance`: relative total volume < 1 AND directional delta > 0 AND good result.
- `participation_fading`: three successive declines in positive directional delta, directional progress and directional aggressive volume in the current episode.
- Divergence requires a new favorable extreme versus recent episode bars, lower directional session CVD than at the previous favorable peak, AND weak result.
- Supported bar: outside the directional edge, on the directional side of causal session VWAP, initiative OR path, no absorption/fading/divergence, complete observed session coverage.
- `SUPPORT_PERSISTING`: supported for at least two consecutive bars. Evidence history retains four bars, resets across inside/outside transitions; missing whole bar invalidates complete-session support.
- Causal session VWAP here is a **bar approximation**, sum((high+low+close)/3 × volume)/sum(volume) from 09:00. It is not tick-exact VWAP. ATR annotations use completed 1h ATR14 known before bar open; this does not turn the strategy clock into 1h.

Selectors use only the setup direction's snapshot at signal formation, not a later label. ETH uses initiative OR possible absorption OR support persisting. BNB uses initiative only. Neither adds a separate EMA/ADX entry filter.

## Risk, exit and compounding

- Initial stop = frozen Pre-NY POC. Long stop must be below actual entry; short stop above. Invalid geometry means no executable trade.
- Define initial price risk D = abs(actual entry−POC). TP1 = entry + s × target_R × D. Never move POC during the trade.
- TP1 closes 10% of initial filled quantity (venue rounding must be handled and logged); remaining 90% retains the **original POC stop**. No trailing, breakeven move, add-on, daily regime exit or second fixed target.
- Time exit trigger: next-day **08:59 New York**; close all remaining quantity. If TP1 never happens, close the full remainder. Holidays/weekends are not excluded by this research rule.
- Quantity research formula for a linear contract: min(E × risk_fraction / D, E × 5 / entry). E is current allocated account equity. Recompute for each new trade: geometric compounding. Max notional/equity is 5, not a mandate to use 5× leverage or a statement about margin mode.
- ETH/BNB risk tiers are 0.5% and 0.25%; HYPE fixed 0.5%. Quantity is not rebalanced inside an open trade. Fees/gaps can cause realized loss larger than intended risk.
- Each model has an independent capital ledger in this evidence. Do not sum the independent equity curves as if a shared-account budget had been tested.

## Execution reference and non-parity warnings

Historical shared-engine replay: first eligible raw aggTrade for entry, at most 300s late and never after 12:00; stop execution uses subsequent eligible print after trigger. Do not retrospectively choose a favorable print. Actual venue matching can differ from this proxy.

Study costs: 4 bps per fill on filled notional; source-ledger execution slippage is included. Funding, queue position, full market impact and live failure rates were not fully modeled. Reported DD is realized/closed equity, not liquidation-safe mark-to-market DD.

Strategy data warm-up unavailability is not the same as a broken feed. Historical C1/EMA/ADX selector fallbacks kept the baseline when relevant features were unknown; sizing fallback used full base risk. Log those semantics during parity testing. **Do not silently convert them into live permission or quietly change them to a new profitable filter.** A separately approved data-health interlock should block live submissions on stale/corrupt inputs while recording the theoretical research decision.

## Paper/live readiness gates (not additional alpha rules)

Before sending any real orders: implement and verify signal parity, complete profile/indicator warm-up, persisted session state, unique order IDs, restart reconciliation, no duplicate fills, reduce-only exits, partial rounding, native protective stop acknowledgment, trigger-price semantics, fee/funding recording, retry/rate-limit behavior and alerting. Verify current symbol/tick/lot/minimum-notional and product availability.

Pending operational choices include venue/product mappings, account capital allocation/margin mode, acceptable slippage/retry budgets, funding treatment, missing-data interlock and emergency procedures. Do not invent these from historical performance. Paper trading is for testing these in addition to recording new performance.

Source implementation: `strategy.py`, `effort_result.py`, `prepare_selected_entries.py`, `evaluate_plugs.py`, `audit_opportunity.py`; shared `backtest_engine`; `risk_research/indicator_sizing.py`. See checkpoint manifest for hashes. A native Lighter or MultiCharts implementation is not yet certified equivalent.
