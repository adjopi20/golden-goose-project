# Live trading

Declared live submodels are ETHUSDC Pre-NY 15m, BTC MVRV fixed 0/7, and BTC MVRV
four-year P5/P95. This describes stage placement, not activation/server verification.

- live_model/pre_ny_eth/strategy.py: frozen decisions, indicators/profile/session;
  contract.json pins its strategy and engine. create_execution calls the shared
  ETH Futures lifecycle with explicit external account configuration and database.
- live_model/btc_mvrv/strategy.py: both related BTC branches share one strategy;
  model_config.json is pinned. This is a Telegram signal monitor, not a portfolio
  or exchange executor. Dedicated signal DB only; funded/trading DBs are rejected.
- HYPE, BNB and LTC paper packages are in paper_trading/models, not this stage.

From the project root, import only (no service is started):

```python
from live_trading.live_model import load_model, verify_release
verify_release()
eth = load_model('pre_ny_eth')
btc = load_model('btc_mvrv_fixed')  # same dual notifier as btc_mvrv_p5_p95
```

BTC has its existing run/once/report/health/backup CLI through
python -m live_trading.live_model.btc_mvrv.strategy. Running it can fetch/send;
no command was run during restructuring. Account state and Telegram credentials
are external. Model imports never open a database or contact a service.

Shared collector/adapters, monitoring, persistence and execution live under
utils/engine. Packages pin shared_engine_20261103_v3 (52 sources). The same public
feed can serve independent paper/live consumers; checkpoints and account state
must remain separate. Promotion moves the operational model package only and
starts a fresh live account, retaining the research copy.

The original six offline candidates/release are now research history at
model_development/models/legacy_frozen_candidates. Original frozen dependencies
still serve compatibility callers; current packages use pinned utils/engine.
Old deployment configurations are compatibility sources, not evidence of new
package activation or end-to-end service conversion. No Docker build/deployment,
account/state move, exchange order or Telegram message was performed.
