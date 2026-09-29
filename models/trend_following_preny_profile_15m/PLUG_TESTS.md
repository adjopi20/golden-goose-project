# Pre-NY 15m entry plug study (research only)

2026-09-26: active scope is Pre-NY only (four assets, four groups). The six
probes below document the historical study; only `delta_without_result` is
the next challenger, not a live filter. See `RESEARCH_FINDINGS_2026-09-26.md`.

The baseline remains `models.trend_following_preny_profile_15m.strategy` with its existing
09:00–12:00 entry window, Pre-NY profile only, POC stop and next-day
08:59 exit. `backtest_engine.replay_aggtrades` supplies actual baseline fills,
fees and net R. This study never recalculates or changes those trades.

`evaluate_plugs.py` applies **one gate at a time** to baseline executed trades.
A rejected trade contributes zero R; a missing feature preserves the baseline
trade. It is an attribution experiment, **not** a compounded-equity backtest or
an approved live filter. Each pair and profile is evaluated separately.

## Prespecified probes

All features are known at the completed baseline signal. The last four 15-minute
bars mean up to one hour ending at that signal, never bars afterward.

| Plug | Measurement | Research gate |
|---|---|---|
| `vwap_persistence` | Minimum directional close-to-causal-NY-VWAP gap, in completed-hour ATR, across the recent bars. VWAP uses 15-minute typical price weighted by bar trade volume. | Keep >= earlier train median. |
| `value_edge_acceptance` | Minimum directional close-to-VAH/VAL gap, in ATR, across the recent bars. | Keep >= earlier train median. |
| `volume_without_result` | Relative volume of the recent NY clock slots versus the median of the same slots on up to 20 *earlier* sessions, paired with directional price displacement in ATR. | Reject only high effort with below-median price result. |
| `delta_without_result` | Directional sum of buyer-minus-seller volume divided by total volume, paired with directional price displacement in ATR. | Reject only high delta effort with below-median price result. |
| `atr_activation` | Completed hourly ATR(14) / median of the prior 20 completed hourly ATR values, from the causal opportunity audit. | Keep >= earlier train median. |
| `fee_stop_burden` | Two fee fills as a fraction of initial POC risk, using actual baseline fill and stop. | Keep <= earlier train median. |

The median is a fixed *procedure*, not a threshold selected for best historical
profit. It is fitted separately from the previous train window for each pair
and profile. The effort/result probes are intentionally conservative: they
only research whether high aggression with weak price response is a bad trait.
They do **not** equate low volume with failure or assert that aggressive flow
always causes continuation.

## Validation

Default: 180 calendar days of training, one NY-session embargo, then 60
calendar days of test, rolling forward by 60 days. A fold needs at least 25
baseline train trades and a given plug needs at least 25 nonmissing training
features. No future session contributes to same-clock volume or fold thresholds.

`summary.csv` compares the baseline and each one-plug gate on the *same* test
opportunities. Read rejected losers together with rejected winners and 2%-MFE
tails. `folds.jsonl` exposes temporal stability; `cross_asset_stability.csv`
equal-weights assets and shows the worst asset. `predictions.jsonl` makes each
decision auditable. There is no combined six-plug rule: combination comes only
after incremental value is supported on future data.

Because the assets have different available histories, the full-history
cross-asset table mixes calendar regimes. `common_period_summary.csv` and
`common_period_stability.csv` repeat the comparison only on the overlapping
test-date interval; the manifest states its dates. Both full-history and common
period views must agree before describing an effect as cross-asset stable.

Historical BTC has already been inspected extensively. Even chronological
folds within it are **research replay, not genuinely untouched OOS evidence**.
Likewise ETH/BNB/HYPE may not be statistically independent on the same market
dates. A gate that looks good must ultimately survive newly collected months,
fee assumptions, and a true equity replay with altered trade frequency.

## Inputs

Per pair, first produce the unchanged observation, shared-engine backtest, and
`audit_opportunity.py` v02 files. This evaluator then requires only the
observation directory and audit directory; it does not scan raw aggTrades or
minute candles. The audit must have `audit_schema_version=2` and explicit
epoch-millisecond hourly timestamps. The fee passed to the evaluator must
match the backtest's fee per fill (4 bps in the current baseline).

Example, after BTC files exist:

```powershell
python -m models.trend_following_preny_profile_15m.evaluate_plugs `
  --dataset BTCUSDC "models/trend_following_preny_profile_15m/runs/btcusdc_2024-01-05_to_2026-08-30_v2_full" "models/trend_following_preny_profile_15m/runs/btcusdc_2024-01-05_to_2026-08-30_opportunity_audit_v02" 4 `
  --output-dir "models/trend_following_preny_profile_15m/runs/btcusdc_plug_research_v01"
```

Repeat `--dataset SYMBOL OBS_DIR AUDIT_DIR FEE_BPS` for ETHUSDC, BNBUSDC and
HYPEUSDT in the same call to obtain the cross-asset report. The training rule,
test horizon, profile definitions and baseline parameters must remain identical.
