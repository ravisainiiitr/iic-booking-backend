#!/usr/bin/env bash
# Shared helpers for Remote Analysis deployment scripts.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.ra-production.yml}"
COMPOSE_PROFILES="${COMPOSE_PROFILES:-guacamole}"
BACKUP_ROOT="${BACKUP_ROOT:-$REPO_ROOT/backups/deploy}"
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-$REPO_ROOT/.deploy-state}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.envs/.production/.django}"
PORTAL_BASE_URL="${PORTAL_BASE_URL:-http://127.0.0.1:8080}"

export COMPOSE_FILE
export COMPOSE_PROFILES

compose() {
  local args=(docker compose -f "$COMPOSE_FILE")
  if [[ -n "${COMPOSE_PROFILES}" ]]; then
    IFS=',' read -r -a profiles <<< "${COMPOSE_PROFILES}"
    for p in "${profiles[@]}"; do
      [[ -n "$p" ]] && args+=(--profile "$p")
    done
  fi
  "${args[@]}" "$@"
}

ts() { date -u +"%Y%m%dT%H%M%SZ"; }

ensure_dirs() {
  mkdir -p "$BACKUP_ROOT" "$DEPLOY_STATE_DIR"
}

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; }
