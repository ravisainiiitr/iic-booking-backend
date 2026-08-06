#!/usr/bin/env bash
# Verify nightly dump integrity and optionally restore into a temporary RDS database.
# Usage:
#   ./iic-restore-verify.sh /home/ubuntu/backups/nightly/nightly-YYYYMMDD
#   VERIFY_RESTORE_DB=1 ./iic-restore-verify.sh /home/ubuntu/backups/nightly/latest
set -euo pipefail

SRC="${1:-}"
if [[ -z "$SRC" || ! -d "$SRC" ]]; then
  echo "Usage: $0 <backup-directory>"
  exit 2
fi

DUMP="$SRC/db/portal.sql.gz"
[[ -f "$DUMP" ]] || { echo "FAIL missing dump: $DUMP"; exit 1; }
gzip -t "$DUMP"
echo "PASS gzip integrity"

ENV_FILE="${ENV_FILE:-/home/ubuntu/iic-booking-backend/.envs/.production/.django}"
PG_MAJOR="${PG_MAJOR:-15}"
PGIMG="postgres:${PG_MAJOR}"

set -a
# shellcheck disable=SC1090
source <(grep -E '^DATABASE_URL=' "$ENV_FILE" | sed 's/\r$//')
set +a
[[ -n "${DATABASE_URL:-}" ]] || { echo "FAIL DATABASE_URL missing"; exit 1; }

if [[ "${VERIFY_RESTORE_DB:-0}" != "1" ]]; then
  echo "SKIP live DB restore (set VERIFY_RESTORE_DB=1)"
  echo "PASS restore-verify (integrity only) $SRC"
  exit 0
fi

TMPDB="iic_restore_verify_$(date +%s)"
echo "Creating temp DB $TMPDB"
docker run --rm --network host \
  -e DATABASE_URL="$DATABASE_URL" \
  -e TMPDB="$TMPDB" \
  "$PGIMG" \
  bash -lc 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$TMPDB\";"'

VERIFY_URL=$(DATABASE_URL="$DATABASE_URL" TMPDB="$TMPDB" python3 - <<'PY'
import os
from urllib.parse import urlparse, urlunparse
u = urlparse(os.environ["DATABASE_URL"])
print(urlunparse((u.scheme, u.netloc, "/" + os.environ["TMPDB"], "", "", "")))
PY
)

echo "Restoring into $TMPDB ..."
if gunzip -c "$DUMP" | docker run --rm -i --network host \
  -e DATABASE_URL="$VERIFY_URL" \
  "$PGIMG" \
  bash -lc 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q' \
  >/tmp/iic-restore-verify.log 2>&1; then
  echo "PASS db test restore into $TMPDB"
else
  echo "FAIL db test restore (see /tmp/iic-restore-verify.log)"
  docker run --rm --network host \
    -e DATABASE_URL="$DATABASE_URL" \
    -e TMPDB="$TMPDB" \
    "$PGIMG" \
    bash -lc 'psql "$DATABASE_URL" -c "DROP DATABASE IF EXISTS \"$TMPDB\";"' || true
  exit 1
fi

echo "Dropping temp DB $TMPDB"
docker run --rm --network host \
  -e DATABASE_URL="$DATABASE_URL" \
  -e TMPDB="$TMPDB" \
  "$PGIMG" \
  bash -lc 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$TMPDB\";"'

echo "PASS restore-verify $SRC"
