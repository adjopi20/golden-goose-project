# Deployment sources

Shared build templates and the public-feed Compose template are in
`utils/engine/deploy/`; the collector Dockerfile is beside its shared sources.
Historical model-specific templates remain with their declared legacy model.
Account configuration, persistent volumes and credentials remain operationally
separate. No image build, service activation, server migration or deployment occurred.

## Burst broker audit and performance alerts

The Binance audit service uses GET-only account access. It writes its own
`binance-broker-audit.sqlite` and records native fills, orders, conditional
orders, income and observed account position/order snapshots. The watcher
queries overlapping history windows and stores exchange payloads by native
ID. This provides the states observed at each poll within Binance`s history
retention. SQLite retains those observations after Binance expires them.

Run the module `utils.engine.monitoring.binance_audit` with `--report`,
`--database <archive>`, and a `--journal <path>` per execution journal to
compare native fills with journal records. The report links exchange/client
order IDs and identifies fills outside the journal or differences in native
values. It leaves the trading journals intact. Keep the archive and account
credentials on the server.

The engine notifier matches seven model contracts to the Monte Carlo baseline
in `burst-performance.json`. It checks cumulative net loss, maximum
closed-equity drawdown, and longest losing streak starting at 20 reconciled
closed trades. Returns use each trade`s net P&L including fills, fees and
funding divided by the strategy's own equity at entry. The simulated and live horizons
match by closed trade count. Above the source horizon, the monitor uses the
most recent source-length history. The alert starts above the shuffle mean
and also displays P95. The heading identifies model performance. A threshold
crossing sends an alert only.


ORDER_UNRESOLVED means a send, query or cancel needs reconciliation.
STOP_REPLACEMENT_REJECTED identifies a rejected trailing update and points
to the current protector and venue reason in the journal. Inspect the
worker's STOP rows and Binance algo orders by client ID before making any
manual change. An open session with an active guard stop can be recovered
by the same worker and journal. During rollout or rollback, keep that worker
on a version that understands guarded replacement until the session is
reconciled. A native stop can remain active while the worker is restarted.


## Release order and secrets

Use the source-only GitHub release branch and exact published commit as the
Biznet build input. The root AGENTS.md defines what belongs in the staged
tree. Verify the package pins, tests and resolved Compose structure before
push. Tag container images with the published commit. Keep the notifier
project separate from the market and trading projects so stopping the
trading stack leaves the notifier up.

The existing Burst secret directory supplies HYPE_BURST_SECRETS. Create
two directories on Biznet for the new account groups.

- QUIET_CORE_SECRETS points to
  /home/adjopi20/.config/golden-goose/quiet-core/futures
- QUIET_TAIL_SECRETS points to
  /home/adjopi20/.config/golden-goose/quiet-tail/futures

Quiet directories hold api-key and hmac-secret as separate plain-text files.
Enter their values directly on Biznet. The Burst profile uses api-key,
ed25519.pem and passphrase. Docker mounts credentials read-only under
/run/account-secrets. Use UID 1000 ownership, directory mode 0700 and file
mode 0600. Give each Binance key Futures permission and restrict it to the
Biznet egress IP. Keep one-way and single-asset mode, and isolated margin with
auto-add disabled on each assigned symbol. Fund each group only with the
collateral needed for its assets. The workers check distinct native
account UIDs and use their own virtual 100-unit starting balances. The
first UID bind for the existing Burst group uses a previously bound Burst
journal and its matching API key.

Enable the burst, quiet-core and quiet-tail Compose profiles independently
as their accounts pass preflight. Run the matching worker with --preflight
instead of --armed to check the broker settings without sending orders.
A fresh worker needs native market warmup before a signal can become eligible.
Start BTC MVRV via its own signal-monitor Compose template and signal volume.


## ADD fill reconciliation — 2026-11-15

The shared engine now derives ENTRY and ADD execution prices from the journal's
Binance trade fills, matched by order and session. A complete quantity must match
executedQty before the weighted price is used for addition-risk checks or shared
margin reservation. A missing avgPrice in a FILLED submission response is not an
execution failure. Reuse existing fills on restart; never resubmit a terminal ADD.
Incomplete ADD fills defer new shared-account admission. Native stop reconciliation,
trailing updates for a terminal ADD, and time/flat exits remain available while
fill history catches up. No quote price or zero-price fallback is permitted.

Offline regression includes all three ADD variants in both directions, persisted
response recovery without avgPrice, weighted partial fills, incomplete peer margin,
late fill history, and native protection through time exit. Existing order-race,
rejection, lost-acknowledgement and stop-replacement regression remains required.
The legacy paper package pin failures in test_frozen_models also reproduce on the
unmodified e30af19 baseline; they are outside this seven-model live release.


## Shared Binance REST budget: rollout and rollback

