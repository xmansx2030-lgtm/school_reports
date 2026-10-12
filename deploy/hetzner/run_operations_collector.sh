#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH="${DEPLOY_PATH:-/opt/school_reports}"
LOCK_FILE="$DEPLOY_PATH/deploy/hetzner/.operations-collector.lock"
COMPOSE_FILES=(-f compose.hetzner.yaml -f compose.caddy.yaml)

cd "$DEPLOY_PATH"

collect() {
  # A pipeline boots Django while Docker samples the web container, so the
  # monitoring command measures its own startup as application CPU usage.
  # Capture first; only start the DB importer after the sample is complete.
  collector_report=$(mktemp)
  trap 'rm -f -- "$collector_report"' EXIT
  python3 deploy/hetzner/collect_operations_inventory.py > "$collector_report"
  docker compose "${COMPOSE_FILES[@]}" exec -T web \
    python manage.py sync_operations_inventory - < "$collector_report"
}

if [ "${1:-}" != "--unlocked" ] && command -v flock >/dev/null 2>&1; then
  flock -n -E 75 "$LOCK_FILE" bash -c \
    'DEPLOY_PATH="$1" bash "$1/deploy/hetzner/run_operations_collector.sh" --unlocked' \
    _ "$DEPLOY_PATH" || {
      collector_status=$?
      # A concurrent collector is benign; collection/import failure is not.
      [ "$collector_status" -eq 75 ] && exit 0
      exit "$collector_status"
    }
  exit 0
fi

collect
