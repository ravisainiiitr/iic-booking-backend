#!/usr/bin/env bash
# Nightly portal DB backup (RDS via DATABASE_URL).
# Installed on production host as /home/ubuntu/bin/iic-nightly-backup.sh
set -euo pipefail

LOG="${LOG:-/var/log/iic-nightly-backup.log}"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "==== $(date -u +%Y-%m-%dT%H:%M:%SZ) nightly backup start ===="

ENV_FILE="${ENV_FILE:-/home/ubuntu/iic-booking-backend/.envs/.production/.django}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/ubuntu/backups/nightly}"
KEEP_DAYS="${KEEP_DAYS:-14}"
PG_MAJOR="${PG_MAJOR:-15}"
LABEL="nightly-$(date -u +%Y%m%d)"
OUT="$BACKUP_ROOT/$LABEL"
mkdir -p "$OUT/db" "$OUT/config"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "FAIL: ENV_FILE missing: $ENV_FILE"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source <(grep -E '^DATABASE_URL=' "$ENV_FILE" | sed 's/\r$//')
set +a

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "FAIL: DATABASE_URL missing"
  exit 1
fi

cp -a "$ENV_FILE" "$OUT/config/django.env"
cp -a /home/ubuntu/iic-booking-backend/docker-compose.production.yml "$OUT/config/" 2>/dev/null || true
cp -a /home/ubuntu/iic-booking-frontend/docker-compose.production.yml "$OUT/config/frontend-compose.yml" 2>/dev/null || true

PGIMG="postgres:${PG_MAJOR}"
echo "Using image $PGIMG"

docker run --rm --network host \
  -e DATABASE_URL="$DATABASE_URL" \
  "$PGIMG" \
  bash -lc 'pg_dump "$DATABASE_URL" --no-owner --no-acl | gzip -c' \
  >"$OUT/db/portal.sql.gz"

gzip -t "$OUT/db/portal.sql.gz"
SIZE=$(du -h "$OUT/db/portal.sql.gz" | awk '{print $1}')
echo "PASS dump size=$SIZE path=$OUT"

find "$BACKUP_ROOT" -maxdepth 1 -type d -name 'nightly-*' -mtime +"$KEEP_DAYS" -exec rm -rf {} +
ln -sfn "$OUT" "$BACKUP_ROOT/latest"
echo "==== $(date -u +%Y-%m-%dT%H:%M:%SZ) nightly backup done ===="