The portfolio Compose template and the Binance collector template require
`GG_BINANCE_REST_BUDGET_DIR` to point to the **same local, persistent** Linux
host directory. Set `GG_BINANCE_REST_GID` (default 19901) consistently. With all
services off this path initially, provision a setgid directory (mode 2770,
group = shared numeric GID) and an empty `binance.sqlite` file (0660, same
group). Verify both container users (worker UID 1000; collector UID 10001)
can create SQLite sidecars and update the file. Do not truncate an existing DB.
Do not use NFS or independent volumes. Do not run out-of-band Binance REST
tools on this host during cutover unless they use this file. A second egress
host would need a different coordinator design.

Reservations use SQLite rollback journaling. A write may wait up to 125 ms
for a lock per attempt. No lock spans HTTP. A restart or crash retains
committed reservations and venue cooldowns. Unanswered attempts keep a 30s
timeout allowance on top of their rolling window. A missing database,
contention timeout, or conflicting configuration defers admission before HTTP.
A response write retries once for SQLite contention. If it still fails, the
client retains the update and must persist it before sending another request.
This includes the original expiry from any venue cooldown. A cooldown that
never reached shared storage cannot inform peers. Check the SQLite error code
and timing in the logs and verify venue state during a storage outage.
Native accepted stops are held at Binance throughout.

Set the same `GG_BINANCE_REST_FUTURES_1M` in both Compose projects; a new
database defaults to 1920, with 1344 available to background work. If changing
caps later, coordinate all clients and update `rest_config` in place with
clients stopped and positions safely protected; retain cooldown and reservations.
The metrics table keeps roughly 24h by UTC minute/service/method/endpoint:
`reserved` count and weight, header maxima, `http_429`/`http_418`, transport
unknown, cooldown and deferral count/wait. `reserved` can include a crashed
attempt before it reached the wire. `http_*` is an observed response. Metrics
are buffered and written separately every five seconds while the client is
active. A process restart can lose an unflushed batch. Only the route and
configured worker labels are recorded.

1. Publish and verify the exact source commit on GitHub before building. Verify
   model pins, the rendered Compose mounts, and read/write access for every
   participating container UID/GID. Save the current image digests and effective
   configuration, including per-worker command overrides. Use GET-only Binance
   checks for account UID, signed position versus attributed fills, standard and
   algo orders, protective stop identity and exit deadline. Leave a lane on its
   current version while a mutation or stop replacement is unresolved or a
   time exit is imminent.
2. Roll workers one at a time with the same journal/account and one execution
   writer per symbol. For the initial transition from an uncoordinated image,
   pause or replace old audits to reduce background load. Monitor venue headers
   and 429 during the mixed rollout. On a compatible shared-limiter upgrade,
   preserve the existing coordinator database and confirm old and new images
   use the same path, schema and limits. Preserve collector volumes and NATS
   checkpoints and verify fresh market coverage after its restart. Roll the
   audit and monitor together if their heartbeat/freshness contract changes.
3. Use `--armed --manage-only` when a canary needs to keep reconciling and
   protecting positions while holding new exposure. A running or Docker-healthy
   process alone does not prove reconciliation is current. Check the last
   successful reconciliation, unresolved orders, actual venue position/stop,
   audit heartbeats and advancing capture, header headroom and cooldowns.
   Compare the SQLite phase/code and recovery time with venue HTTP status.
   A short SQLITE_BUSY before HTTP can recover on the next cycle. Repeated
   failures or increasing reconciliation age call for investigation.
4. Restore the intended mode explicitly after the checks pass. Removing
   `--manage-only` restores ENTRY and, for models that support it, ADD. Verify
   the effective override and running flags for every worker, not only the
   last one restarted. Confirm fresh reconciliation and protection after the
   switch. Setup and ADD deadlines stay in force during a pause.
5. For rollback, use the saved compatible image and configuration for the
   affected lane. Stop its current writer, retain its journal, check position
   and native stops, and verify the replacement reconciles. Retain the latest
   limiter state. If the rollback image predates shared budgeting, account
   for its uncoordinated traffic and keep old audit load low. Restore matching
   alert configuration if needed. Preserve protective stops and never replay
   a pending POST as a recovery shortcut.

