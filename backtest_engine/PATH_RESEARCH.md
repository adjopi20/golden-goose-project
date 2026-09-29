# Reusable path research v1

Two layers, not a new trading engine:

1. A model adapter exports actual entry fills, causal context and candidate stops.
2. `python -m backtest_engine.path_audit --experiment config.json --output-dir NEW_DIR`
   caches raw price/ID/time paths once, then evaluates stop/target geometry.

The BTC adapter is `models.btc_pny.prepare_path_audit`. It consumes the completed
70-trade exit challenger, not a new observation run. Early Immediate's CP002 stop
is unchanged. Outside-VA compares its original stop with a validated shelf closest
to that original stop, retaining at least 75% of original risk. The anchor is NOT
silently changed to POC. Missing shelf evidence falls back, never drops a trade.
The inherited shelf recipe uses P99 per-minute aggressive-side VWAP clusters:
at least two bubble minutes, width <=1.5 one-minute ATR, buffer 0.5 ATR, previous
30 minutes excluding the final validation candle. Post-cluster candles must defend
the buffered zone and validation close must finish favorably outside the zone.
ATR uses the same existing last-30-minute calculation. This is an executed-flow
proxy, not observed resting liquidity. Only candles completed by the signal are
used. No automatic expensive feature rebuild. Missing P99 schema is an error.

## Execution

From the repository root:

```powershell
.\models\btc_pny\run_path_audit.ps1 -Stage prepare
.\models\btc_pny\run_path_audit.ps1 -Stage run
```

Run the second only after the first succeeds. Prepare is feature/ledger only;
run reads raw history on the first pass. Raw paths are compressed Parquet files
per actual entry through cutoff + next available print. Completed paths survive
an interrupted run; rerun with a new OutputDir to reuse them. A manifest is only
written after a successful complete audit. Output directories are never overwritten.
Raw source identity uses absolute path, size and mtime (not a full huge-file hash);
each path cache has a SHA256 checksum. In-place raw replacement with spoofed size
and mtime is not detected; use a new path cache for such replacements.

## Outputs

- `trade_target_audit.jsonl`: one row per trade x stop x target, MFE/MAE, target
  first touch, next-print target or terminal fill, fees, drawdown from local peak
  after each milestone until next target or terminal, and winner-to-stop paths.
- `regime_summary.json`: route x stop x target x V05 direction/volatility/alignment.
- `matched_stop_comparison.json`: same-entry winner-to-loser conversions and net R.
  Earlier/later attribution retains the existing historical split (2025-08-15),
  not a newly optimized split or untouched holdout.
- Preparation `shelf_and_regime_evidence.jsonl`: why shelf was used or fell back,
  causal timestamps and full V05 context.
- `manifest.json`: completed run inputs and engine/signal hashes.

The same entry ID/price must match raw data. Initial stop crossings truncate MFE
and milestones immediately; execution is next print. Time cutoff takes priority;
no target first touched at/after cutoff counts. A price gap can cross multiple
targets on one print. Milestone hit rate is NOT net win rate. Missing closing
data fails instead of inventing settlement. MFE is risk-bounded, not unrestricted
beyond a stopped-out position. Store the full path regardless, for future variants.

## Reuse

Copy `experiment.json`, edit `targets_r` (e.g. [1,1.5,2,3,4]), run to a new output
directory. No raw source re-scan for matching entries/horizons. A new stop can be
added to prepared `stops` only if its causal evidence is separately recorded.
Different assets/models export the same signal contract via a thin adapter.
The generic audit currently shares the project's Binance aggTrade reader.

This version implements A (expansion/regime attribution) and B (matched stop
comparison), NOT a generic optimizer, adaptive sizing, partials or trailing.
Existing exit replay remains the place for those execution policies. They can
later consume this cache instead of duplicating raw reads. Do not call this a
full equity backtest: normalized net R assumes linear fees, ignores order-book
impact, funding, size limits and partial liquidity. No best-variant auto-selection.
Historical groups are descriptive and already researched, not untouched OOS.
Keep later held-out data sealed and do not size up from tiny regime cells.

Tests: `python -B -m unittest backtest_engine.tests.test_path_audit -v`.
