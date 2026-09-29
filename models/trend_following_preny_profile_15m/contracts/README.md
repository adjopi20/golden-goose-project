# Checkpoint TF-PRENY-PAPER-001 — 2026-09-27

## Decision

Three asset-specific **working research models / paper candidates** are recorded. This is a specification checkpoint, not a claim of established live profitability, not authorization to place orders, and not another parameter optimization.

| Contract | Entry selector after common baseline | TP1 | Remainder | Sizing |
|---|---|---|---|---|
| [ETHUSDC](ETHUSDC_V1.md) | C2 initiative OR possible absorption OR support persisting | 10% at 1R | Original POC stop / next-day 08:59 NY | EMA200 directional gap ≥0.5%: 0.5% risk; else 0.25% |
| [BNBUSDC](BNBUSDC_V1.md) | C2 initiative | 10% at 2R | Same | ADX14 ≥30: 0.5%; else 0.25% |
| [HYPEUSDT](HYPEUSDT_V1.md) | C1 delta × weak-result exclusion | 10% at 1R | Same | Fixed 0.5% |

ETH 1R/10% was explicitly selected by the user during this checkpoint. BNB/HYPE retain the shortlist references. No active BTC contract. No combined-account model. The shared specification is [COMMON_V1.md](COMMON_V1.md); the model-specific [research protocol](../docs/RESEARCH_PROTOCOL.md) preserves other selectors without activating them for this trend-following Pre-NY 15m model.

## Latest evidence, exact chosen exits

Source: `reports/risk_research/three_shortlists_selected_sizing_stress_v01/historical_sizing.csv`, scenario `baseline`, policy `indicator_sizing` for ETH/BNB and `fixed_base` for HYPE.

| Metric | ETH | BNB | HYPE |
|---|---:|---:|---:|
| Evaluated dates | 2024-11-01–2026-08-30 | 2026-06-01–2026-08-30 | 2026-03-01–2026-08-30 |
| Trades | 317 | 25 | 97 |
| Net WR | 35.65% | 40.00% | 37.11% |
| Mean net R/trade | +0.2634 | +0.5502 | +0.4502 |
| Money PF | 1.506 | 2.441 | 1.771 |
| Compounded return | +50.22% | +7.16% | +23.28% |
| Final equity from 1,000 | 1,502.24 | 1,071.63 | 1,232.76 |
| Realized max DD | 5.88% | 2.71% | 4.75% |
| Combined stress return | −14.11% | +2.24% | −0.47% |
| Combined stress max DD | 21.31% | 4.65% | 10.08% |
| Return after missing largest winner | +35.54% | +0.40% | +12.46% |

Different periods and trade counts: do not rank these returns as if exposure/time were equal. They are subsets after historical fold/calibration eligibility, not the whole available raw dataset. Research fees are included; funding/full market impact and floating drawdowns are not fully included. Net-R expectancy and money PF are different units, especially with changing size.

Combined stress = fees ×1.5, extra round-trip 2 bps, additional 0.25R stop loss on remaining exposure, and positive gross trade profit ×0.75. It does not also remove the largest winner. These are conditional adverse assumptions, not forecasts.

Stress study used 5,000 paths and 7-/14-calendar-day blocks. Baseline 14-day-block DD p95 for the chosen exits is approximately ETH 14.85%, BNB 4.26%, HYPE 9.03%. Resampling these small historical samples does not prove future survival; BNB's small DD estimate is particularly sample-limited. Source `stress_summary.csv`.

## Findings to retain

- ETH has the deepest selected evidence; EMA sizing retained a useful historical trade-off. Choosing 1R/10% slightly sacrifices return versus 2R/10% for lower observed DD. This is a preference, not statistical proof of superiority.
- BNB is promising but fragile: only 25 evaluated trades, eight full-size ADX≥30 trades, gains concentrated in August, and almost no profit after deleting the top winner. Do not generalize its PF 2.44 into reliable future odds.
- HYPE retains C1 entry and simple fixed sizing. Extra sizing sophistication was not justified. Tail dependence remains even when fixed sizing is preferable.
- Positive sizing multipliers do not improve the WR of an unchanged trade ledger. Money PF and DD can change. Use a train-only risk-matched control in addition to fixed 0.5%.
- Possible absorption is not universally bad. It is an ETH inclusion group; do not silently invert it or add it to BNB/HYPE.
- These results have influenced model selection. Historical walk-forward is not pristine unseen OOS after repeated inspection. Prospective paper logs are the next untouched evidence stream.

## Provenance and reusable tools

- Entry implementation: `strategy.py`, `effort_result.py`, `evaluate_plugs.py`, `prepare_selected_entries.py`, `audit_opportunity.py` in the parent model folder.
- Entry/context research: `runs/four_asset_stateful_effort_result_v01`, `runs/four_asset_observation_context_v01`, `runs/four_asset_plug_study_v01`.
- Raw execution/sweep: `runs/four_asset_selected_partial_time_exit_v01`; shared `backtest_engine/replay_aggtrades.py` and `backtest_engine/research_exit_variants.py`.
- Sizing evidence: `reports/risk_research/three_shortlists_volatility_sizing_v01`, `three_shortlists_indicator_sizing_v01`, `three_shortlists_selected_sizing_stress_v01`.
- Stress specification: `risk_research/configs/three_shortlists_selected_sizing_stress.json`.
- Snapshot hashes and source-file validation: `evidence_manifest.json` beside this file. Paths in that manifest are repository-relative.

## Before paper trading

Implement an adapter, not another backtest engine. Verify identical historical decisions from the shared code, persist order/session state, validate protective orders and restart recovery, initialize HYPE's causal C1 calibration, record fees/funding and measure floating equity. Use testnet for API plumbing and mainnet shadow data for realistic signal/cost observation; testnet fills are not profitability evidence.

No live capital, API connection, server purchase or account configuration has been authorized by creating this checkpoint. Review operational unknowns in COMMON_V1 and [deployment options](../docs/DEPLOYMENT_OPTIONS_2026-09-27.md).
