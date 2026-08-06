#!/usr/bin/env bash
# Backup database, media, and configuration for Remote Analysis / Portal.
# Usage:
#   ./scripts/deploy/backup.sh
#   ./scripts/deploy/backup.sh --db-only --label pre-deploy
#   ./scripts/deploy/backup.sh --media-only

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

ensure_dirs

# Load DATABASE_URL from production env file when not already set (RDS / external Postgres).
if [[ -z "${DATABASE_URL:-}" && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^DATABASE_URL=' "$ENV_FILE" | sed 's/\r$//')
  set +a
fi
DO_DB=1
DO_MEDIA=1
DO_CFG=1
LABEL="$(ts)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-only) DO_MEDIA=0; DO_CFG=0; shift ;;
    --media-only) DO_DB=0; DO_CFG=0; shift ;;
    --config-only) DO_DB=0; DO_MEDIA=0; shift ;;
    --label) LABEL="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

OUT="$BACKUP_ROOT/$LABEL"
mkdir -p "$OUT"
log "Backup → $OUT"

if [[ "$DO_CFG" -eq 1 ]]; then
  mkdir -p "$OUT/config"
  cp -a "$ENV_FILE" "$OUT/config/" 2>/dev/null || true
  [[ -f .env ]] && cp -a .env "$OUT/config/" || true
  cp -a docker-compose.ra-production.yml "$OUT/config/" 2>/dev/null || true
  cp -a docker-compose.production.yml "$OUT/config/" 2>/dev/null || true
  pass "config"
fi

if [[ "$DO_DB" -eq 1 ]]; then
  mkdir -p "$OUT/db"
  if compose ps postgres 2>/dev/null | grep -q postgres; then
    compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
      | gzip -c >"$OUT/db/portal.sql.gz"
    pass "postgres dump (compose)"
  elif command -v pg_dump >/dev/null 2>&1 && [[ -n "${DATABASE_URL:-}" ]]; then
    pg_dump "$DATABASE_URL" | gzip -c >"$OUT/db/portal.sql.gz"
    pass "postgres dump (DATABASE_URL)"
  else
    fail "postgres dump unavailable — set compose postgres or DATABASE_URL"
  fi
fi

if [[ "$DO_MEDIA" -eq 1 ]]; then
  mkdir -p "$OUT/media"
  # Prefer named volume copy via temporary container
  if docker volume inspect iic-ra-production_production_media >/dev/null 2>&1; then
    docker run --rm \
      -v iic-ra-production_production_media:/src:ro \
      -v "$OUT/media:/dst" \
      alpine:3.20 sh -c "cd /src && tar czf /dst/media.tar.gz ."
    pass "media volume"
  elif [[ -d iic_booking/media ]]; then
    tar czf "$OUT/media/media.tar.gz" -C iic_booking media
    pass "media directory"
  else
    log "WARN: media volume/dir not found — skip"
  fi
fi

echo "$OUT" >"$DEPLOY_STATE_DIR/last_backup_path"
echo "Backup complete: $OUT"
pass "backup"
