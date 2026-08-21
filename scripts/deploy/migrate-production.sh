#!/usr/bin/env bash
# Explicit production migration — NEVER call from normal deploy/startup.
#
# Usage (on EC2, after backups + GO approval):
#   CONFIRM_MIGRATE=YES ./scripts/deploy/migrate-production.sh
#
# Or via GitHub Actions workflow "Migrate Production" with confirm_migrate=MIGRATE.
#
# DO NOT run this during production candidate RO qualification.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

if [[ "${CONFIRM_MIGRATE:-}" != "YES" ]]; then
  fail "Refusing: set CONFIRM_MIGRATE=YES to apply production migrations"
  echo "DEPLOYMENT ≠ MIGRATION. Application deploy must not call this script."
  exit 2
fi

ensure_dirs
log "=== EXPLICIT production migrate ($(ts)) ==="
log "Taking optional DB backup first (SKIP_DB_BACKUP=1 to skip)"
if [[ "${SKIP_DB_BACKUP:-0}" != "1" ]]; then
  "$SCRIPT_DIR/backup.sh" --db-only --label "pre-migrate-$(ts)" || log "WARN: backup.sh returned non-zero"
fi

log "showmigrations (before)"
compose run --rm --no-deps django python manage.py showmigrations users || true

log "migrate --noinput"
compose run --rm --no-deps django python manage.py migrate --noinput

log "showmigrations (after)"
compose run --rm --no-deps django python manage.py showmigrations users || true

pass "Explicit migrate complete — verify application health separately"
