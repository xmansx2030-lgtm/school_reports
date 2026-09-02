#!/usr/bin/env bash
#
# Runs *on the Hetzner server*, invoked by .github/workflows/ci.yml after the
# test job passes. The workflow ships the compose files into $DEPLOY_PATH over
# ssh, then executes this script there.
#
# Deliberately never touches deploy/hetzner/env.production — production secrets
# live only on the server and are the one thing a deploy must not overwrite.
#
# Required environment (exported by the workflow over ssh):
#   APP_IMAGE              fully qualified image ref, e.g. ghcr.io/owner/repo:<sha>
#   GHCR_USER              GitHub actor, for `docker login ghcr.io`
#   GHCR_TOKEN_FROM_STDIN  when 1, the registry token is read from stdin
#
# Can also be run by hand on the server for a rollback:
#   APP_IMAGE=ghcr.io/owner/repo:<older-sha> bash deploy/hetzner/remote_deploy.sh
set -euo pipefail

DEPLOY_PATH="${DEPLOY_PATH:-/opt/school_reports}"
EDGE_NETWORK="school-platform-edge"
COMPOSE_FILES=(-f compose.hetzner.yaml -f compose.caddy.yaml)

die() { printf '\n[deploy] ERROR: %s\n' "$1" >&2; exit 1; }
log() { printf '[deploy] %s\n' "$1"; }

# --- preflight ---------------------------------------------------------------
: "${APP_IMAGE:?APP_IMAGE is not set}"
RELEASE_SHA="${RELEASE_SHA:-${APP_IMAGE##*:}}"
case "$RELEASE_SHA" in
  *[!0-9a-fA-F]*|"") RELEASE_SHA="unknown" ;;
esac

command -v docker >/dev/null 2>&1 || die "docker is not installed on this server."
docker compose version >/dev/null 2>&1 || die "the docker compose v2 plugin is missing."

cd "$DEPLOY_PATH" || die "DEPLOY_PATH '$DEPLOY_PATH' does not exist."

[ -f compose.hetzner.yaml ] || die "compose.hetzner.yaml missing in $DEPLOY_PATH — the file sync step did not run."
[ -f deploy/hetzner/env.production ] || die \
  "deploy/hetzner/env.production is missing in $DEPLOY_PATH.
   Production secrets are never shipped from CI. Create it once on the server
   from deploy/hetzner/env.production.example, then re-run this deploy."

# Derive least-privilege environment files on every deploy.  The database and
# cache containers must never receive payment, email, AI, or object-storage
# credentials simply because the application needs them.
derive_env_file() {
  local target="$1" pattern="$2" expected="$3" tmp count
  tmp="$(mktemp "$DEPLOY_PATH/deploy/hetzner/.env-scope.XXXXXX")"
  grep -E "$pattern" deploy/hetzner/env.production >"$tmp"
  count="$(wc -l <"$tmp")"
  if [ "$count" -ne "$expected" ] || grep -Eq '^[^=]+=$' "$tmp"; then
    rm -f -- "$tmp"
    die "Could not derive $target: expected $expected non-empty values."
  fi
  install -m 600 -o root -g root "$tmp" "$target"
  rm -f -- "$tmp"
}

derive_env_file deploy/hetzner/env.postgres \
  '^(POSTGRES_DB|POSTGRES_USER|POSTGRES_PASSWORD)=' 3
derive_env_file deploy/hetzner/env.redis \
  '^(REDIS_PASSWORD|REDIS_MAXMEMORY|REDIS_MAXMEMORY_POLICY)=' 3

# `edge` is declared external in compose.hetzner.yaml, so compose will refuse to
# start rather than create it. Creating it here keeps a fresh server bootable.
docker network inspect "$EDGE_NETWORK" >/dev/null 2>&1 || {
  log "creating external network $EDGE_NETWORK"
  docker network create "$EDGE_NETWORK" >/dev/null
}

# Remember what is running now, so a failed deploy can be reverted by hand.
PREVIOUS_IMAGE="$(docker inspect --format '{{.Config.Image}}' school-reports-web-1 2>/dev/null || true)"

export APP_IMAGE RELEASE_SHA

# يُثبَّت الوسم المنشور في `.env` بجانب ملفات compose.
#
# ‏compose يقرأ هذا الملف تلقائياً، فيصير `docker compose up -d` اليدوي — بعد
# تعديل متغيّر بيئة مثلاً — عاملاً على الصورة المنشورة نفسها. وقبل هذا كان
# الأمر اليدوي يسقط على وسم `school-reports:local` فيستبدل صورة الإنتاج بصمت
# ويُسقط الموقع بخطأ صلاحيات لا علاقة له بالسبب.
#
# ويُكتب بذرّية (ملف مؤقت ثم `mv`) كي لا يقرأ أمرٌ متزامن ملفاً نصف مكتوب.
printf 'APP_IMAGE=%s\nRELEASE_SHA=%s\n' "$APP_IMAGE" "$RELEASE_SHA" > "$DEPLOY_PATH/.env.tmp"
mv -f "$DEPLOY_PATH/.env.tmp" "$DEPLOY_PATH/.env"
log "pinned APP_IMAGE and RELEASE_SHA in $DEPLOY_PATH/.env"

