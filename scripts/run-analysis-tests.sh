#!/usr/bin/env bash
# One-command Analysis Platform regression harness.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/venv/bin/python" ]]; then
  PYTHON="$ROOT/venv/bin/python"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python"
fi

REPORT_DIR="$ROOT/tests/analysis_platform/report"
mkdir -p "$REPORT_DIR"
JUNIT="$REPORT_DIR/pytest-junit.xml"

PERF=0
LAB=0
E2E=0
SKIP_CLEANUP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --perf) PERF=1 ;;
    --lab) LAB=1 ;;
    --e2e) E2E=1 ;;
    --skip-cleanup) SKIP_CLEANUP=1 ;;
    --agent-id) export ANALYSIS_AGENT_ID="$2"; shift ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
  shift
done

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.test}"

MARKERS="analysis_platform"
if [[ "$PERF" -eq 1 ]]; then
  export ANALYSIS_PERF=1
  MARKERS="$MARKERS or analysis_perf"
fi
if [[ "$LAB" -eq 1 ]]; then
  export ANALYSIS_LAB=1
  MARKERS="$MARKERS or analysis_lab"
fi

echo "=== Analysis Platform Test Harness ==="
echo "Root: $ROOT"

set +e
"$PYTHON" -m pytest -m "$MARKERS" tests/analysis_platform --junitxml="$JUNIT" -q
PYTEST_EXIT=$?
set -e

"$PYTHON" - <<PY
from pathlib import Path
from tests.analysis_platform.reporting import write_dashboard
out = write_dashboard(
    report_dir=Path(r"$REPORT_DIR"),
    junit_path=Path(r"$JUNIT"),
    metrics={"runner": "run-analysis-tests.sh"},
)
s = out["summary"]
print("DASHBOARD", out["html"])
print(f"PASSED {s['passed']} FAILED {s['failed']} SKIPPED {s['skipped']}")
PY

if [[ "$E2E" -eq 1 ]]; then
  export ANALYSIS_E2E=1
  E2E_DIR="$ROOT/tests/analysis_platform/e2e"
  pushd "$E2E_DIR" >/dev/null
  if [[ ! -d node_modules ]]; then
    npm install
    npx playwright install chromium
  fi
  set +e
  npx playwright test
  E2E_EXIT=$?
  set -e
  popd >/dev/null
  if [[ "$E2E_EXIT" -ne 0 ]]; then PYTEST_EXIT=$E2E_EXIT; fi
else
  echo "[e2e] skipped (pass --e2e)"
fi

if [[ "$SKIP_CLEANUP" -eq 0 && "${ANALYSIS_CLEANUP:-}" == "1" ]]; then
  "$PYTHON" -c "import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.test'); django.setup(); from tests.analysis_platform.utils.cleanup import cleanup_apt_prefix; print('cleaned', cleanup_apt_prefix())"
fi

echo "JUnit: $JUNIT"
echo "HTML:  $REPORT_DIR/dashboard.html"
exit "$PYTEST_EXIT"
