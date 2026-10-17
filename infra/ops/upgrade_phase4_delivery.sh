#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
venue="${1:-}"
fresh="${2:-}"
[[ "$venue" == binance || "$venue" == lighter ]] || { echo 'Usage: bash infra/ops/upgrade_phase4_delivery.sh binance|lighter [--fresh-epoch]' >&2; exit 2; }
[[ -z "$fresh" || ( "$venue" == binance && "$fresh" == --fresh-epoch ) ]] || exit 2
export GG_PHASE4_COMMIT="$(git rev-parse HEAD)"
account="${venue}-account-shadow"
strategy="${venue}-strategy-paper-shadow"
container="golden-goose-phase2-shadow-${account}-1"
strategy_container="golden-goose-phase2-shadow-${strategy}-1"
data_dir() { docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$1"; }
account_db="$(data_dir "$container")/account.sqlite"
strategy_db="$(data_dir "$strategy_container")/strategy.sqlite"
strategy_image="$(docker inspect --format '{{.Image}}' "$strategy_container")"
group="$(python3 - "$account_db" <<'PY'
import pathlib,sqlite3,sys
with sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True) as db:
    print(dict(db.execute('SELECT key,value FROM metadata'))['account_group'])
PY
)"
compose=(docker compose -f infra/deploy/phase2/compose.yaml
  -f models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml
  -f models/trend_following_preny_profile_15m/deploy/phase4.retention-recovery.yaml)
case "$venue:$group" in
  binance:binance_preny_phase4_v1) config=binance.account.json ;;
  binance:binance_preny_phase4_v2)
    config=binance.account.recovered.json
    compose+=(-f models/trend_following_preny_profile_15m/deploy/phase4.single-inflight-recovery.yaml)
    fresh="" ;;
  lighter:lighter_preny_phase4_v2) config=lighter.account.recovered.json ;;
  *) echo "Unreviewed account group: $group" >&2; exit 1 ;;
esac
if [[ -n "$fresh" ]]; then
  docker volume inspect golden-goose-phase2-shadow_binance_account_phase4_v2 >/dev/null 2>&1 && {
    echo 'Recovery volume already exists; review it, never overwrite it.' >&2; exit 1;
  }
  compose+=(-f models/trend_following_preny_profile_15m/deploy/phase4.single-inflight-recovery.yaml)
fi
test -z "$(git status --porcelain -- live_engine market_data trading_core infra/ops/upgrade_phase4_delivery.sh infra/ops/upgrade_phase4_account_health.py models/trend_following_preny_profile_15m/deploy)" || {
  echo 'Commit the reviewed runtime/config files first.' >&2; exit 1;
}
check_state() {
  python3 - "$account_db" "$strategy_db" "$fresh" "$GG_PHASE4_COMMIT" <<'PY'
import json,pathlib,sqlite3,sys
from infra.ops.upgrade_phase4_account_health import inspect
from live_engine.account_worker import status
from live_engine.account_delivery_upgrade import SOURCE_RELEASE
account,strategy,fresh,target=sys.argv[1:]
if status(pathlib.Path(account))['accounting_checks']['status']!='pass':
    raise SystemExit('Accounting audit failed; do not reset or upgrade this ledger')
with sqlite3.connect(pathlib.Path(account).resolve().as_uri()+'?mode=ro',uri=True) as db:
    inspect(db,target,compatible={SOURCE_RELEASE})
    meta=dict(db.execute('SELECT key,value FROM metadata'))
    if (meta.get('schema'),meta.get('owner'),meta.get('environment'))!=('4','paper_account_service','server-paper'):
        raise SystemExit('Wrong ledger identity')
    states=[json.loads(r[0]) for r in db.execute('SELECT state FROM accounts')]
    active=sum(bool(s['position'] or s['signals']) for s in states)
    pending=db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('pending','open')").fetchone()[0]
    reservations=db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]
    uncertain=db.execute("SELECT COUNT(*) FROM requests WHERE status='closed_uncertain'").fetchone()[0]
    blocked=db.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'blocked:%'").fetchone()[0]
    if fresh:
        if active or pending or reservations:raise SystemExit('Fresh epoch requires flat/reconciled old account')
        if not (blocked or uncertain):raise SystemExit('Fresh epoch is only for the affected ledger, not a healthy one')
    elif blocked or uncertain:raise SystemExit('Ledger is uncertain; do not use healthy upgrade')
with sqlite3.connect(pathlib.Path(strategy).resolve().as_uri()+'?mode=ro',uri=True) as db:
    if fresh and db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]:
        raise SystemExit('Strategy has unpublished intentions; review before fresh epoch')
print('Account/strategy preflight passed.')
PY
}
check_state
"${compose[@]}" config --quiet
"${compose[@]}" build "$account"
trap 'r=$?; if [[ $r -ne 0 ]]; then "${compose[@]}" stop "$strategy" "$account" || true; echo "Upgrade stopped. Inspect the selected Phase 4 services; no ledger/durable was deleted. Do not restart blindly." >&2; fi' EXIT
"${compose[@]}" stop "$strategy"
if [[ -z "$fresh" ]]; then
  "${compose[@]}" run --rm --no-deps --entrypoint python "$account" \
    -m live_engine.account_delivery_upgrade --database /data/account.sqlite \
    --config "/app/config/$config" --target "$GG_PHASE4_COMMIT" --quiesce
fi
"${compose[@]}" stop "$account"
sleep 2
check_state
stamp="$(date -u +%Y%m%dT%H%M%SZ)-delivery-${venue}"
backup_dir="/root/golden-goose-phase4-backups/$stamp"
python3 - "$account_db" "$strategy_db" "$backup_dir" <<'PY'
import pathlib,sqlite3,sys
destination=pathlib.Path(sys.argv[3]);destination.mkdir(parents=True,exist_ok=False)
for source,name in zip(sys.argv[1:3],('account.sqlite','strategy.sqlite')):
    with sqlite3.connect(pathlib.Path(source).resolve().as_uri()+'?mode=ro',uri=True) as src:
        with sqlite3.connect(destination/name) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise SystemExit('Backup integrity failed')
print('Backups:',destination)
PY
if [[ -z "$fresh" ]]; then
  # The helper holds the writer lock; refuses in-flight deliveries; never ACKs or deletes messages.
  "${compose[@]}" run --rm --no-deps --entrypoint python "$account" \
    -m live_engine.account_delivery_upgrade --database /data/account.sqlite \
    --config "/app/config/$config" --target "$GG_PHASE4_COMMIT" \
    --backup "/data/pre-delivery-${stamp}.sqlite"
else
  echo 'Starting a NEW Binance validation epoch at 1000 per allocation. Old history remains separate.'
fi
"${compose[@]}" up -d --no-deps --no-build "$account"
for _ in {1..36}; do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
  [[ "$health" == healthy ]] && break
  [[ "$health" == unhealthy ]] && { echo 'Account unhealthy; keep strategy stopped.' >&2; exit 1; }
  sleep 5
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
"${compose[@]}" start "$strategy"
[[ "$(docker inspect --format '{{.Image}}' "$strategy_container")" == "$strategy_image" ]]
"${compose[@]}" exec -T "$account" python -m live_engine.account_worker --database /data/account.sqlite --status
echo "Selected Phase 4 upgrade completed. Backups: $backup_dir. Other services were not restarted."
