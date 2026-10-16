# Strategy: Fixed MVRV 0/7 v1

- Asset/style/timeframe: BTC, spot long-or-cash cycle valuation, completed daily UTC observation.
- Hypothesis: sufficiently low absolute valuation offers entry opportunities; extreme high valuation offers an exit. Future cycle recurrence is not guaranteed.
- Entry: account is cash, no pending action, valid current daily data, Z < 0. Strict inequality, no crossing requirement.
- Exit: account holds BTC, no pending action, valid data, Z >= 7.
- Position size: 100% of this account's cash on entry, 100% of this account's attributed BTC on exit. Account initially receives 50% of system capital; profit compounds locally.
- Price SL / hard loss cap / trailing / partial TP / time stop: none, deliberately consistent with the research. Exit is the indicator signal only.
- Risk: unleveraged, no shorts, BTC exposure may be full account value; large mark-to-market DD and multi-year underwater periods possible. No tactical market/regime filters have been added.
- Execution/data/fees/guards: shared frozen `CONTRACT.md`; paper and manual-alert modes are distinct.
- Historical evidence: local `percentile_p5_20261003` research and release verification; retrospective history, not untouched OOS. No deployed forward trade statistics yet. Not enough independent cycle trades to claim stable edge.
- Continue/review/retire: paper acceptance and operational criteria from `CONTRACT.md`; no automated market-risk rule change.
- Dependencies: shared Coin Metrics data, causal Z calculation, common engine and durable ledger. No CVDD, NUPL or LLM.
- Change log: 2026-10-03 v1, operator-selected fixed benchmark.
