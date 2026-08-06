#!/usr/bin/env bash
# Verify a backup directory is readable and (optionally) test-restore DB into a temp name.
# Usage:
#   ./scripts/deploy/restore-verify.sh backups/deploy/<label>
#   VERIFY_RESTORE_DB=1 ./scripts/deploy/restore-verify.sh backups/deploy/<label>

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

SRC="${1:-}"
if [[ -z "$SRC" || ! -d "$SRC" ]]; then
  echo "Usage: $0 <backup-directory>"
  exit 2
fi

PASS=0
FAIL=0
ok() { pass "$*"; PASS=$((PASS + 1)); }
bad() { fail "$*"; FAIL=$((FAIL + 1)); }

echo "=== restore-verify: $SRC ==="

[[ -d "$SRC/config" ]] && ok "config dir" || bad "config dir missing"
[[ -f "$SRC/db/portal.sql.gz" ]] && ok "db dump present" || bad "db dump missing"
if [[ -f "$SRC/media/media.tar.gz" ]]; then
  ok "media archive present"
  tar tzf "$SRC/media/media.tar.gz" >/dev/null && ok "media archive readable" || bad "media archive corrupt"
else
  echo "SKIP  media archive"
fi

if [[ -f "$SRC/db/portal.sql.gz" ]]; then
  gzip -t "$SRC/db/portal.sql.gz" && ok "db gzip integrity" || bad "db gzip corrupt"
fi

if [[ "${VERIFY_RESTORE_DB:-0}" == "1" && -f "$SRC/db/portal.sql.gz" ]]; then
  TMPDB="ra_restore_verify_$(date +%s)"
  log "Creating temp DB $TMPDB"
  if compose ps postgres 2>/dev/null | grep -q postgres; then
    compose exec -T postgres sh -c "createdb -U \"\$POSTGRES_USER\" $TMPDB" || true
    if gunzip -c "$SRC/db/portal.sql.gz" | compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d $TMPDB" >/tmp/ra_restore_verify.log 2>&1; then
      ok "db test restore into $TMPDB"
    else
      bad "db test restore failed (see /tmp/ra_restore_verify.log)"
    fi
    compose exec -T postgres sh -c "dropdb -U \"\$POSTGRES_USER\" --if-exists $TMPDB" || true
  elif [[ -x /home/ubuntu/bin/iic-restore-verify.sh ]]; then
    VERIFY_RESTORE_DB=1 /home/ubuntu/bin/iic-restore-verify.sh "$SRC" && ok "db test restore (RDS helper)" || bad "db test restore (RDS helper)"
  else
    bad "no compose postgres and no /home/ubuntu/bin/iic-restore-verify.sh"
  fi
else
  echo "SKIP  live DB restore test (set VERIFY_RESTORE_DB=1)"
fi

echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
