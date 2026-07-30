#!/usr/bin/env bash
# Remote Analysis portal health check (Linux/macOS). Safe to re-run.
# Usage: ./HealthCheck.sh https://booking.example.edu [BEARER_TOKEN]

set -u
BASE_URL="${1:-}"
TOKEN="${2:-}"
if [[ -z "$BASE_URL" ]]; then
  echo "Usage: $0 <BaseUrl> [BearerToken]"
  exit 2
fi
BASE_URL="${BASE_URL%/}"
PASS=0
FAIL=0

pass() { echo "PASS  $1 ${2:-}"; PASS=$((PASS+1)); }
fail() { echo "FAIL  $1 ${2:-}"; FAIL=$((FAIL+1)); }

echo "=== Remote Analysis HealthCheck ==="
echo "BaseUrl=$BASE_URL"

code=$(curl -sS -o /tmp/ra_live.json -w "%{http_code}" --max-time 30 "$BASE_URL/api/v1/analysis/health/live/" || true)
if [[ "$code" == "200" ]] && grep -q '"status": "ok"' /tmp/ra_live.json 2>/dev/null; then
  pass "Liveness" "http=$code"
else
  fail "Liveness" "http=$code body=$(head -c 200 /tmp/ra_live.json 2>/dev/null)"
fi

code=$(curl -sS -o /tmp/ra_ready.json -w "%{http_code}" --max-time 30 "$BASE_URL/api/v1/analysis/health/ready/" || true)
if [[ "$code" == "200" ]] && grep -q '"status": "ready"' /tmp/ra_ready.json 2>/dev/null; then
  pass "Readiness" "http=$code $(tr -d '\n' < /tmp/ra_ready.json | head -c 300)"
else
  fail "Readiness" "http=$code $(tr -d '\n' < /tmp/ra_ready.json 2>/dev/null | head -c 400)"
fi

code=$(curl -sS -o /tmp/ra_health.json -w "%{http_code}" --max-time 30 "$BASE_URL/api/v1/analysis/health/" || true)
if [[ "$code" == "200" ]]; then
  pass "Combined health" "http=$code"
else
  fail "Combined health" "http=$code"
fi

if [[ -n "$TOKEN" ]]; then
  AUTH=(-H "Authorization: Bearer $TOKEN")
  code=$(curl -sS -o /tmp/ra_diag.json -w "%{http_code}" --max-time 30 "${AUTH[@]}" "$BASE_URL/api/v1/analysis/operations/diagnostics/" || true)
  if [[ "$code" == "200" ]]; then
    pass "Diagnostics API" "http=$code"
    if command -v python3 >/dev/null 2>&1; then
      python3 - <<'PY' || fail "Diagnostics parse" "python error"
import json
p=json.load(open("/tmp/ra_diag.json"))
print("  mock_guacamole=", p.get("settings",{}).get("mock_guacamole"))
print("  DEBUG=", p.get("django",{}).get("DEBUG"))
print("  storage_writable=", p.get("storage",{}).get("workspace_root_writable"))
print("  workstations=", len(p.get("workstations") or []))
print("  queue_length=", (p.get("scheduler") or {}).get("queue_length"))
print("  warnings=", p.get("warnings"))
if p.get("settings",{}).get("mock_guacamole"):
    raise SystemExit(1)
if p.get("django",{}).get("DEBUG"):
    raise SystemExit(2)
if not p.get("storage",{}).get("workspace_root_writable"):
    raise SystemExit(3)
PY
      if [[ $? -eq 0 ]]; then
        pass "Production flags (mock/DEBUG/storage)" ""
      else
        fail "Production flags (mock/DEBUG/storage)" "see diagnostics warnings"
      fi
    fi
  else
    fail "Diagnostics API" "http=$code"
  fi

  code=$(curl -sS -o /tmp/ra_ops.json -w "%{http_code}" --max-time 30 "${AUTH[@]}" "$BASE_URL/api/v1/analysis/operations/dashboard/" || true)
  [[ "$code" == "200" ]] && pass "Operations dashboard" || fail "Operations dashboard" "http=$code"

  code=$(curl -sS -o /tmp/ra_ws.json -w "%{http_code}" --max-time 30 "${AUTH[@]}" "$BASE_URL/api/v1/analysis/workspaces/dashboard/" || true)
  [[ "$code" == "200" ]] && pass "Workspace dashboard" || fail "Workspace dashboard" "http=$code"
else
  echo "SKIP  Authenticated checks (pass bearer token as arg 2)"
fi

echo ""
echo "Summary: PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
