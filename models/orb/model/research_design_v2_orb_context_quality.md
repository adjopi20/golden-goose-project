# ORB Research Design V2 - Context Quality Layer

Status: design prompt for next isolated research run.

This version upgrades the prior sequential candle acceptance research. It does not revise corrected result classification. Result classification from the latest corrected-result run is treated as the benchmark label. The new objective is to enrich ORB bias with context so the engine can separate directional premise from entry timing and trade quality.

## Goal Prompt

Use this as the next `/goal` objective:

```text
Revise the ORB research design in models/orb without modifying prior run artifacts.

Create a new isolated run folder under models/orb/runs/ for an ORB context-quality layer. Keep the corrected result classification from 20260627_016_corrected_result_8h_structure_cap unchanged and use it only as benchmark truth. Keep the 09:30 NY opening anchor and display timestamps in WIB where user-facing samples are shown.

Core objective:
Enrich the existing I1_0930 sequential ORB signal so the engine can distinguish:
- directional bias/premise,
- entry readiness,
- wait-for-retrace context,
- wait-for-structure-rejection context,
- dirty breakout / exhaustion / absorption warnings,
- mean-reversion candidates after structural rejection.

Do not treat ORB bias as an automatic entry. Bias decision and entry decision must be separate outputs.

Base workflow:
1. Build ORB Area A volume profile from 09:30-09:45 NY.
2. From 09:45 NY onward, evaluate 1-minute candles sequentially through Area B/C/D:
   - B = 09:45-10:00 NY,
   - C = 10:00-10:15 NY,
   - D = 10:15-10:30 NY.
3. Keep candle labels against Area A VP:
   - balance,
   - bidirectional_armed_imbalance,
   - bidirectional_armed_breakout,
   - armed_imbalance_up/down,
   - imbalance_up/down,
   - armed_breakout_up/down,
   - breakout_up/down.
4. Upgrade acceptance quality:
   - Bias/context scoring starts at B1, before bias is determined.
   - Bias is determined only after sequential evidence is strong enough.
   - A candidate accepted move should prefer 3 total consecutive candles with directional OHLC progression:
     - long/up: candle1 OHLC <= candle2 OHLC <= candle3 OHLC,
     - short/down: candle1 OHLC >= candle2 OHLC >= candle3 OHLC.
   - Preserve the old 4-candle/no-invalidation acceptance result as a baseline comparison, but create the stricter quality-aware variant for this run.

Context features to compute candle by candle:
- directional cleanliness:
  - consecutive higher OHLC for up,
  - consecutive lower OHLC for down,
  - breakout_up with red body is dirty,
  - breakout_down with green body is dirty,
  - breakout labels without OHLC progression warn of absorption/exhaustion.
- acceptance:
  - body remains outside profile boundary,
  - pullback rejects profile_high/profile_low and returns in bias direction,
  - rejection at a stronger level increases confidence.
- exhaustion/absorption:
  - new high/low but weaker extension,
  - large aggressive volume with poor price follow-through,
  - breakout candle followed by opposite-color reclaim,
  - delta or taker imbalance disagrees with continuation,
  - repeated breakout attempts that fail to progress.
- extension risk:
  - max observed extension through current candle from ORB profile_high/profile_low,
  - classify chase risk using observed distributions from prior analysis.
- structural interaction:
  - build pre-NY profile from 01:30-09:30 NY,
  - track pre-NY high, low, VAL, POC, VAH,
  - track structural sweep, rejection, acceptance, and value-area reclaim.

Pre-NY structural level rule:
- Use 09:45 NY open as the reference, not the later bias-decision price.
- If 09:45 open is inside pre-NY high/low, use pre-NY high and low as the first structural levels to observe.
- If 09:45 open is outside pre-NY high/low, use pre-NY VAL, POC, and VAH as structural reaction levels.
- If no useful structural level is reached, use synthetic adverse levels measured from 09:45 open:
  - 1% adverse,
  - 2% adverse.

Structural interpretation:
- For short bias:
  - adverse move is up.
  - If observing pre-NY value levels, watch VAL, then POC, then VAH.
  - Rejection from VAL or POC supports short continuation or a better short entry.
  - Acceptance above VAH, plus rejection of attempts back down, weakens or invalidates short premise.
- For long bias:
  - adverse move is down.
  - If observing pre-NY value levels, watch VAH, then POC, then VAL.
  - Rejection from VAH or POC supports long continuation or a better long entry.
  - Acceptance below VAL, plus rejection of attempts back up, weakens or invalidates long premise.
- If price sweeps pre-NY high/low and quickly reclaims against the sweep, mark structural rejection.
- If price accepts back inside pre-NY value area after a failed breakout, mark mean-reversion candidate separately from trend-following.

Sequential scoring:
- Start scoring at B1, before any final bias exists.
- Scoring is not the final strategy; it is a research feature to explain context.
- Latest events should carry more weight than older events.
- Suggested score primitives:
  - +2 accepted breakout/imbalance in one direction,
  - +1 clean directional OHLC sequence,
  - +1 pullback rejects ORB profile boundary and returns in direction,
  - -1 rejection only occurs at lower-quality internal value level,
  - -2 close returns into ORB value area after breakout,
  - -2 dirty breakout with opposite-color body and no follow-through,
  - -3 adverse structural sweep/reclaim against bias,
  - +2 adverse move reaches structural level and rejects back in bias direction.

Outputs:
- candle_context.parquet/csv with per-candle labels, OHLCV, delta, taker imbalance, body color, OHLC progression flags, extension, rejection/acceptance flags, structure touched, and score changes.
- context_events.parquet/csv with accepted/rejected/dirty/exhaustion/structural events.
- bias_decisions.csv showing when bias appears, current score, direction, and evidence chain.
- entry_context.csv showing whether the post-bias state is enter_now_clean_continuation, wait_for_retrace, wait_for_structure_rejection, avoid_dirty_breakout, avoid_exhaustion_warning, mean_reversion_candidate, or bias_invalidated_or_flipped.
- summary tables comparing raw ORB, old acceptance, stricter acceptance, and context-filtered decisions against corrected result labels.
- at least 3 latest manual-validation samples with WIB timestamps, including candles, ORB VP, pre-NY VP, structural reactions, bias decision, entry context, and corrected result benchmark.

Important constraints:
- Use 1-minute candles only; no sub-minute bars.
- Anchor all session logic to America/New_York local time, then convert display timestamps to WIB.
- Do not use 15:00 outcome data to build a signal. 15:00 data is benchmark/outcome only.
- Keep trend-following and mean-reversion labels separate.
- Do not call a context filter an edge unless later expectancy work proves it after costs and slippage.
```

