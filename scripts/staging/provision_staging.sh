#!/usr/bin/env bash
# Provision isolated local/staging Docker stack.
# HARD STOP if DATABASE_URL looks like production RDS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ENV_FILE="${STAGING_ENV_FILE:-.envs/.staging/.django}"
COMPOSE=(docker compose -f docker-compose.staging.yml --env-file "$ENV_FILE")

die() { echo "ERROR: $*" >&2; exit 1; }

echo "=== STAGING PROVISION (isolated) ==="
echo "repo=$ROOT"
echo "env_file=$ENV_FILE"

[[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE — copy docs/release/migration/sample.env.staging first"

# Safety: refuse production markers in env file
if grep -Eiq 'iic-booking-rds\.cvs75htsmowj|equip\.iitr\.ac\.in' "$ENV_FILE"; then
  # FRONTEND_URL must not be production; allow commenting about it in docs only
  if grep -Eiq '^[[:space:]]*DATABASE_URL=.*iic-booking-rds' "$ENV_FILE"; then
    die "SAFETY STOP: DATABASE_URL points at production RDS"
  fi
  if grep -Eiq '^[[:space:]]*FRONTEND_URL=https?://equip\.iitr\.ac\.in' "$ENV_FILE"; then
    die "SAFETY STOP: FRONTEND_URL is production portal"
  fi
fi

# Require expected staging DB name
if ! grep -Eq '^[[:space:]]*POSTGRES_DB=iic_booking_staging[[:space:]]*$' "$ENV_FILE"; then
  die "SAFETY STOP: POSTGRES_DB must be iic_booking_staging"
fi

command -v docker >/dev/null || die "docker not installed"
docker info >/dev/null 2>&1 || die "docker daemon not running — start Docker Desktop / dockerd"

EXPECTED_COMMIT="${EXPECTED_BACKEND_COMMIT:-f7783f9}"
HEAD="$(git rev-parse --short HEAD)"
echo "git_head=$HEAD (expected prefix $EXPECTED_COMMIT)"
if [[ "$HEAD" != "$EXPECTED_COMMIT"* && "$(git rev-parse HEAD)" != "$EXPECTED_COMMIT"* ]]; then
  echo "WARNING: HEAD is not $EXPECTED_COMMIT — continuing only if STAGING_ALLOW_OTHER_COMMIT=1"
  [[ "${STAGING_ALLOW_OTHER_COMMIT:-0}" == "1" ]] || die "Checkout $EXPECTED_COMMIT before staging provision"
fi

export BACKEND_GIT_COMMIT="$(git rev-parse --short HEAD)"
export GIT_SHA="$BACKEND_GIT_COMMIT"

echo "=== Building & starting staging compose project: iic-booking-staging ==="
"${COMPOSE[@]}" up -d --build

echo "=== Waiting for django healthy ==="
for i in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T django python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/v1/analysis/health/ready/', timeout=3)" 2>/dev/null; then
    echo "django ready"
    break
  fi
  sleep 5
  [[ $i -eq 60 ]] && die "django did not become ready"
done

echo "=== Applying migrations on STAGING postgres only ==="
"${COMPOSE[@]}" exec -T django python manage.py migrate --noinput
"${COMPOSE[@]}" exec -T django python manage.py showmigrations users | tee /tmp/staging_showmigrations_users.txt || true
"${COMPOSE[@]}" exec -T django python manage.py showmigrations equipment | tee /tmp/staging_showmigrations_equipment.txt || true

echo "=== Database identity proof ==="
"${COMPOSE[@]}" exec -T django python - <<'PY'
import json, os
from django.db import connection
from datetime import datetime, timezone
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.staging")
import django
django.setup()
with connection.cursor() as c:
    c.execute("SELECT current_database(), inet_server_addr()::text, version()")
    db, addr, ver = c.fetchone()
payload = {
    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "environment": "STAGING",
    "database_name": db,
    "inet_server_addr": addr,
    "version": ver.split("\n")[0],
    "database_url_host_hint": (os.environ.get("DATABASE_URL") or "").split("@")[-1],
    "production_rds_marker_present": "iic-booking-rds" in (os.environ.get("DATABASE_URL") or ""),
    "safety_result": "PASS" if db == "iic_booking_staging" and "iic-booking-rds" not in (os.environ.get("DATABASE_URL") or "") else "FAIL",
}
print(json.dumps(payload, indent=2))
open("/tmp/staging_database_preflight.json", "w").write(json.dumps(payload, indent=2))
PY

echo "=== Staging provision complete (API http://127.0.0.1:8180) ==="
echo "PRODUCTION WRITES = NO (this script never targets production compose/RDS)"
