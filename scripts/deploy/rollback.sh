#!/usr/bin/env bash
# Restore previous git revision + redeploy images (config backup retained).
# Usage: ./scripts/deploy/rollback.sh
# Optional: ROLLBACK_REF=<sha> ./scripts/deploy/rollback.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

ensure_dirs
PREV_REF_FILE="$DEPLOY_STATE_DIR/previous_git_ref"
CUR_REF_FILE="$DEPLOY_STATE_DIR/current_git_ref"

TARGET="${ROLLBACK_REF:-}"
if [[ -z "$TARGET" && -f "$PREV_REF_FILE" ]]; then
  TARGET="$(cat "$PREV_REF_FILE")"
fi
if [[ -z "$TARGET" ]]; then
  fail "No previous git ref. Set ROLLBACK_REF=<sha>"
  exit 2
fi

log "=== Rollback to $TARGET ==="

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  fail "Not a git repository"
  exit 2
fi

# Backup current config again
STAMP="$(ts)"
mkdir -p "$BACKUP_ROOT/config-pre-rollback-$STAMP"
cp -a "$ENV_FILE" "$BACKUP_ROOT/config-pre-rollback-$STAMP/" 2>/dev/null || true

git rev-parse HEAD >"$DEPLOY_STATE_DIR/rollback_from_ref" || true
git checkout "$TARGET"

# Rebuild and restart without pulling
export SKIP_GIT_PULL=1
export SKIP_DB_BACKUP="${SKIP_DB_BACKUP:-1}"

log "Rebuilding at $TARGET (NO migrate — application rollback only)"
compose build
compose up -d --remove-orphans
log "NOTE: DB schema is NOT rolled back by this script. If schema is newer than code, restore DB from backup."

git rev-parse HEAD >"$CUR_REF_FILE"
log "Waiting for readiness"
for i in $(seq 1 40); do
  code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 \
    "$PORTAL_BASE_URL/api/v1/analysis/health/ready/" || true)
  [[ "$code" == "200" ]] && break
  sleep 5
done

"$SCRIPT_DIR/verify-production.sh" || true

echo "Rollback attempted to $TARGET (verify output above)."
echo "If DB schema is newer than code, restore DB from backups/deploy — see Production Deployment Guide."
pass "Rollback finished"