## Frozen Decisions

- Result classification is frozen for now.
- The next research layer is ORB context quality, not a new result-label method.
- Only `I1_0930` is in scope unless explicitly changed later.
- Area A is the first 15 minutes of NY open.
- Area B/C/D are the only ORB observation areas for initial bias work.
- Bias and entry are separate decisions.
- Pre-bias scoring starts at B1.
- Post-bias observation is for entry timing, retrace waiting, structural reaction, or avoidance.

## Decision Process

1. Build Area A ORB VP.
2. Build pre-NY VP from 01:30-09:30 NY.
3. At 09:45 NY open, choose structural level set:
   - inside pre-NY high/low: observe pre-NY high/low,
   - outside pre-NY high/low: observe pre-NY VAL/POC/VAH,
   - no useful structure reached: observe 1% and 2% adverse levels from 09:45 open.
4. Start sequential candle scoring from B1.
5. Determine bias only when evidence is strong enough.
6. After bias appears, continue scoring for entry context.
7. Compare decisions against corrected result labels only after the signal decision is fixed.

## Interpretation

The research question is no longer only "did ORB choose the right direction?"

The better question is:

- Did the sequence produce a directional premise?
- Was the premise clean enough to enter immediately?
- If not, what level should be observed for retrace or structural rejection?
- Did the later candles confirm, weaken, invalidate, or convert the setup into mean reversion?

This is intended to explain cases where ORB direction has predictive power but direct entry has poor risk/reward, gets trapped, or enters after exhaustion.
