#!/usr/bin/env bash
# Fail-fast wrapper for validate_deployment_startup inside compose or local venv.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

EXTRA=()
[[ "${STRICT:-1}" == "1" ]] && EXTRA+=(--strict)
[[ "${SKIP_GUACAMOLE:-0}" == "1" ]] && EXTRA+=(--skip-guacamole)

if compose ps >/dev/null 2>&1; then
  compose run --rm --no-deps django python manage.py validate_deployment_startup "${EXTRA[@]}"
elif [[ -x "$REPO_ROOT/venv/Scripts/python.exe" ]]; then
  "$REPO_ROOT/venv/Scripts/python.exe" "$REPO_ROOT/manage.py" validate_deployment_startup "${EXTRA[@]}"
elif [[ -x "$REPO_ROOT/venv/bin/python" ]]; then
  "$REPO_ROOT/venv/bin/python" "$REPO_ROOT/manage.py" validate_deployment_startup "${EXTRA[@]}"
else
  python manage.py validate_deployment_startup "${EXTRA[@]}"
fi
