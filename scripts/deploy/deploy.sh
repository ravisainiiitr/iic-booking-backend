#!/usr/bin/env bash
# One-command Remote Analysis production deploy (IIT Roorkee ops).
# Usage:
#   ./scripts/deploy/deploy.sh
#   COMPOSE_PROFILES=guacamole,flower PORTAL_BASE_URL=https://booking.iitr.ac.in ./scripts/deploy/deploy.sh
#
# Does not change application business logic — build, restart, verify.
# DEPLOYMENT ≠ MIGRATION: does not run manage.py migrate.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

ensure_dirs
STAMP="$(ts)"
PREV_REF_FILE="$DEPLOY_STATE_DIR/previous_git_ref"
CUR_REF_FILE="$DEPLOY_STATE_DIR/current_git_ref"
CFG_BACKUP="$BACKUP_ROOT/config-$STAMP"

log "=== Remote Analysis deploy ($STAMP) ==="
log "COMPOSE_FILE=$COMPOSE_FILE profiles=$COMPOSE_PROFILES"

if [[ ! -f "$ENV_FILE" ]]; then
  fail "Missing env file: $ENV_FILE"
  echo "Copy docs/release/rc1/sample.env.production and edit secrets."
  exit 2
fi

# 1) Backup configuration
log "Backing up configuration → $CFG_BACKUP"
mkdir -p "$CFG_BACKUP"
cp -a "$ENV_FILE" "$CFG_BACKUP/" 2>/dev/null || true
[[ -f .env ]] && cp -a .env "$CFG_BACKUP/" || true
cp -a "$COMPOSE_FILE" "$CFG_BACKUP/" 2>/dev/null || true
echo "$STAMP" >"$CFG_BACKUP/TIMESTAMP"

# 2) Record git refs for rollback
if git rev-parse --git-dir >/dev/null 2>&1; then
  if [[ -f "$CUR_REF_FILE" ]]; then
    cp "$CUR_REF_FILE" "$PREV_REF_FILE"
  else
    git rev-parse HEAD >"$PREV_REF_FILE" || true
  fi
  if [[ "${SKIP_GIT_PULL:-0}" != "1" ]]; then
    log "Pulling latest code (SKIP_GIT_PULL=1 to skip)"
    git pull --ff-only || log "WARN: git pull failed — continuing with local tree"
  fi
  git rev-parse HEAD >"$CUR_REF_FILE"
  log "Git HEAD=$(cat "$CUR_REF_FILE")"
else
  log "WARN: not a git checkout — skip pull/tag tracking"
fi

# 3) Optional DB backup before migrate
if [[ "${SKIP_DB_BACKUP:-0}" != "1" ]]; then
  log "Database backup (SKIP_DB_BACKUP=1 to skip)"
  "$SCRIPT_DIR/backup.sh" --db-only --label "pre-deploy-$STAMP" || log "WARN: backup.sh returned non-zero"
fi

# 4) Build images
log "Building images"
compose build

# 5) Start infrastructure first
log "Starting postgres + redis"
compose up -d postgres redis
sleep 5

# 6) Static + RA settings sync only (NO migrate — DEPLOYMENT ≠ MIGRATION)
log "collectstatic + sync_remote_analysis_settings (migrate skipped; use scripts/deploy/migrate-production.sh)"
compose run --rm --no-deps django python manage.py collectstatic --noinput
compose run --rm --no-deps django python manage.py sync_remote_analysis_settings || true

# 7) Startup validation (fail fast)
log "Startup validation"
if ! compose run --rm --no-deps django python manage.py validate_deployment_startup --strict; then
  fail "validate_deployment_startup"
  echo "Aborting restart. Fix env/deps then re-run. Rollback: ./scripts/deploy/rollback.sh"
  exit 1
fi

# 8) Restart app services
log "Restarting Portal + Celery (+ profiles)"
compose up -d --remove-orphans

# 9) Wait for health
log "Waiting for Portal readiness"
READY=0
for i in $(seq 1 60); do
  code=$(curl -sS -o /tmp/ra_ready_deploy.json -w "%{http_code}" --max-time 5 \
    "$PORTAL_BASE_URL/api/v1/analysis/health/ready/" || true)
  if [[ "$code" == "200" ]]; then
    READY=1
    break
  fi
  sleep 5
done
if [[ "$READY" -ne 1 ]]; then
  fail "Portal readiness timeout (last http=$code)"
  exit 1
fi
pass "Portal readiness"

# 10) Post-deploy verify
log "Running verify-production.sh"
"$SCRIPT_DIR/verify-production.sh" || {
  fail "verify-production"
  exit 1
}

echo ""
echo "======== DEPLOYMENT SUMMARY ========"
echo "Timestamp:     $STAMP"
echo "Compose file:  $COMPOSE_FILE"
echo "Profiles:      $COMPOSE_PROFILES"
echo "Config backup: $CFG_BACKUP"
echo "Git HEAD:      $(cat "$CUR_REF_FILE" 2>/dev/null || echo n/a)"
echo "Prev HEAD:     $(cat "$PREV_REF_FILE" 2>/dev/null || echo n/a)"
echo "Portal URL:    $PORTAL_BASE_URL"
echo "Health live:   $PORTAL_BASE_URL/api/v1/analysis/health/live/"
echo "Health ready:  $PORTAL_BASE_URL/api/v1/analysis/health/ready/"
echo "Rollback:      ./scripts/deploy/rollback.sh"
echo "===================================="
pass "Deploy complete"
