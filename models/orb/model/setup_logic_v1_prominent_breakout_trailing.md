# ORB Setup Logic V1 - Prominent Breakout With Trailing Runner

Status: frozen working setup definition. Not yet proven as an edge.

Purpose: define a no-lookahead setup evaluator that can harvest clean ORB expansion moves like 2026-05-31 without hardcoding future targets.

## Core Principle

Bias and entry are evaluated together as one setup state. A breakout starts a candidate setup, but entry is allowed only when the breakout quality is strong enough or when a valid retest/rejection confirms acceptance.

Do not use fixed `TP2 = 4R` unless it comes from pre-known structure or a tested historical distribution. For now, use a trailing runner after TP1.

Bias scoring starts before a bias is declared. The purpose of scoring is to detect which side is beginning to win, not to justify a bias after the fact. After a bias exists, the same evaluator continues candle by candle, but the objective changes from bias discovery to entry quality, pullback quality, and risk placement.

Bias is not entry. A session can have a valid directional read but no immediate trade if the breakout is extended, low quality, or likely exhausted.

## Inputs

- 1-minute OHLCV only.
- `buy_volume`, `sell_volume`, `delta`, `taker_imbalance`, `trade_count` when available.
- Area A ORB volume profile from `09:30-09:45 NY`:
  - `profile_low`
  - `VAL`
  - `POC`
  - `VAH`
  - `profile_high`
- Sequential candles from `09:45 NY` onward.
- Rolling candle statistics computed only from already-closed prior candles.

## Price Location Score

Use POC as the neutral center:

```text
above profile_high      = +3
VAH zone                = +2
above POC inside VA     = +1
POC area                =  0
below POC inside VA     = -1
VAL zone                = -2
below profile_low       = -3
```

This score is context only. It must not bury recent breakout evidence forever.

## Required Scores

The engine must keep separate scores:

```text
background_score    = whole observed context
recent_score        = last 3-5 candles or decay-weighted score
active_event_score  = only since current breakout/imbalance event began
breakout_quality    = prominence of the breakout/follow-through candle
extension_risk      = whether the move is tradable or already chase-risk
activity_score      = whether the market is active enough to trade
```

Cumulative/background score is context. Recent score and active event score decide whether a new side is taking control.

Use decay or explicit windows so old balance does not bury a new breakout. Example:

```text
background_score: all candles since 09:45 NY
recent_score: last 3-5 candles
active_event_score: candles since current event started
```

If price spends 18 candles rotating inside value and then produces a 3-candle clean breakout, the recent score and active event score must be able to dominate the old background score. Background weakness can lower confidence, but it must not block recognition that market activity has started to pick a side.

## Bias Decision State

The evaluator maintains these states:

```text
NO_BIAS
CANDIDATE_UP
CANDIDATE_DOWN
BIAS_UP
BIAS_DOWN
EXHAUSTION_WARNING
STRUCTURAL_PULLBACK_WAIT
STRUCTURAL_REJECTION_ENTRY_READY
NO_TRADE
```

State transitions are sequential and candle-based:

```text
1. Start after Area A profile is complete.
2. Score every candle from 09:45 NY onward.
3. A breakout or imbalance can create a candidate bias.
4. A candidate becomes bias only if recent/event quality confirms it.
5. Bias does not force an entry.
6. If quality is poor or extension is high, wait for retest or structural pullback.
7. If the opposite side accepts beyond important structure, downgrade or invalidate the bias.
```

Current preferred tightening:

```text
Old acceptance: 4 total event candles without invalidation.
New quality requirement: 3 total candles can confirm, but they should show directional OHLC progression.
```

For short:

```text
candle_1 OHLC >= candle_2 OHLC >= candle_3 OHLC
```

For long:

```text
candle_1 OHLC <= candle_2 OHLC <= candle_3 OHLC
```

Opposite-color breakout candles are allowed as information, but they reduce quality. Example: a `breakout_up` candle that closes red is not clean trend evidence even if the body is above `profile_high`.

## Candidate Setup Start

For short:

```text
armed_breakout_down warns.
breakout_down starts when candle body is fully below profile_low.
```

For long:

```text
armed_breakout_up warns.
breakout_up starts when candle body is fully above profile_high.
```

Armed candles alone do not permit entry.

## Prominent Follow-Through Entry

Entry is allowed when an active breakout is followed by a prominent continuation candle.

Short requirements:

```text
1. active breakout_down exists.
2. body remains fully below profile_low.
3. no reclaim of profile_low after breakout starts.
4. follow-through candle close is below prior breakout bodies.
5. range_z >= 2.0.
6. body_z >= 1.5.
7. volume_z >= 1.5.
8. delta is negative.
9. close is in the lower part of the candle.
10. extension is not already classified as chase-risk.
```

