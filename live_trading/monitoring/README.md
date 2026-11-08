# Monitoring sources

Shared health checks and supported ledger alerts are in `utils/engine/monitoring/`.
Operational target/account configuration remains specific to its environment.

## Burst live recovery

- **A symbol loses its feed or its execution cycle fails.** Pause its entries
  while continuing to manage open positions and reconcile the journal. Other
  symbols can keep running while their own feeds and account checks pass.
  Repaired and stale data cannot generate entries.
- **The whole worker exits or hangs.** Confirm position size and open native
  protection directly at Binance. Preserve the original journals and mounts.
  Docker restarts an exited process with `unless-stopped`. An unhealthy
  process can require operator restart of the same verified image. Resume with
  the same account, configuration, and journals so the worker queries existing
  order IDs before considering new entries. Keep working native orders in place.
- **Binance acknowledgement is ambiguous.** Query the durable order ID.
  Leave an existing native stop in place until a replacement is confirmed.
  A genuinely rejected or lost stop with an open, unprotected position follows
  the emergency protection policy. A completed native stop with an unfilled
  residual is handled by a reduce-only close.
- **The host or Docker daemon is down.** Native stop and already resting
  orders remain at the venue. Future trailing updates and the 48-hour exit
  require a working executor. After recovery an overdue time exit is handled
  as a normal strategy exit. A watcher outside the trading host is needed to
  detect a full-host outage.

The central engine notifier in `live_trading/deploy/burst-alerts.compose.yaml`
uses one Telegram destination and one durable delivery queue. It reads the
HYPE Burst and BTC MVRV journals through read-only mounts. INJ and UNI join the
same watcher after their first worker heartbeat. For a legacy HYPE journal,
the watcher uses the timestamp of the persisted public trade cursor; newer
workers publish an explicit heartbeat and error status.

Set `BURST_ALERTS_COMMIT`, `HYPE_BURST_STATE`, and
`BURST_ALERTS_TELEGRAM_ENV` externally. The latter uses the existing
`TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` contract.
The notifier has a separate alert database and does not mount exchange keys or
the Docker socket. Alerts identify the originating target even when several
strategies share a container. Existing BTC MVRV strategy signals continue to
use their own delivery outbox. The same-host notifier can report a dead worker
while the notifier and server remain reachable.
