# Paper deployment preparation — foundation only

**2026-10-02 update:** Binance collector/evaluator is running on the VPS. A
separate Lighter public trade collector/evaluator is available for deployment;
its historical delta references and paper fill parity have not yet been
validated. Use
[SERVER_RUNBOOK.md](SERVER_RUNBOOK.md) for the server steps and
[PAPER_FEED_V01.md](../docs/PAPER_FEED_V01.md) for local commands/readiness.
The foundation notes below predate these workers.

Shared infrastructure: `live_engine/`. Alpha remains in this model. Each venue
uses its own feed and SQLite volume. Neither worker sends real orders.
The two config files create separate venue ledgers, not actual market mappings.

Each model has **1,000 virtual units** for plumbing tests; this is not a real
capital allocation. ETH/BNB allow 0.25% or 0.5% risk, HYPE 0.5%. The generic engine
does not choose the tier: a causal model adapter must apply the contract's EMA
or ADX rule. Entry selectors likewise remain in existing research/model code.
TP1 settings: ETH 10% at 1R, BNB 10% at 2R, HYPE 10% at 1R. The remainder keeps
the original stop/time exit. These are not new exit choices.

Files use JSON syntax (valid YAML subset) to avoid another parser dependency.
Do not add YAML-only comments or syntax. Lighter symbol IDs/products/fees are
not verified. Both configs retain **4 bps research-proxy fees** for foundation
testing, not a claim about either venue's actual current fee schedule.

## Optional initialization / status

Run from the repository root. The database is outside OneDrive because an active
SQLite database should not be file-synced as though it were a static document.

```powershell
$paper = Join-Path $env:LOCALAPPDATA "golden-goose-paper"
foreach ($venue in @("binance", "lighter")) {
    .\.venv\Scripts\python.exe -m live_engine.runtime `
      --config "models/trend_following_preny_profile_15m/deploy/$venue.paper.yaml" `
      --database "$paper/$venue.sqlite" `
      --action init
    if ($LASTEXITCODE -ne 0) { throw "Paper initialization failed: $venue" }
}
```

Use `--action status` with the same arguments to inspect state. This **does not
start trading**. For backup, use `--action backup --backup-to <new-path>`; do not
copy just the live `.sqlite` file while WAL writes are active.

## Next gate, before unattended venue-native paper trading

1. Build/test public feed normalization and gap recovery separately per venue.
   Confirm product availability and trade-side semantics. No borrowed Binance
   profile/delta for Lighter.
2. Reuse `strategy.evaluate_session`, existing C2 annotation and `keep` selectors;
   implement causal warm-up/references and check signal parity on recorded data.
   Avoid treating the evaluator's partial-session NO_TRADE as final before noon.
3. HYPE C1 needs a valid pre-session training calibration. The last research
   artifact's validity ends 2026-09-27; do not silently extend it indefinitely.
4. Validate the Binance worker on the VPS, including feed freshness, alerting,
   deadline behavior, retention and restore drills. Health alone is not an
   alert; an external notification path is still required for unattended use.
5. The Docker/Compose package is observation-first. Do not treat its paper
   ledger as proof of real-order readiness.

Real orders, exchange reconciliation, protective-order acknowledgments, rounding,
funding and actual account/subaccount capital isolation are a separate gate.
Hermes is optional maintenance tooling, not part of strategy execution.
