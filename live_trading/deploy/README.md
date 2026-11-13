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

Each directory holds api-key, ed25519.pem and passphrase. The private key
and passphrase stay on Biznet. Docker mounts the directory read-only under
/run/account-secrets. Give the server user with UID 1000 access, with mode
0700 for the directory and 0600 for files. Use the Ed25519 public key when
registering each API key on Binance, with Futures permission and a Biznet
IP allowlist. Keep one-way and single-asset mode, and isolated margin with
auto-add disabled on each assigned symbol. Fund each group only with the
collateral needed for its assets. The workers check distinct native
account aliases and use their own virtual 100-unit starting balances.

Enable the burst, quiet-core and quiet-tail Compose profiles independently
as their accounts pass preflight. Run the matching worker with --preflight
instead of --armed to check the broker settings without sending orders.
A fresh worker needs native market warmup before a signal can become eligible.
Start BTC MVRV via its own signal-monitor Compose template and signal volume.