Long requirements are mirrored:

```text
1. active breakout_up exists.
2. body remains fully above profile_high.
3. no reclaim of profile_high after breakout starts.
4. follow-through candle close is above prior breakout bodies.
5. range_z >= 2.0.
6. body_z >= 1.5.
7. volume_z >= 1.5.
8. delta is positive.
9. close is in the upper part of the candle.
10. extension is not already classified as chase-risk.
```

Entry is at the next 1-minute candle open after the prominent follow-through candle closes.

## Retest Entry Alternative

If breakout is accepted but extension is already large, do not chase. Wait for retest.

Short retest entries:

```text
valid retest levels:
- profile_low
- VAL
- breakout candle open
- last failed-reclaim high

entry trigger:
- price retests one of those levels,
- rejects upward attempt,
- closes back in breakout direction.
```

Long retest entries are mirrored:

```text
valid retest levels:
- profile_high
- VAH
- breakout candle open
- last failed-reclaim low

entry trigger:
- price retests one of those levels,
- rejects downward attempt,
- closes back in breakout direction.
```

## Structural Pullback Continuation Entry

After a first trade exits or after an accepted bias becomes too extended to chase, keep the same directional bias but stop looking for immediate continuation. Wait for price to pull back into known structural levels.

Use structural levels that are known before the re-entry:

```text
pre-NY session profile: 01:30-09:30 NY
previous NY 8h block: 09:30-17:30 NY
Asia/Japan 8h block: 17:30-01:30 NY
Europe 8h block: 01:30-09:30 NY
```

For implementation, convert those windows to WIB for display only. The anchor remains NY time.

Observe:

```text
high
low
VAL
VAH
POC
```

Which pre-NY structural set to use:

```text
If the 09:45 NY open is inside the pre-NY high/low:
  use pre-NY high and low as primary structural levels.

If the 09:45 NY open is outside the pre-NY high/low:
  use pre-NY VAL, VAH, and POC as pullback/acceptance levels.
```

Also track the low of each prior 8h block for short continuation after a downside move, and the high of each prior 8h block for long continuation after an upside move. The closest structural level to the first move's MFE zone is usually the first pullback level to observe.

For short continuation:

```text
1. Existing bias is down.
2. Price pulls back upward into a known structural level or structural band.
3. Do not short just because it touched the level.
4. Wait for failed acceptance above the level.
5. Re-entry requires close back below the level, lower-low follow-through, and preferably a full body back below the level.
```

For long continuation, mirror the logic.

Valid short re-entry examples:

```text
price reclaims structural low
then closes back below structural low
then makes lower low
then full body remains below structural low
entry at next 1-minute open
```

Valid long re-entry examples:

```text
price reclaims structural high from below
then holds above structural high
then makes higher high
then full body remains above structural high
entry at next 1-minute open
```

The structural pullback engine uses two stop concepts:

```text
thesis invalidation stop:
  wider stop beyond the full pullback swing high/low.

execution invalidation stop:
  tighter stop beyond the failed-reclaim / failed-reject candle that created the re-entry.
```

The execution stop is preferred for backtesting if it is known before entry and actually invalidates the re-entry premise.

## Acceptance, Rejection, And Confidence

Acceptance and rejection are not binary only. Track quality.

Acceptance improves when:

```text
body remains beyond the level
pullback rejects the level and returns in bias direction
OHLC progresses in the bias direction
delta/taker imbalance supports the direction
volume expands with follow-through
```

For up bias, higher rejection levels are stronger:

```text
profile_high rejection > VAH rejection > POC rejection > VAL rejection
```

For down bias, lower rejection levels are stronger:

```text
profile_low rejection > VAL rejection > POC rejection > VAH rejection
```

Confidence decreases when:

```text
breakout candle is opposite color
body returns inside value area
OHLC fails to progress after breakout
new high/low has poor follow-through
large volume creates no continuation
price sweeps a structural level against the bias and accepts the other side
```

Rejection pattern:

```text
1. Price pierces or closes beyond a level.
2. Next 1-3 candles fail to continue.
3. Price closes back on the original side.
4. Follow-through returns in the bias direction.
```

Acceptance pattern:

```text
1. Price crosses the level.
2. Bodies stay beyond the level.
3. Pullbacks into the level fail.
4. Follow-through extends in the same direction.
```

This layer scores setup quality. It does not replace the ORB direction engine.

## Stop Logic

Use the closest valid structure that invalidates the setup.

For short:

```text
SL above last failed-breakdown / armed-breakout high.
If unavailable, SL above profile_low/profile boundary reclaim level with buffer.
```

For long:

