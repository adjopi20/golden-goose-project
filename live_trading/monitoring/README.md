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

The independent `live_trading/deploy/burst-alerts.compose.yaml` uses the existing
ledger watchdog and Telegram delivery queue. It reads all three Burst journals
through a read-only mount and has a separate alert database. It receives no
exchange keys or Docker socket. Set `BURST_ALERTS_COMMIT`,
`HYPE_BURST_STATE` and `BURST_ALERTS_TELEGRAM_ENV` externally. The Telegram file
uses the existing `TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, and
`TELEGRAM_CHAT_ID` contract. Confirm the alert destination and delivery on the
server before relying on it. Heartbeat, persistent worker errors, protection,
unresolved orders and overdue exits are watched per symbol. The same-host
notifier can report a dead worker while the notifier and server remain reachable.
