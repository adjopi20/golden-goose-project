# Binance paper collector on an Ubuntu droplet

This package runs the **model-specific** ETHUSDC, BNBUSDC and HYPEUSDT
Binance USDC-M public-data collector and evaluator. A second service collects
ETH, BNB and HYPE trades from Lighter's own perp markets in its own database.
Neither sends real orders.
The default command is collection/evaluation only; simulated fills require an
explicit `--paper` in a later, reviewed invocation. Lighter currently runs
venue-native public-feed observation only; its paper fills remain disabled.

## First deployment

On the laptop, commit and push the reviewed code. Do not commit `storage/`,
`tmp/`, `runs/`, SQLite files, or any credentials. On the Ubuntu server:

```bash
sudo apt update
sudo apt install -y git ca-certificates
# Install Docker Engine + Compose plugin using Docker's current Ubuntu instructions.
git clone https://github.com/adjopi20/golden-goose-project.git
cd golden-goose-project
export COMPOSE_FILE=models/trend_following_preny_profile_15m/deploy/compose.yaml
docker compose build binance-paper
# First initialization is run once, before the always-on worker.
docker compose run --rm --no-deps binance-paper --database /data/binance.sqlite --action warmup
docker compose up -d --no-build binance-paper
```

Use the SSH identity and repository authentication method you already control.
For a private GitHub repository, configure deploy-key/SSH access first; do not
paste a GitHub token into a command or commit it. Check Binance public Futures
access from the droplet before warmup (`curl -fsS https://fapi.binance.com/fapi/v1/time`).
If that endpoint is blocked, stop; do not use a network workaround.

Warmup fetches approximately 25 days of native 1-minute klines for indicators.
It does **not** reconstruct a full pre-NY aggregate-trade volume profile. Start
the collector before 01:00 New York time and allow it to collect a complete
01:00–09:00 window before treating decisions as ready. First-day NOT_READY is
expected. No raw trade archive is kept in SQLite.

## Logs and health

```bash
docker compose ps
docker compose logs --tail=100 binance-paper
docker compose logs -f --tail=100 binance-paper
docker compose exec binance-paper python -m models.trend_following_preny_profile_15m.paper_worker --database /data/binance.sqlite --action status
```

## Add Lighter observation alongside Binance

After pushing the Lighter worker changes, on the server's deployment branch:

```bash
git pull --ff-only
docker compose build lighter-observe
docker compose up -d --no-deps --no-build lighter-observe
docker compose ps
docker compose logs --tail=100 lighter-observe
docker compose exec lighter-observe python -m models.trend_following_preny_profile_15m.lighter_worker --database /data/lighter.sqlite --action status
```

Do not run Binance warmup in the Lighter volume. Lighter candles do not provide
historical aggressive buy/sell delta, so this service starts collecting its own
native trades and builds its own profile/minute bars over time. Entry evaluation
waits for 10 earlier native 09:00–12:00 sessions with usable minute coverage;
until then it records NOT_READY. ETH, BNB and HYPE native market IDs are checked
at startup.
On reconnect, the worker uses Lighter's trade-history pagination to recover
missed prints. If that history cannot bridge the gap, it resets coverage and
waits for a fresh, complete Pre-NY window before trusting a new profile.
Lighter trade IDs are monotonic for a market, not consecutive like Binance's
aggregate-trade IDs; the two feeds use different continuity checks.

The two services have independent named volumes and can run at the same time.
At 2 GB RAM, check `docker stats --no-stream` during the 09:00–12:00 New York
evaluation window. If memory pressure is high, stop and diagnose rather than
assuming the model ran accurately. This Lighter service does not generate
simulated fills yet; its fee schedule and order execution assumptions need a
separate parity test before enabling paper fills.

The worker writes compact structured evaluation/feed events to stdout. Docker's
`local` logging driver caps retained container logs at three 10 MB files. The
health check is read-only: ETH, BNB and HYPE feed cursors must each be no older
than 180 seconds. A missing/stale cursor marks the container unhealthy after
the startup grace period. `restart: unless-stopped` restarts a process that
**exits**, not a merely unhealthy process. An unhealthy state requires an alert
or manual investigation; do not assume it self-heals. Persistent failures,
missing profiles, or NOT_READY decisions should be diagnosed before paper fills.

## Safe update and recovery

```bash
git pull --ff-only
docker compose build binance-paper
docker compose up -d --no-deps binance-paper
docker compose ps
docker compose logs --tail=100 binance-paper
```

The named `binance_paper_data` volume preserves SQLite and checkpoint state
across container replacement. Never run two workers against that volume.
Do not run `docker compose down -v`: that removes the database volume.
The model recovers aggregate-trade ID gaps from Binance when possible; an
unrecoverable gap must be investigated, not silently skipped.

For a consistent SQLite backup while running, use the application's backup
API, then copy the resulting file **off the droplet**. For example:

```bash
docker compose exec -T binance-paper python -m live_engine.runtime --config models/trend_following_preny_profile_15m/deploy/binance.paper.yaml --database /data/binance.sqlite --action backup --backup-to /data/binance-backup.sqlite
docker compose cp binance-paper:/data/binance-backup.sqlite ./binance-backup.sqlite
```

Use a new backup filename each time; the API refuses to overwrite an existing
backup. A copy on the same droplet is not disaster recovery. Download it to
your laptop or external storage, and periodically test restoring into a
**separate** paper environment. Do not copy the live SQLite file by itself
while WAL writes are active.

## Scope and limitations

Each venue has one named volume containing three isolated virtual account
ledgers. No real API key or exchange order adapter is mounted.
No public ports are exposed. HYPE C1 calibration expired; without a new valid
venue-native artifact HYPE must remain NOT_READY for entry. Costs remain the
research proxy, not verified live Binance fees. This deploy is an observation
step, not approval for unattended real trading.