```text
SL below last failed-breakout / armed-breakout low.
If unavailable, SL below profile_high/profile boundary reclaim level with buffer.
```

The stop must be known at entry. Do not use future MFE/MAE to define risk.

## Take Profit And Runner

For now:

```text
TP1 = 2R.
At TP1, close 50%.
Runner uses trailing TP only.
No fixed TP2.
```

After TP1:

```text
1. Move runner protection to breakeven or latest valid structure.
2. For short, trail behind lower-high structure.
3. For long, trail behind higher-low structure.
4. Optional predefined climax exit:
   - body_z >= 3.0
   - volume_z >= 5.0
   - close remains in breakout direction
```

The climax exit rule is allowed only if it is defined before testing. It must not be added after seeing a target candle.

## No-Trade Filters

Do not trade lonely markets:

```text
Area A range too small.
Area A volume too low.
B/C/D trade_count too low.
current candle range_z too low.
current candle volume_z too low.
```

Exact thresholds must be tested. Until then, market activity score is a feature, not a hard edge claim.

## May 31 2026 Reference Example

This example validates the intended decision flow, not final profitability.

Area A profile:

```text
profile_low  = 9.007
VAL          = 9.0138
POC          = 9.024
VAH          = 9.0242
profile_high = 9.027
```

Sequence:

```text
B3 20:47 WIB: armed_breakout_down
B4 20:48 WIB: breakout_down starts
B5 20:49 WIB: breakout_down continues
B6-B7: body remains below profile_low, no reclaim
B8 20:52 WIB: prominent follow-through candle
B9 20:53 WIB: short entry at next open
```

B8 quality:

```text
range_z  ~= 4.53
body_z   ~= 3.66
volume_z ~= 5.76
delta    = -785.34
body fully below profile_low
close near lower part of candle
```

Trade plan:

```text
entry = 8.989
SL    = 9.017
risk  = 0.028
TP1   = 8.933
```

SL reason:

```text
B3 high = 9.016.
SL = B3 high + small buffer = 9.017.
If price reclaims above that high, the short breakout premise is wrong.
```

Runner:

```text
After TP1, close 50%.
Runner trails only.
No fixed 4R target.
```

The 21:18 WIB move is captured only if the predefined runner/trailing or climax-exit rule keeps the runner open. Do not hardcode `4R` from this example.

## May 31 2026 Continuation Example

This example defines how to evaluate the second move after the first short trade exits.

Known structural levels before the re-entry:

```text
previous NY block low = 8.896 at 2026-05-30 20:36 WIB
Asia/Japan block low  = 8.900 at 2026-05-31 06:44 WIB
Europe block low      = 8.939 at 2026-05-31 17:22 WIB
```

The relevant pullback band is `8.896-8.900`, because it is closest to the downside MFE area and price later pulls back into it.

Sequence:

```text
22:23 WIB: price reaches/reclaims 8.900.
22:24-22:26 WIB: pullback continues; high prints 8.921.
22:40 WIB: candle closes back below 8.900.
22:41 WIB: lower low and close below 8.900.
22:42 WIB: full body below 8.900 confirms acceptance back below structure.
22:43 WIB: short re-entry at next open.
```

Trade plan using execution invalidation:

```text
entry = 8.885 at 22:43 WIB open
SL    = 8.905
risk  = 0.020
TP1   = 8.845
```

SL reason:

```text
22:40 high = 8.904.
SL = 22:40 failed-reclaim high + small buffer = 8.905.
If price reclaims above that failed-reclaim high after accepting below 8.900, the re-entry premise is wrong.
```

Alternative wider thesis stop:

```text
SL = 8.922
Reason: above the full pullback swing high at 22:26 WIB.
```

Runner:

```text
TP1 2R is hit at 8.845.
After TP1, close 50%.
Runner trails only.
No fixed 4R target.
```

Allowed predefined runner exits:

```text
trail behind lower-high structure, or
exit on extreme climax only if the rule existed before the test:
  range_z >= 5.0
  body_z >= 4.0
  volume_z >= 4.0
  close remains in breakout direction
```

On this sample, 23:33 WIB qualifies as an extreme downside climax:

```text
range_z  ~= 6.94
body_z   ~= 6.04
volume_z ~= 4.13
close    = 8.814
low      = 8.795
```

Use the close for a deterministic climax exit. Do not use the low unless the exit rule is a resting limit or trailing rule that would actually execute there.

## Next Test

Build a new isolated run that tests:

```text
prominent_followthrough_entry
TP1 2R 50%
trailing runner
no fixed TP2
no fees/slippage first
then fees/slippage later
```

Compare against:

```text
old through_D backtest
first accepted event backtest
context-quality diagnostic run
```
