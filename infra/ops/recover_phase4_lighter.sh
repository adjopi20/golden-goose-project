#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
export GG_PHASE4_COMMIT="$(git rev-parse HEAD)"
phase2=infra/deploy/phase2/compose.yaml
phase4=models/trend_following_preny_profile_15m/deploy/phase4.paper-shadow.yaml
repair=models/trend_following_preny_profile_15m/deploy/phase4.retention-recovery.yaml
compose=(docker compose -f "$phase2" -f "$phase4" -f "$repair")
account=golden-goose-phase2-shadow-lighter-account-shadow-1
strategy=golden-goose-phase2-shadow-lighter-strategy-paper-shadow-1

if [[ -n "$(git status --porcelain -- live_engine/account_store.py live_engine/account_worker.py models/trend_following_preny_profile_15m/deploy infra/ops/recover_phase4_lighter.sh)" ]]; then
  echo 'Recovery requires committed account code/config.' >&2
  exit 1
fi
"${compose[@]}" config --quiet

data_dir() {
  docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$1"
}
old_account="$(data_dir "$account")/account.sqlite"
strategy_db="$(data_dir "$strategy")/strategy.sqlite"
old_strategy_image="$(docker inspect --format '{{.Image}}' "$strategy")"

check_empty() {
  python3 - "$old_account" "$strategy_db" <<'PY'
import json, sqlite3, sys
account, strategy = sys.argv[1:]
with sqlite3.connect('file:'+account+'?mode=ro', uri=True) as db:
    requests = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
    journals = db.execute("SELECT COUNT(*) FROM journal WHERE kind IN ('fill','trade')").fetchone()[0]
    positions = sum(json.loads(s)['position'] is not None for (s,) in db.execute('SELECT state FROM accounts'))
    if requests or journals or positions:
        raise SystemExit(f'Old Lighter account has requests={requests}, fills/trades={journals}, positions={positions}; stop and reconcile')
with sqlite3.connect('file:'+strategy+'?mode=ro', uri=True) as db:
    proposals = db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0]
    outbox = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
    if proposals or outbox:
        raise SystemExit(f'Lighter strategy has proposals={proposals}, outbox={outbox}; stop and reconcile')
print('Old Lighter account and strategy have no positions, requests, proposals or outbox rows.')
PY
}

check_empty
docker exec -i golden-goose-phase2-shadow-binance-account-shadow-1 python - <<'PY'
import asyncio
from market_data.broker import connect
async def check():
    nc = await connect('nats://nats:4222')
    try:
        c = await nc.jetstream().consumer_info('GG_INTENT_V1','phase4_lighter_preny_phase4_v1_GG_INTENT_V1')
        if c.delivered.consumer_seq or c.num_pending or c.num_ack_pending:
            raise SystemExit('Old Lighter intention durable has activity; stop and reconcile')
    finally:
        await nc.close()
asyncio.run(check())
PY

echo 'Stopping only the Lighter Phase 4 strategy and account.'
"${compose[@]}" stop lighter-strategy-paper-shadow lighter-account-shadow
trap 'result=$?; if [[ $result -ne 0 ]]; then echo "Recovery stopped; inspect the Lighter services. No old ledger or durable was deleted." >&2; fi' EXIT
check_empty

backup_dir="/root/golden-goose-phase4-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
python3 - "$old_account" "$strategy_db" "$backup_dir" <<'PY'
import pathlib, sqlite3, sys
account, strategy, destination = sys.argv[1:]
for source, name in [(account,'lighter-account-v1.sqlite'),(strategy,'lighter-strategy.sqlite')]:
    with sqlite3.connect('file:'+source+'?mode=ro', uri=True) as src:
        with sqlite3.connect(str(pathlib.Path(destination)/name)) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
print('Consistent SQLite backups saved in',destination)
PY

"${compose[@]}" build lighter-account-shadow
"${compose[@]}" up -d --no-deps --no-build lighter-account-shadow
for _ in {1..36}; do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$account")"
  [[ "$health" == healthy ]] && break
  [[ "$health" == unhealthy ]] && { echo 'New Lighter account unhealthy' >&2; exit 1; }
  sleep 5
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "$account")" == healthy ]]

"${compose[@]}" start lighter-strategy-paper-shadow
[[ "$(docker inspect --format '{{.Image}}' "$strategy")" == "$old_strategy_image" ]] || {
  echo 'Existing Lighter strategy image changed unexpectedly' >&2
  exit 1
}
"${compose[@]}" ps -a
"${compose[@]}" exec -T lighter-account-shadow \
  python -m live_engine.account_worker --database /data/account.sqlite --status
echo "Lighter Phase 4 recovery started with fresh ledger; old backup: $backup_dir"
