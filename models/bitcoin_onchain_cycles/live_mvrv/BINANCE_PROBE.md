# Virtual Testnet execution acceptance

This is NOT a strategy, backtest or OOS performance record. It creates two explicitly labelled probe BUY/SELL intents, not fabricated MVRV signals. It reuses the actual order sizing, durable intent, reconciliation, native BTC/USDT fee and cost-basis code. No production orders are allowed.

## Scope and isolation

- Dedicated fresh probe database/volume; at most 50 virtual USDT, split 25:25 for branch attribution.
- The exchange account is shared. Stop the existing Testnet worker first and leave it stopped until restore succeeds. Extra API keys on the same venue account do not create separate balances.
- Preflight refuses strategy errors, pending strategy actions/orders, inventory mismatch, insufficient surplus and an active earlier probe.
- Strategy online backup is saved before arming. All accounts/actions/decisions/fills/marks/raw/vintages/venue ledger hashes are frozen. Strategy thresholds and its 500:500 capital are not changed.
- A persisted `active_testnet_probe` guard blocks the updated worker if someone accidentally starts it during the test. Do not operate an old worker image or manually trade through another client while the probe is active.
- Buy spends 99% of each probe branch cash (49.50 USDT maximum total), not the faucet BTC or strategy BTC. Sell sells only acquired probe holdings, rounded to venue rules. Residual BTC dust is recorded, not reported as zero.
- Probe changes shared account totals. After all four orders reconcile and no tradable probe BTC remains, `restore` verifies exact balance deltas against the fee ledger, then changes ONLY the strategy's unallocated-surplus metadata with an immutable audit record and outbox notification. It never changes strategy trade/mark history or branch wealth. Repeat restore is idempotent.
- Missing/unknown order, failed inventory checks or incomplete phases HALT. Do not delete DBs/volumes, rerun init, refresh due times, increase budget or blindly resend. Keep the worker stopped, inspect and reconcile.

## Commands (local PowerShell)

Replace existing strategy volume with the name verified in Docker, and choose a NEW project name for every new probe. Do not modify `INITIAL_CAPITAL` in the normal Testnet env file. The commands enable execution only for buy/sell disposable containers; this remains virtual only. The current local key is Ed25519. Encrypted PEMs need a separate passphrase-file mount before unattended probes; this template does not mount one automatically.

```powershell
$env:MVRV_STRATEGY_VOLUME = 'btc-mvrv-binance-testnet_binance_testnet_data'
$probeCompose = @('--env-file','binance-testnet.env','-p','btc-mvrv-probe-NEW-ID','-f','compose.binance-probe.yaml')

# Prevent shell Telegram overrides from changing the resumed worker's bot.
Remove-Item Env:\TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue

docker compose @btcCompose stop btc-mvrv-testnet
docker compose @probeCompose run --rm btc-mvrv-probe init --confirm-virtual-testnet --capital 50
```

Run buy with optional deliberate client-side response loss. Exit code 2 and an UNKNOWN-outcome diagnostic are expected for this one command; a real buy may have filled. Never treat the diagnostic as a failed exchange order and send a replacement order:

```powershell
docker compose @probeCompose run --rm -e BINANCE_TESTNET_EXECUTION_ENABLED=true btc-mvrv-probe buy --confirm-virtual-testnet --simulate-lost-buy-response
docker compose @probeCompose run --rm btc-mvrv-probe verify --confirm-virtual-testnet
docker compose @probeCompose run --rm -e BINANCE_TESTNET_EXECUTION_ENABLED=true btc-mvrv-probe buy --confirm-virtual-testnet
docker compose @probeCompose run --rm btc-mvrv-probe verify --confirm-virtual-testnet
```

Each disposable container is a fresh process reading the same persistent probe DB. After the buys reconcile, repeat buy once and compare order/trade IDs and holdings: no extra order or fill is permitted. Do not use the lost-response flag again. Then:

```powershell
docker compose @probeCompose run --rm -e BINANCE_TESTNET_EXECUTION_ENABLED=true btc-mvrv-probe sell --confirm-virtual-testnet
docker compose @probeCompose run --rm btc-mvrv-probe verify --confirm-virtual-testnet
docker compose @probeCompose run --rm btc-mvrv-probe backup --confirm-virtual-testnet --destination /data/after_sell_backup.sqlite
docker compose @probeCompose run --rm btc-mvrv-probe verify --confirm-virtual-testnet --database /data/after_sell_backup.sqlite
docker compose @probeCompose run --rm btc-mvrv-probe restore --confirm-virtual-testnet
```

Resume ONLY after restore and inventory checks succeed:

```powershell
docker compose @btcCompose up -d btc-mvrv-testnet
docker compose @btcCompose exec btc-mvrv-testnet python -m live_mvrv.binance_main report
```

Inspect report and healthy heartbeat after resume. Original accounts must still be 500:500 if no strategy signal has occurred. The probe and backup volumes are retained for audit. Never use `down -v`. An actual Testnet roundtrip validates transport/accounting mechanics, not production liquidity/slippage or financial edge. Real network timeout/partial-fill/cancel outcomes remain separate from simulated client response loss.
