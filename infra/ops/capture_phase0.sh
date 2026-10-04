#!/usr/bin/env bash
# Run from the server repository root. Does not rebuild/restart containers.
set -euo pipefail
umask 077

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
base=models/trend_following_preny_profile_15m/deploy/compose.yaml
paper=models/trend_following_preny_profile_15m/deploy/paper.simulation.yaml
helper=infra/ops/phase0_snapshot.py
capture_id="phase0_$(date -u +%Y%m%dT%H%M%SZ)_$$"
output_base=${1:-/tmp/golden-goose-phase0}
mkdir -p "$output_base"
output_base=$(cd "$output_base" && pwd)
out="$output_base/$capture_id"
mkdir "$out"
compose=(docker compose -f "$base" -f "$paper")
trap 'printf "Capture failed. Preserve %s; do not analyze incomplete artifacts.\n" "$out" >&2' ERR

test -f "$helper"
"${compose[@]}" config --quiet
git rev-parse HEAD > "$out/server_git_commit.txt"
git branch --show-current > "$out/server_git_branch.txt"
git diff --name-status HEAD -- live_engine backtest_engine models/trend_following_preny_profile_15m infra/ops > "$out/server_runtime_changes.txt"

for service in binance-paper lighter-observe; do
    if [[ "$service" == binance-paper ]]; then
        venue=binance
        database=/data/binance.sqlite
    else
        venue=lighter
        database=/data/lighter.sqlite
    fi
    cid=$("${compose[@]}" ps -q "$service")
    if [[ -z "$cid" ]]; then
        printf 'Missing running service: %s\n' "$service" >&2
        exit 1
    fi
    docker inspect --format '{"container_id":{{json .Id}},"image_id":{{json .Image}},"created":{{json .Created}},"started_at":{{json .State.StartedAt}},"running":{{json .State.Running}},"restart_count":{{json .RestartCount}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}' "$cid" > "$out/${venue}_container.json"
    image_id=$(docker inspect --format '{{.Image}}' "$cid")
    docker image inspect --format '{{json .RepoDigests}}' "$image_id" > "$out/${venue}_image_digests.json"
    remote_dir="/data/$capture_id"
    "${compose[@]}" exec -T "$service" python - --root /app --database "$database" \
        --venue "$venue" --output-dir "$remote_dir" < "$helper"
    docker cp "$cid:$remote_dir" "$out/$venue"
    "${compose[@]}" logs --tail=200 "$service" > "$out/${venue}_recent_logs.txt" 2>&1
done

docker stats --no-stream --format '{{json .}}' \
    "$("${compose[@]}" ps -q binance-paper)" "$("${compose[@]}" ps -q lighter-observe)" > "$out/resources.jsonl"
printf 'Both container snapshots completed. This is evidence capture, not a migration approval.\n' > "$out/CAPTURE_COMPLETE.txt"
tar -C "$output_base" -czf "$out.tar.gz" "$capture_id"
sha256sum "$out.tar.gz"
printf '\nArchive: %s.tar.gz\nCopy this archive off the VPS. Keep existing services running.\n' "$out"
