# Trend Following Pre-NY Profile 15m

Checkpoint: **TF-PRENY-15M-001**, 2026-09-26. Research baseline, not a live-deployment approval.

## Hypothesis and scope

Persistent progress outside the completed Pre-NY value area, supported by directional aggressive flow and price response, may identify intraday expansion. This is a falsifiable hypothesis, not an established edge.

- Four separate asset experiments: BTCUSDC, ETHUSDC, BNBUSDC, HYPEUSDT.
- Only **Pre-NY volume profile**, 01:00 inclusive to 09:00 exclusive, America/New_York (DST-aware).
- Price candles and delta observations: **completed 15-minute bars**. No waiting for 1-hour closes.
- Previous-day/24h profile is excluded from the active model and comparisons.
- Historical runs are preserved; old two-profile results are not relabelled as new runs.

## Entry rules (unchanged Pre-NY baseline)

1. Observe 09:00-12:00 NY. Entry fill at exactly 12:00 is allowed; after it is not.
2. Long above VAH; short below VAL. A wick alone does not confirm acceptance.
3. Default: two consecutive completed 15-minute closes outside the same edge. Earliest candidate is 09:30. The existing three-bar option remains a research parameter, not the default.
4. Directional cumulative delta of the outside-value sequence and latest-bar delta must be positive; latest directional candle body and close-to-close progress must also be positive.
5. Three successively declining positive directional deltas **together with** declining positive price progress cause WAIT. Declining delta alone is not a veto.
6. Re-entry into value, side change, or a missing 15-minute bar resets the sequence. Maximum one entry candidate per asset/session.
7. No eligible breakout by 12:00 means no trade. The new delta-result challenger is **not active** in baseline entry.

Profile parameters remain 50 price bins and 70% value area, without new optimization. Profile is frozen at 09:00 before entry observation.

## Stop, exit, sizing and execution

- Initial hard stop: completed Pre-NY POC; must be below actual long fill / above actual short fill.
- No fixed TP, partial exit or trailing stop in this baseline.
- Time-exit trigger: next-day 08:59 NY. Raw replay fills at the first available eligible print; a sparse feed can fill later than trigger time.
- Reuse `backtest_engine.replay_aggtrades`; do not create a model-specific backtest engine.
- Entry uses the first eligible aggTrade, maximum delay 300 seconds and never after 12:00. Stop fills on the print after its trigger.
- Comparison settings unchanged: initial equity 1000, risk fraction 0.005, maximum leverage 5, fee 4 bps per fill. Assets remain separate portfolios.
- AggTrade fill is a proxy, not an order-book execution model. Funding/market impact are absent. Reported equity drawdown is realized-close based, not intratrade mark-to-market.
- No new daily-loss circuit breaker, risk adjustment or regime filter is introduced by this checkpoint.

## Interfaces and historical compatibility

Active module: `models.trend_following_preny_profile_15m.strategy`.
Optional `--profile-window pre_ny` is now the only accepted profile choice and the default.
Signal schema remains compatible with the shared replay engine; strategy identifier is `trend_following_preny_profile_15m_v1`.
Prepared historical profiles containing both windows can be read: only Pre-NY is evaluated. Use a fresh output directory, not an old two-profile output.

`audit_opportunity.py` remains a non-trading research utility. Its existing EMA/ADX/ATR **hourly context** fields and schema are preserved to reproduce the historical study. These do not decide entry, and do not mean the active candle is 1h. Changing those research denominators to 15m would be a separate versioned audit, not part of this rename. New strategy preparation no longer generates 1h candles.

The folder (including runs) was renamed from `models/daily_profile_1h`. Historical manifest paths remain provenance strings; point commands at the new location. Old module commands must use the new module name. `RULES_LEGACY_V2.md` is historical documentation only.

## Research decision

### Unified observation context (2026-09-26; not a trading filter)

`evaluate_plugs --context-only` combines the original six-plug features, existing
signal-time audit context, historical research gates, and new **15m EMA200,
SMA20 (MA20), ADX14 and directional DI** into one candidate ledger across assets.
It does not refit thresholds or change entries/stops/exits. See
`OBSERVATION_CONTEXT.md` for the command, timing and coverage contract.
The old hourly ATR and EMA/ADX fields remain explicitly separate and unchanged.
No order bubbles/P99, ADX cutoff, MA filter, or new strategy rule is introduced.

See `RESEARCH_FINDINGS_2026-09-26.md`. Only `delta_without_result` advances as a candidate challenger; it is not frozen as a trading filter. Next research: baseline versus this challenger using shared-engine replay, fixed entry/stop/exit/cost/sizing, plus equal-frequency random-removal control. No stacking six gates, no asset-specific winning-rule selection, no replacement entry after rejection in the first comparison.

Promotion requires improved net quality and risk across periods/assets while reporting sacrificed winners and tails. No new numeric success threshold is selected from these results. Chronological reuse of already-inspected data is research, not untouched OOS.
