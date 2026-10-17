# ETHUSDC pilot — $145, Binance USD-M, server baru

Checkpoint 2026-10-17. **Preparation only, NOT live activation.** The existing
Pre-NY Binance/Lighter paper services and MVRV monitor remain unchanged.

## Decisions approved

- Pilot: one ETHUSDC model on 139.190.102.183. Other server stays paper.
- Allocated capital: 145 USDC. Futures account currently unused according to operator.
- Existing TF-PRENY-ETH-001 entry and EMA200 sizing: 0.25% or 0.5% of allocated
  equity (initial price-risk budgets 0.3625 / 0.725 USDC). Not 0.5% position value.
- Initial POC stop, 10% TP1 at 1R; remainder original stop / next-day 08:59 NY.
- Notional/equity cap 5, unchanged. No new indicators or entry filters.
- API key believed Ed25519; verify locally and with authenticated read-only calls.
- Having an execution-enabled API key is not authorization to send an order now.

## First gate: one-shot read-only preflight

Reusable code: `live_engine.futures_preflight` and
`live_engine.adapters.binance_futures_readonly`. The latter has only allowlisted
GET endpoints, no POST/cancel/transfer/leverage/margin mutation, and no retry.
Requests errors/raw venue error messages are redacted. It does not open any
paper database, subscribe to intentions, or change account state.

Checks: production authentication, account mode, all Futures positions, all
regular and conditional open orders, available **USDC**, ETHUSDC symbol filters,
current leverage/margin mode, commission rates, fresh BBO, and minimum quantity
that permits an exact 10% split. Public-only mode does not prove authentication.
An authenticated GET pass does not prove API TRADE permission or order execution.

Public venue snapshot checked through the new server on 2026-10-17: entry
MIN_NOTIONAL 20 USDC, LOT_SIZE and MARKET_LOT_SIZE step/minQty 0.001 ETH.
An exact 10% split therefore needs at least 0.010 ETH total / 0.001 ETH TP1.
At the observed ask 2406.23 this is about 24.06 USDC notional. The minimum-size
position fits the 0.25% budget only for stop distance up to about 1.51%, or
about 3.01% at 0.5% risk. These are illustrative feasibility bounds at that
snapshot, not new entry thresholds. Re-read live filters/quote; never hardcode
this price or assume this observation verifies the authenticated account.

The geometry report uses an exact-split quantity grid as a **feasibility bound**,
not an approved live sizing policy. Actual entry/POC, risk cap, native stop price
rounding, partial quantity rounding and margin must be validated per intention.
If a signal cannot fit the budget/venue rules, skip rather than enlarge risk.
No MIN_NOTIONAL assumption is imposed on reduce-only TP, but quantity grid and
minimum quantity still apply to entry, TP and remainder. Fee/slippage/funding are
outside the approved price-risk budget, as explicitly accepted by the operator.

`live_ready` is always false. `read_only_checks: pass` only clears this first gate.

## Secrets — only on server, never in Git/chat/history

Use a dedicated Futures key, no withdrawal permission, restricted to the new
server IP. Keep PEM/API-key/passphrase OUTSIDE the repository/image. The public
key matching this PEM must already be registered on Binance production with
Futures access; do not assume a Spot/Testnet registration is interchangeable.
Do not overwrite the SSH key or any existing MVRV credential.

Suggested files:

```text
/opt/golden-goose-secrets/preny-eth/binance-api-key.txt
/opt/golden-goose-secrets/preny-eth/binance-ed25519.pem
/opt/golden-goose-secrets/preny-eth/passphrase.txt  # only if encrypted
```

The API-key text file contains ONLY the API key, not `KEY=value`. Keep the PEM
exactly as exported. Set only these files' ownership to UID/GID 10001:10001 and
mode 400 so the non-root container can read them; keep the parent directory
root-owned mode 700. Never recursively change ownership of the project/server.
An unencrypted PEM uses `/dev/null` for the unused passphrase mount by default.
For an encrypted PEM, set GG_PRENY_PASSPHRASE_FILE to its restricted secret file.

Once reviewed code is committed/pushed and pulled on the server, run as root:

```bash
cd /root/golden-goose-project
export GG_PILOT_COMMIT=$(git rev-parse HEAD)
export GG_PRENY_API_KEY_FILE=/opt/golden-goose-secrets/preny-eth/binance-api-key.txt
export GG_PRENY_PRIVATE_KEY_FILE=/opt/golden-goose-secrets/preny-eth/binance-ed25519.pem
# Only if the PEM is encrypted:
# export GG_PRENY_PASSPHRASE_FILE=/opt/golden-goose-secrets/preny-eth/passphrase.txt
pilot=models/trend_following_preny_profile_15m/deploy/pilot.preflight.yaml
docker compose -f "$pilot" config --quiet &&
docker compose -f "$pilot" build eth-futures-preflight &&
docker compose -f "$pilot" run --rm --no-deps eth-futures-preflight
```

Use `config --quiet`, not expanded config; do not run a broad `up`/`down`, use
`--remove-orphans`, copy an active DB, or restart the existing paper services.
This one-shot container has no writable persistent volume. It reads real account
balances but does NOT fund a model ledger or place even a test order.
Return the resulting JSON only, never secret files. Exit 2 means a blocker/error;
do not repeatedly retry an authentication/rate-limit failure without diagnosis.

## Fixed remaining gates (not indefinite strategy research)

1. Account/quantity preflight above; explicitly approve the actual execution
   rounding policy, stop trigger basis and margin/slippage controls.
2. Durable real-order lifecycle: unique IDs, state committed before submission,
   authenticated position/fill reconciliation, confirmed native POC stop,
   partial fills, 10% TP and time exit; funding/fees ledger.
3. Recovery tests for lost response, disconnect and restart while open. Never
   blind-resubmit an unknown order; never treat a public print as a real fill.
4. One active live writer per account. Both servers must not trade the same
   account concurrently. Actionable alerts, consistent backup and restore drill.
5. Separate explicit authorization for the first real-money pilot transaction.

The current paper AccountStore/AccountOwner are intentionally NOT converted into
live stores. No automated live adapter or live entry service is supplied by this
preflight checkpoint. Waiting alone cannot complete gates 2–5.

## Validation of this preparation

- 24 preflight tests pass, including Ed25519 signatures, no order endpoints,
  redacted transport errors, quantity-grid intersection, exact TP feasibility,
  insufficient USDC, foreign positions, regular/conditional orders, stale quotes
  and rejected contract changes. Same 24 tests pass after installation locally.
- Compose configuration validates. Docker image build, authenticated production
  access, real-order lifecycle and recovery are NOT yet validated by this change.
- Expanded existing regression: 107 pass, 1 pre-existing failure in
  `test_pure_runtime_and_core_have_no_io_dependencies`: runtime/observer.py imports
  `live_engine.state_store.encode`. This import exists in base commit 38383bd;
  no evaluator/observer code was changed. Record for separate boundary cleanup,
  not as a newly diagnosed trade/financial bug or a reason to erase ledgers.
- No API credentials were read, no real/test order sent, no server service
  restarted. The only server check for this work used public market endpoints.

## References checked 2026-10-17

- [Official USD-M SDK: Ed25519 REST support](https://github.com/binance/binance-connector-python/blob/master/clients/derivatives_trading_usds_futures/README.md)
- [Futures account fields/configuration](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V3)
- [Exchange information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)
- [Conditional order query / trading endpoints](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order)
- [General security, timeouts and rate limits](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)
