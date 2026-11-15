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

1. Review and publish the exact commit; build from it and verify all package
   pins, rendered Compose mounts, UID/GID access, and coordinator behavior
   without trading. Save original image digests and configuration. Inventory
   existing positions, native stop (including guard/child state), pending orders
   and account identity read-only. Do not cut over a lane with an unresolved
   mutation, a stop replacement in flight, or an imminent time exit without
   resolving it under the current version first.
2. Pause/replace the old audits first to relieve background load; keep
   monitor and native stops running. Roll the collector without dropping
   its volume or NATS checkpoints; check that coverage is fresh. Canary one
   worker at a time with `--armed --manage-only`, the same journal and account,
   never two writers for a symbol. Confirm the exact native position, protector,
   fill attribution and exit timer before moving to peers. Mixed old/new
   versions cannot share the local counter, so keep audit load off and watch
   venue headers and 429 during this short period.
3. Once seven lanes and the collector reconcile, restore audits with the
   new heartbeat/freshness config and deploy the matching monitor config.
   Check operation for several venue minutes, including DB deferrals,
   `CAPTURE_STALE`, `RECONCILIATION_STALE`, unconfirmed stops and header
   headroom. Remove `--manage-only` only after current coverage, no unknown
   mutations and operator review. The original setup deadlines remain in force.
4. Roll back a failing lane one at a time to its saved **b901f9c** image
   (contains the ADD fix) and exact original command/mounts. Quiesce the
   replacement writer, keep and reconcile the latest journal/position/STOPs,
   and verify the old worker actually takes over. Keep the low-priority audits
   paused if the old image would overload the IP. Restore the old monitor
   config when restoring old audits. Never reset/relabel account data, replay
   pending POSTs, or cancel existing protective stops just to roll back.
   If still banned or protection is unclear, native protection alone is not
   evidence that a timed or manual exit is available; follow operator recovery.
