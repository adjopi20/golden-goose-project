#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
compose=infra/deploy/phase2/compose.yaml
stamp="$(date -u +%Y%m%dT%H%M%SZ)_$$"
root="/tmp/golden-goose-phase2/$stamp"
mkdir -p "$root"
docker compose -f "$compose" ps -a > "$root/services.txt"
docker stats --no-stream > "$root/resources.txt"
docker compose -f "$compose" images > "$root/images.txt"
docker compose -f "$compose" logs --since 24h --tail 300 > "$root/logs.txt" 2>&1
for venue in binance lighter; do
  service="${venue}-collector"
  docker compose -f "$compose" exec -T "$service" python -m market_data.run \
    --config "/app/config/$venue.json" --database "/data/$venue.sqlite" --action status > "$root/$venue-status.json"
  docker compose -f "$compose" exec -T "$service" python -c \
    "from market_data.store import DataStore; s=DataStore('/data/$venue.sqlite','$venue'); s.backup('/data/phase2_$stamp.sqlite'); s.close()"
  container="$(docker compose -f "$compose" ps -q "$service")"
  docker cp "$container:/data/phase2_$stamp.sqlite" "$root/$venue.sqlite"
done
docker compose -f "$compose" exec -T shadow-receipts python -m market_data.shadow \
  --database /data/receipts.sqlite --status > "$root/shadow-status.json"
tar -czf "$root.tar.gz" -C "$(dirname "$root")" "$(basename "$root")"
sha256sum "$root.tar.gz"
printf '\nArchive: %s.tar.gz\nExisting paper services are unchanged.\n' "$root"
