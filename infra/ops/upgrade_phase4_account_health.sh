#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export GG_PHASE4_COMMIT="$(git rev-parse HEAD)"
phase2=infra/deploy/phase2/compose.yaml
phase4=models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml
repair=models/trend_following_preny_profile_15m/deploy/phase4.retention-recovery.yaml
compose=(docker compose -f "$phase2" -f "$phase4" -f "$repair")
accounts=(binance-account-shadow lighter-account-shadow)
strategies=(binance-strategy-paper-shadow lighter-strategy-paper-shadow)
databases=()
for service in "${accounts[@]}"; do
  container="golden-goose-phase2-shadow-${service}-1"
  directory="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$container")"
  [[ -n "$directory" ]] || { echo "Missing data mount: $container" >&2; exit 1; }
  databases+=("$directory/account.sqlite")
done
if [[ -n "$(git status --porcelain -- live_engine market_data trading_core infra/ops/upgrade_phase4_account_health.py infra/ops/upgrade_phase4_account_health.sh models/trend_following_preny_profile_15m/deploy)" ]]; then
  echo 'Upgrade requires committed runtime and deployment files.' >&2
  exit 1
fi
python3 infra/ops/upgrade_phase4_account_health.py --database "${databases[@]}" --target "$GG_PHASE4_COMMIT"
"${compose[@]}" config --quiet
# Both accounts use this same image. Build before pausing consumers.
"${compose[@]}" build binance-account-shadow
"${compose[@]}" stop "${strategies[@]}" "${accounts[@]}"
trap 'result=$?; if [[ $result -ne 0 ]]; then echo "Upgrade stopped. Keep backups and inspect Phase 4 accounts before resuming; no ledger was reset." >&2; fi' EXIT
backup_dir="/root/golden-goose-phase4-backups/$(date -u +%Y%m%dT%H%M%SZ)-health"
python3 infra/ops/upgrade_phase4_account_health.py --database "${databases[@]}" --target "$GG_PHASE4_COMMIT" --backup-dir "$backup_dir"
"${compose[@]}" up -d --no-deps --no-build "${accounts[@]}"
for service in "${accounts[@]}"; do
  container="golden-goose-phase2-shadow-${service}-1"
  for _ in {1..36}; do
    health="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
    [[ "$health" == healthy ]] && break
    [[ "$health" == unhealthy ]] && { echo "$service unhealthy" >&2; exit 1; }
    sleep 5
  done
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
done
# Start existing strategy containers, preserving their release and state.
"${compose[@]}" start "${strategies[@]}"
for service in "${accounts[@]}"; do
  "${compose[@]}" exec -T "$service" python -m live_engine.account_worker --database /data/account.sqlite --status
done
echo "Phase 4 accounts upgraded with all trading history preserved. Backups: $backup_dir"