# --- pull --------------------------------------------------------------------
if [ "${GHCR_TOKEN_FROM_STDIN:-}" = "1" ]; then
  GHCR_TOKEN="$(cat)"
fi

if [ -n "${GHCR_TOKEN:-}" ]; then
  log "logging in to ghcr.io"
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-x-access-token}" --password-stdin
  # The registry token expires with the CI run; do not leave it on disk.
  trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
fi

log "pulling $APP_IMAGE"
docker pull --quiet "$APP_IMAGE"

# The app runtime now has a stable UID. Existing named volumes may still be
# owned by the distro-assigned UID used by older images, which makes the
# one-shot collectstatic step fail before migrations can complete. Restrict
# the ownership repair to the generated static-assets volume; user media is in
# private object storage and is deliberately not touched here.
log "ensuring the static-assets volume belongs to the app runtime"
docker volume create school-reports_static-data >/dev/null
docker run --rm --user 0:0 --entrypoint chown \
  -v school-reports_static-data:/app/staticfiles \
  "$APP_IMAGE" -R 10001:10001 /app/staticfiles

# --- release -----------------------------------------------------------------
# `up -d` re-runs the one-shot `migrate` service (migrate --noinput +
# collectstatic); web/worker/beat wait on service_completed_successfully, so a
# failed migration aborts the release instead of serving a half-migrated app.
# No --remove-orphans: compose counts services from inactive profiles (pgbouncer)
# as orphans and would tear them down on every deploy.
log "starting release"
if ! docker compose "${COMPOSE_FILES[@]}" up -d; then
  log "release startup failed — last migrate log lines:"
  docker compose "${COMPOSE_FILES[@]}" logs --tail 120 migrate || true
  die "release startup failed."
fi

# A bind-mounted Caddyfile can change without changing the container spec, so
# ``compose up`` correctly leaves the running proxy untouched. Reload it
# explicitly: this validates the new configuration and swaps it without a
# listener restart or dropped connections.
log "reloading Caddy configuration"
docker compose "${COMPOSE_FILES[@]}" exec -T caddy \
  caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

# --- verify ------------------------------------------------------------------
log "waiting for web to report healthy"
for _ in $(seq 1 60); do
  status="$(docker inspect --format '{{.State.Health.Status}}' school-reports-web-1 2>/dev/null || echo missing)"
  case "$status" in
    healthy|unhealthy) break ;;
  esac
  sleep 5
done

if [ "$status" != "healthy" ]; then
  log "web is '$status' after the deploy — last 60 log lines:"
  docker compose "${COMPOSE_FILES[@]}" logs --tail 60 migrate web || true
  if [ -n "$PREVIOUS_IMAGE" ]; then
    log "roll back with: APP_IMAGE=$PREVIOUS_IMAGE docker compose ${COMPOSE_FILES[*]} up -d"
  fi
  die "deploy did not reach a healthy state."
fi

# Record the release immediately after it is proven healthy.  The periodic
# synchronizer remains a safety net, but waiting for its five-minute interval
# makes the operations app briefly advertise the just-deployed commit as a new
# release and can invite a duplicate deployment.
case "$RELEASE_SHA" in
  [0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]*)
    if [ "${#RELEASE_SHA}" -eq 40 ]; then
      log "stamping the verified Tawtheeq release"
      docker compose "${COMPOSE_FILES[@]}" exec -T web \
        python manage.py stamp_managed_project_release \
        tawtheeq "$RELEASE_SHA" --image "$APP_IMAGE"
    fi
    ;;
esac

# Keep the operations app aligned with every Docker Compose project on this
# host. The collector uses read-only Docker commands and writes through a
# management command inside the already-running web container; no Docker socket
# is exposed to the public web process.
log "synchronizing operations project inventory"
bash deploy/hetzner/run_operations_collector.sh

if command -v crontab >/dev/null 2>&1; then
  COLLECTOR_MARKER="# tawtheeq-operations-inventory"
  COLLECTOR_CRON="*/5 * * * * DEPLOY_PATH=$DEPLOY_PATH /usr/bin/env bash $DEPLOY_PATH/deploy/hetzner/run_operations_collector.sh >> $DEPLOY_PATH/deploy/hetzner/operations-collector.log 2>&1 $COLLECTOR_MARKER"
  {
    crontab -l 2>/dev/null \
      | grep -vF "$COLLECTOR_MARKER" \
      | grep -vF "deploy/hetzner/run_operations_collector.sh" \
      || true
    printf '%s\n' "$COLLECTOR_CRON"
  } | crontab -
  log "installed five-minute operations inventory collector"
else
  log "warning: crontab is unavailable; inventory was collected once but automatic refresh was not installed"
fi

docker image prune --force --filter "until=168h" >/dev/null 2>&1 || true

log "deployed $APP_IMAGE successfully"
docker compose "${COMPOSE_FILES[@]}" ps
