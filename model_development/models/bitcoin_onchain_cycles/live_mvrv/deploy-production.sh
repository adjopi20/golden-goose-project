#!/usr/bin/env bash
# First rollout intentionally refuses live execution. Does not touch other projects.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
test -f binance-production.env || { echo 'Create binance-production.env from the example and configure it locally.' >&2; exit 1; }
compose=(docker compose --env-file binance-production.env -p btc-mvrv-production -f compose.binance-production.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" build
# Check resolved container values, not only the env file (shell vars can override it).
"${compose[@]}" run --rm --no-deps --entrypoint python btc-mvrv-production -c '
import os
assert os.environ["BINANCE_ENV"] == "production"
assert os.environ["BINANCE_PRODUCTION_EXECUTION_ENABLED"].strip().lower() == "false", "First deployment requires execution OFF"
assert not os.environ.get("BINANCE_PRODUCTION_ACK"), "First deployment requires empty acknowledgement"
assert float(os.environ["INITIAL_CAPITAL"]) > 0, "Set positive assigned capital"
print("Production rollout guard passed: execution OFF; no order sent.")
'
"${compose[@]}" run --rm --no-deps btc-mvrv-production status
"${compose[@]}" up -d --wait --wait-timeout 900 btc-mvrv-production
"${compose[@]}" exec -T btc-mvrv-production python -m live_mvrv.binance_main report
echo 'Production monitor deployed with execution OFF. Review report, Telegram and off-server backup before any separate activation decision.'
