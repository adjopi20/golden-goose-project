#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(git rev-parse --show-toplevel)"
compose=infra/deploy/phase2/compose.yaml
stamp="$(date -u +%Y%m%dT%H%M%SZ)_$$"
root="${1:-/tmp/golden-goose-phase2}/$stamp"
mkdir -p "$root"
trap 'printf "Capture failed. Preserve %s; do not analyze incomplete artifacts.\n" "$root" >&2' ERR
git rev-parse HEAD > "$root/server_git_commit.txt"
docker compose -f "$compose" ps -a > "$root/services.txt"
docker stats --no-stream > "$root/resources.txt"
# Container metadata survives removal of an old image from Docker's image store.
# Do not replace the recorded running image ID with today's mutable image tag.
containers="$(docker compose -f "$compose" ps -a -q)"
test -n "$containers"
mkdir "$root/images"
for container in $containers; do
  docker inspect --format '{"container_id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},"configured_image":{{json .Config.Image}},"started_at":{{json .State.StartedAt}},"running":{{json .State.Running}},"restart_count":{{json .RestartCount}},"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}}}' "$container" >> "$root/containers.jsonl"
  image_id="$(docker inspect --format '{{.Image}}' "$container")"
  if docker image inspect --format '{{json .RepoDigests}}' "$image_id" \
      > "$root/images/$container.json" 2> "$root/images/$container.stderr.txt"; then
    :
  elif [[ "$(< "$root/images/$container.stderr.txt")" == *"No such image:"* ]]; then
    printf '{"status":"unavailable","reason":"image_metadata_missing","image_id":"%s"}\n' \
      "$image_id" > "$root/images/$container.json"
    printf 'WARNING: image metadata missing for %s (%s); running image ID preserved.\n' \
      "$container" "$image_id" | tee -a "$root/warnings.txt" >&2
  else
    cat "$root/images/$container.stderr.txt" >&2
    exit 1
  fi
done
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
printf 'Capture complete; inspect warnings before migration approval.\n' > "$root/CAPTURE_COMPLETE.txt"
tar -czf "$root.tar.gz" -C "$(dirname "$root")" "$(basename "$root")"
sha256sum "$root.tar.gz"
printf '\nArchive: %s.tar.gz\nExisting paper services are unchanged.\n' "$root"
