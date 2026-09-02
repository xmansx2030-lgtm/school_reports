#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH="${DEPLOY_PATH:-/opt/school_reports}"
LOCK_FILE="$DEPLOY_PATH/deploy/hetzner/.operations-collector.lock"
COMPOSE_FILES=(-f compose.hetzner.yaml -f compose.caddy.yaml)

cd "$DEPLOY_PATH"

collect() {
  python3 deploy/hetzner/collect_operations_inventory.py \
    | docker compose "${COMPOSE_FILES[@]}" exec -T web \
        python manage.py sync_operations_inventory -
}

if [ "${1:-}" != "--unlocked" ] && command -v flock >/dev/null 2>&1; then
  flock -n "$LOCK_FILE" bash -c \
    'DEPLOY_PATH="$1" bash "$1/deploy/hetzner/run_operations_collector.sh" --unlocked' \
    _ "$DEPLOY_PATH" || exit 0
  exit 0
fi

collect
