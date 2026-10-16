# Ed25519 Testnet setup

No production orders are supported. This first check is read-only and uses a separate env file, Compose project and ledger from the alerts worker. Never paste an API key, PEM contents or passphrase into chat.

## Local authenticated status

Run from `models/bitcoin_onchain_cycles/live_mvrv`. Copy the template only if no Testnet file exists:

```powershell
if (-not (Test-Path -LiteralPath 'binance-testnet.env')) {
    Copy-Item -LiteralPath 'binance-testnet.env.example' -Destination 'binance-testnet.env'
}
```

Edit `binance-testnet.env` locally, not the alerts `.env`:

```env
BINANCE_ENV=testnet
BINANCE_KEY_TYPE=ed25519
BINANCE_API_KEY=YOUR_REGISTERED_TESTNET_API_KEY
BINANCE_API_SECRET=
BINANCE_PRIVATE_KEY_HOST_PATH=C:/path/to/btc_onchain_binance_testnet.pem
BINANCE_TESTNET_EXECUTION_ENABLED=false
INITIAL_CAPITAL=1000
TELEGRAM_ENABLED=false
```

The API key is the identifier returned by Binance after registering the public key. The host path points to the PRIVATE PEM, not the public PEM. Do not copy the PEM into the repo or Docker image. The override mounts the existing file read-only and refuses to create a missing source path. Prefer storing private keys outside cloud-synced folders; changing the path does not move the file.

```powershell
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml -f compose.binance-ed25519.yaml config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose configuration failed' }
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml -f compose.binance-ed25519.yaml build
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
docker compose --env-file binance-testnet.env -p btc-mvrv-binance-testnet -f compose.binance-testnet.yaml -f compose.binance-ed25519.yaml run --rm btc-mvrv-testnet status
```

If the key is encrypted, `status` asks for its passphrase directly in the interactive terminal with hidden input. Do not use `-T` for that check. An unencrypted key requires no passphrase. Wrong key type, unreadable file or invalid passphrase fails closed. `status` never submits orders, creates a strategy database or sends Telegram. Expected output identifies Testnet, BTCUSDT, account balances and unrelated open orders. Do not print expanded Compose config because it contains credentials.

## Encrypted key in an unattended worker

Do not start a worker yet. Interactive prompting is intentionally limited to `status`; `once` and `run` must not hang waiting for a password. For a later unattended encrypted-key setup:

1. Create a separate local passphrase file outside the repository/cloud sync, using a trusted editor, and restrict its filesystem permissions. This is a plaintext secret: a read-only mount is not encryption.
2. Set `BINANCE_PRIVATE_KEY_PASSPHRASE_HOST_PATH=C:/path/to/private.passphrase` in the Testnet env file. Do not store the passphrase itself in env.
3. Add `-f compose.binance-passphrase.yaml` after the Ed25519 override to each Compose command. It mounts only that existing file read-only.

Do not mount the passphrase file for an unencrypted PEM. For unencrypted PEMs, omit this override. Native Windows bind permissions and server permissions need checking separately. The worker caches the loaded signer for the process lifetime; changing keys requires a controlled restart and reviewed ledger/key migration, not silent rotation.

After read-only acceptance, follow the initialization and Testnet-only execution gates in [BINANCE_ADAPTER.md](BINANCE_ADAPTER.md), including these overrides on every command. Keep the execution switch false until explicitly proceeding with Testnet orders. No threshold edits or historical signal injection to force trades.

Protocol reference: [Binance Spot Test Network](https://testnet.binance.vision/). Ed25519 signs the exact URL-encoded payload and sends a base64 signature; no private key is sent to Binance.
