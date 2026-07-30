#!/usr/bin/env bash
# Production acceptance: health + optional toolkit / Guacamole probes.
# Usage:
#   ./scripts/deploy/verify-production.sh
#   PORTAL_BASE_URL=https://… ADMIN_TOKEN=… ./scripts/deploy/verify-production.sh
#   RUN_SELF_TEST=1 ADMIN_TOKEN=… ./scripts/deploy/verify-production.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PASS=0
FAIL=0
do_pass() { pass "$*"; PASS=$((PASS + 1)); }
do_fail() { fail "$*"; FAIL=$((FAIL + 1)); }

BASE="${PORTAL_BASE_URL%/}"
TOKEN="${ADMIN_TOKEN:-${TOKEN:-}}"
SKIP_GUAC="${SKIP_GUACAMOLE:-0}"

echo "=== verify-production ==="
echo "BaseUrl=$BASE"

# Portal / RA health
code=$(curl -sS -o /tmp/ra_v_live.json -w "%{http_code}" --max-time 20 "$BASE/api/v1/analysis/health/live/" || true)
[[ "$code" == "200" ]] && do_pass "RA liveness" || do_fail "RA liveness http=$code"

code=$(curl -sS -o /tmp/ra_v_ready.json -w "%{http_code}" --max-time 20 "$BASE/api/v1/analysis/health/ready/" || true)
if [[ "$code" == "200" ]] && grep -q '"status": "ready"' /tmp/ra_v_ready.json 2>/dev/null; then
  do_pass "RA readiness"
else
  do_fail "RA readiness http=$code $(tr -d '\n' </tmp/ra_v_ready.json 2>/dev/null | head -c 200)"
fi

code=$(curl -sS -o /tmp/ra_v_health.json -w "%{http_code}" --max-time 20 "$BASE/api/v1/analysis/health/" || true)
[[ "$code" == "200" ]] && do_pass "RA combined health" || do_fail "RA combined health http=$code"

# Container-side dependency validation when compose available
if docker compose -f "$COMPOSE_FILE" ps >/dev/null 2>&1; then
  if compose run --rm --no-deps django python manage.py validate_deployment_startup ${SKIP_GUAC:+--skip-guacamole} --strict; then
    do_pass "validate_deployment_startup"
  else
    do_fail "validate_deployment_startup"
  fi
else
  echo "SKIP  compose validate_deployment_startup (compose not available)"
fi

# Redis via readiness JSON if present
if grep -q '"cache": "ok"' /tmp/ra_v_ready.json 2>/dev/null || grep -q '"database": "ok"' /tmp/ra_v_ready.json 2>/dev/null; then
  do_pass "Readiness dependency checks (see JSON)"
fi

# Guacamole probe from readiness
if [[ "$SKIP_GUAC" == "1" ]]; then
  echo "SKIP  Guacamole (SKIP_GUACAMOLE=1)"
else
  if grep -q '"guacamole": "ok"' /tmp/ra_v_ready.json 2>/dev/null; then
    do_pass "Guacamole (readiness ok)"
  elif grep -q '"guacamole": "mock' /tmp/ra_v_ready.json 2>/dev/null; then
    do_fail "Guacamole still mock in production readiness"
  else
    do_fail "Guacamole not ok in readiness payload"
  fi
fi

# Authenticated toolkit checks
if [[ -n "$TOKEN" ]]; then
  AUTH=(-H "Authorization: Token $TOKEN")
  # Prefer Token; Bearer also used in some clients — try Token first
  code=$(curl -sS -o /tmp/ra_v_dash.json -w "%{http_code}" --max-time 60 \
    "${AUTH[@]}" "$BASE/api/v1/analysis/operations/toolkit/dashboard/" || true)
  if [[ "$code" != "200" ]]; then
    AUTH=(-H "Authorization: Bearer $TOKEN")
    code=$(curl -sS -o /tmp/ra_v_dash.json -w "%{http_code}" --max-time 60 \
      "${AUTH[@]}" "$BASE/api/v1/analysis/operations/toolkit/dashboard/" || true)
  fi
  [[ "$code" == "200" ]] && do_pass "Toolkit dashboard" || do_fail "Toolkit dashboard http=$code"

  if [[ "${RUN_CONNECTIVITY:-0}" == "1" ]]; then
    code=$(curl -sS -o /tmp/ra_v_conn.json -w "%{http_code}" --max-time 180 \
      -X POST "${AUTH[@]}" -H "Content-Type: application/json" -d '{}' \
      "$BASE/api/v1/analysis/operations/toolkit/connectivity/" || true)
    [[ "$code" == "200" ]] && do_pass "Toolkit connectivity" || do_fail "Toolkit connectivity http=$code"
  else
    echo "SKIP  Toolkit connectivity (set RUN_CONNECTIVITY=1)"
  fi

  if [[ "${RUN_SELF_TEST:-0}" == "1" ]]; then
    code=$(curl -sS -o /tmp/ra_v_self.json -w "%{http_code}" --max-time 300 \
      -X POST "${AUTH[@]}" -H "Content-Type: application/json" -d '{}' \
      "$BASE/api/v1/analysis/operations/toolkit/self-test/" || true)
    [[ "$code" == "200" ]] && do_pass "Toolkit self-test" || do_fail "Toolkit self-test http=$code"
  else
    echo "SKIP  Toolkit self-test (set RUN_SELF_TEST=1)"
  fi
else
  echo "SKIP  Authenticated toolkit checks (set ADMIN_TOKEN)"
fi

# Celery containers (when compose stack is local)
if docker compose -f "$COMPOSE_FILE" ps >/dev/null 2>&1; then
  if compose ps --status running 2>/dev/null | grep -q celeryworker; then
    do_pass "Celery worker running"
  else
    do_fail "Celery worker not running"
  fi
  if compose ps --status running 2>/dev/null | grep -q celerybeat; then
    do_pass "Celery beat running"
  else
    do_fail "Celery beat not running"
  fi
fi

# Flower optional
if [[ "${CHECK_FLOWER:-0}" == "1" ]]; then
  fcode=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "${FLOWER_URL:-http://127.0.0.1:5555}/" || true)
  [[ "$fcode" == "200" || "$fcode" == "302" ]] && do_pass "Flower" || do_fail "Flower http=$fcode"
fi

echo ""
echo "======== VERIFY SUMMARY ========"
echo "PASS=$PASS FAIL=$FAIL"
echo "================================"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
