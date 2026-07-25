# Out-of-Sample Validation

Frozen boundary: `2025-04-13`.

As checked on `2026-07-25`, the supplied BTC price, market-cap, and MVRV data
are jointly complete through `2026-05-23`, giving 406 available OOS days.
Daily CDD continues through `2026-07-24`, but CDD alone cannot extend the full
strategy dataset.

The paper rules generated no new NUPL or MVRV buy/sell transition during those
406 days. All six strategies carried their already-open BTC position forward,
so their OOS return equals buy-and-hold for this interval.

Thresholds and allocation rules must be frozen before this track is scored.
