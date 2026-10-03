# Binance paper collector on an Ubuntu droplet

This package runs the **model-specific** ETHUSDC, BNBUSDC and HYPEUSDT
Binance USDC-M public-data collector and evaluator. A second service collects
ETH, BNB and HYPE trades from Lighter's own perp markets in its own database.
Neither sends real orders.
The default command is collection/evaluation only. `paper.simulation.yaml`
enables simulated fills on the existing SQLite ledgers. It does not add real
exchange orders or credentials.

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

## Simulated fills after updating both workers

The Binance HYPE C1 artifact was fitted to 123 Binance HYPE Pre-NY baseline
candidates from 2026-03-01 through 2026-08-30, using pre-entry features only.
It is valid from 2026-10-02 through 2026-10-31. Lighter cannot use that artifact;
HYPE there remains unready until a Lighter-native calibration exists. ETH and
BNB on Lighter need 10 earlier native 09:00-12:00 sessions with usable coverage.
HYPE starts recording its own C1 pre-entry features as soon as its native
profile and completed-hour ATR are available. Once at least 25 earlier HYPE
candidates exist, freeze their 180-day feature medians for a new 30-day window:

```bash
docker compose exec -T lighter-observe python -m models.trend_following_preny_profile_15m.calibrate_c1_native \
  --database /data/lighter.sqlite --venue lighter --valid-from YYYY-MM-DD \
  --output /data/lighter-hype-c1-YYYY-MM.json
```

Use the next NY session date for `--valid-from`; the artifact refuses to use
candidates from that date or later. Add `--c1-calibration` with that exact
path to the Lighter command in `paper.simulation.yaml`, rebuild/recreate only
the Lighter service, then inspect its status. The Binance artifact is never
used for Lighter.

From the repository root on the deployment branch, build the updated shared
image and recreate the two existing services. Their named volumes persist:

```bash
git pull --ff-only
docker compose -f models/trend_following_preny_profile_15m/deploy/compose.yaml \
  -f models/trend_following_preny_profile_15m/deploy/paper.simulation.yaml config --quiet
docker compose -f models/trend_following_preny_profile_15m/deploy/compose.yaml \
  -f models/trend_following_preny_profile_15m/deploy/paper.simulation.yaml build binance-paper
docker compose -f models/trend_following_preny_profile_15m/deploy/compose.yaml \
  -f models/trend_following_preny_profile_15m/deploy/paper.simulation.yaml up -d --no-deps --no-build binance-paper lighter-observe
docker compose -f models/trend_following_preny_profile_15m/deploy/compose.yaml \
  -f models/trend_following_preny_profile_15m/deploy/paper.simulation.yaml ps
```

Check each service's `--action status` and confirm `execution_mode: simulated`.
The original completed 15-minute candle remains the signal time. The simulated
entry becomes eligible only when evaluation finishes and only within the
original 5-minute deadline. Entry, TP1, POC stop and next-day time exit use
the next native public trade print as a proxy fill. Pending candidates,
positions, triggers, fills and closed equity persist across worker restarts.
Historical candidates cannot create retrospective paper fills.

The two services have independent named volumes and can run at the same time.
At 2 GB RAM, check `docker stats --no-stream` during the 09:00–12:00 New York
evaluation window. The Lighter paper fills are proxies from its next public
trade, with the existing 4 bps research fee assumption. They do not represent
verified executable bid/ask fills or Lighter's actual fee schedule.

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
No public ports are exposed. Binance HYPE has a dated calibration artifact;
Lighter HYPE needs its own native one. Costs remain research proxies, not
verified venue fees. This deploy is paper simulation, not real trading.
