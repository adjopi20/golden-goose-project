# ORB V2 Dual Entry Models

Status: research extension on top of frozen V2 setup evaluation.

Do not change the V2 bias or confirmation rules. ORB still establishes the directional bias, and candle confirmation plus closed-candle delta still gate entry quality.

## Entry Models

### Trend Following

- Reference levels: current NY ORB profile from `09:30-09:45 NY`.
- Entries: prominent ORB breakout, ORB profile retest, structural reclaim/rejection in the ORB bias direction.
- TP1: default V2 `4R`, close 50%.
- Runner: trail 1R after TP1.

### Mean Reversion

- Reference levels: highs/lows of the three completed sessions before current NY open:
  - previous NY `09:30-17:30`,
  - overnight `17:30-01:30`,
  - pre-NY `01:30-09:30`.
- Bias does not flip. Mean reversion is only a different entry model in the existing ORB bias direction.
- Use only when there is a premise: prior range/balance, price sweeps opposite-side structural levels, fails to accept beyond them, then rejects back in the ORB bias direction.
- Entry still needs the same setup evaluation style: closed candle rejection/reclaim plus confirmation-window delta agreeing with the ORB bias.
- TP1: prior-24h volume-profile POC from `09:30 NY` previous day to just before current `09:30 NY`, close 50%.
- After TP1: protect the remaining 50% by moving SL to entry.
- TP2 research target: prior-24h VAL for short bias, prior-24h VAH for long bias.

Mean reversion must stay reported separately from trend following.