See the [VPS rollout and recovery record](../../docs/operations/LIVE_EXECUTION_LESSONS.md#vps-rollout-and-recovery-record-2026-11-15-wib)
for deployed commits, final worker modes, and validation observed on the server.

## Binance private user-data shadow

The shadow overlay uses the existing NATS server. The base public configuration
stays the default. Burst, Quiet Core and Quiet Tail have separate NATS accounts
with no imports or exports. Publisher, consumer and provisioning credentials
are distinct. The publisher can write its event subject. The consumer can pull
its durable, acknowledge messages and read its stream/consumer status.

NATS 2.12.12 disables JetStream in the implicit global account (`$G`) when
explicit accounts opt in. The overlay sends existing unauthenticated public
clients to `GG_PUBLIC` using `no_auth_user`. Plan a separate maintenance window
for this broker cutover:

1. Pause the public publishers and consumers at a recorded checkpoint. Record
   stream sequence ranges and durable ACK floors. Use the NATS CLI to run
   `nats backup stream <stream> <backup-directory> --consumers` for each file-backed
   public stream. Keep the existing broker volume and original config available
   for rollback. Check free disk capacity for both stored copies and backups.
2. Restart the same broker with the overlay, retaining the Compose project and
   volume. While clients stay paused, restore each snapshot into `GG_PUBLIC`
   using `nats backup restore stream <backup-directory>` against that broker.
   The unauthenticated NATS connection selects `GG_PUBLIC` in this overlay.
3. Verify subjects, sequence ranges, durable ACK floors and the public
   publish/pull path before resuming. Use the existing public feed recovery
   procedure for any gaps during downtime. If restore or validation fails,
   restart with the original config and volume before resuming publishers.
   After writes resume, rollback needs a new checkpoint and reconciliation.
   Provision private streams only after the public path is healthy.

See the [NATS stream backup and restore guide](https://docs.nats.io/learn/backup-recovery/stream-backup-restore).

Build the shadow image with utils/engine/deploy/Dockerfile.burst-live and keep
the established Compose base file first when rendering it with the
private_shadow.compose.yaml overlay in utils/engine/deploy/market_feed.
Set GG_PRIVATE_SHADOW_IMAGE and GG_PRIVATE_RELEASE_SHA256 to the verified
image and matching shadow manifest digest. Point GG_PRIVATE_OBSERVED_DIR at the journal
directory and GG_BINANCE_REST_BUDGET_DIR at the existing coordinator, with
the same GID and budget settings. The observed journal bind is read-only.
Each consumer owns a separate shadow SQLite volume. Never mount its volume
over a trading journal.

The v12 image upgrades its existing shadow SQLite schema in place. Keep a
consistent backup of each shadow DB before switching consumers. Roll back to
v10 with the matching pre-upgrade shadow DB backup. A broker sequence gap after
restore requires a fresh baseline. Use the v12 image for the shadow roles only.

Supply the broker's external GG_PRIVATE_NATS_ENV_FILE with
GG_PUBLIC_NATS_FALLBACK_PASSWORD and unique high-entropy
GG_PRIVATE_{BURST,QUIET_CORE,QUIET_TAIL}_{MANAGER,CONSUMER,INIT} passwords.
Supply matching files named <account>.<role> in GG_PRIVATE_PASSWORD_DIR,
readable only by the intended user, and read-only Binance credential
directories via GG_PRIVATE_{BURST,QUIET_CORE,QUIET_TAIL}_BINANCE_DIR.
Burst expects the current Ed25519 layout and the Quiet groups expect HMAC.
Use exchange keys restricted to data access. The manager verifies the signed
UID against the journal binding on every new session. The audit must also be
attributed to that binding through the matching worker key hash. The manager
can use a distinct read-only key for the same UID. Keep broker access on the
trusted internal network, or configure TLS when crossing that boundary.

Use the three private-<account>-init services once to provision the fixed
GG_PRIVATE_V1 stream and gg_shadow_v1 durable inside each account. Existing
configurations must match the expected stream and consumer policy. Then the
private-shadow profile adds one manager and one consumer per account.
The manager uses the routed private WebSocket, renews its listen key at 30
minutes, checks ping/pong independently of account activity, and reconnects
on expiration, connection loss or planned rotation before 24 hours. It
verifies UID and obtains a budgeted position/open-order/conditional-order
snapshot on connect. It refreshes that snapshot roughly every 15 minutes at
background priority to recover the current view after overlap or gaps. It
never calls a trading endpoint or deletes the account's listen key.

Each account retains at most 50,000 messages, 32 MiB and 24 hours in its
JetStream namespace. Saturation rejects new publications. Missing heartbeat
or a stream sequence gap makes coverage incomplete. A new verified REST
baseline can recover the present view. Earlier missed events stay
unverified. The consumer stores a receipt before acknowledging, applies
fills once by account/symbol/trade identity, and tracks algo and child
order identities separately. Conflicts are diagnosed rather than applied as
new fills. A changed stream generation requires an explicitly fresh shadow
database and a new baseline. The consumer bounds local evidence retention to
two days and reports connection liveness, pending deliveries, timestamp lag,
snapshot overlap, REST comparison coverage and mismatch status. Late audit
evidence is treated as provisional. A persistent mismatch requires comparable
evidence to advance over at least two minutes.

Before reducing polling, collect a representative shadow observation that
includes flat and open positions, ENTRY/ADD/exit, partial fills, stop trigger
and child completion, venue and broker disconnects, restart before and after
ACK, and quiet-account periods. Confirm account and order attribution, zero
duplicate fills, explained mismatches, restored state after gaps, and measured
latency/backlog. Wallet balance updates are recorded as evidence. The current
audit does not contain a comparable wallet balance checkpoint. REST snapshots
are sequential reads, so an overlapping event or stale audit interval prevents
a state-match claim. No polling or live trade logic is changed by this phase.

Official references:
- [Binance USD-M connection and supported private events](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams)
- [Binance listen-key REST endpoints and weights](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/user-data-streams)
- [Binance event field definitions](https://pkg.go.dev/github.com/binance/binance-connector-go/clients/derivativestradingusdsfutures/src/websocketstreams/models)
- [NATS account isolation](https://docs.nats.io/learn/security/accounts-and-multitenancy)
