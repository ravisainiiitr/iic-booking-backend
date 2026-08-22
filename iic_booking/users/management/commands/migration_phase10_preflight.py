"""
Phase 10.1 — Pre-deploy local/CI safety check (no database required).

Verifies Phase 8A/8B/8C artifacts, migrations 0101–0103, forbidden absences.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand

REQUIRED_FILES = [
    "iic_booking/users/legacy_ledger/migration_refund.py",
    "iic_booking/users/legacy_ledger/equipment_mapping.py",
    "iic_booking/users/legacy_ledger/booking_bridge.py",
    "iic_booking/users/legacy_ledger/migration_emails.py",
    "iic_booking/users/legacy_ledger/migration_notifications.py",
    "iic_booking/users/legacy_ledger/migration_t0.py",
    "iic_booking/users/legacy_ledger/legacy_booking_mysql.py",
    "iic_booking/users/migrations/0101_migration_booking_settlement.py",
    "iic_booking/users/migrations/0102_legacy_equipment_booking_bridge.py",
    "iic_booking/users/migrations/0103_migration_notification_batch.py",
    "iic_booking/users/management/commands/migration_production_t0_readiness.py",
    "iic_booking/users/management/commands/migration_production_legacy_qualification.py",
    "iic_booking/users/management/commands/migration_cleanup_test_accounts.py",
    "iic_booking/users/management/commands/migration_reconcile_legacy_blocks.py",
    "iic_booking/users/management/commands/migration_abort_batch.py",
    "iic_booking/users/tests/test_migration_refund_settlement.py",
    "iic_booking/users/tests/test_phase8b_legacy_booking_bridge.py",
    "iic_booking/users/tests/test_phase8c_staging_simulation.py",
    "iic_booking/users/tests/test_real_integration_preflight.py",
    "iic_booking/users/tests/test_real_integration_activation.py",
]

FORBIDDEN_GLOBS = [
    "iic_booking/equipment/migrations/*0188*",
    "docs/release/phase-R14/**",
    "iic_booking/**/test_r14_*",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def run_preflight(*, run_tests: bool = False) -> dict:
    root = _repo_root()
    missing = [p for p in REQUIRED_FILES if not (root / p).exists()]
    forbidden_hits = []
    for pattern in FORBIDDEN_GLOBS:
        if list(root.glob(pattern)):
            forbidden_hits.append(pattern)

    test_results = {"skipped": True, "reason": "run_tests=False"}
    if run_tests and not missing:
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "iic_booking/users/tests/test_migration_refund_settlement.py",
            "iic_booking/users/tests/test_phase8b_legacy_booking_bridge.py",
            "iic_booking/users/tests/test_phase8c_staging_simulation.py",
            "iic_booking/users/tests/test_real_integration_preflight.py",
            "iic_booking/users/tests/test_real_integration_activation.py",
            "-q",
            "--tb=no",
        ]
        try:
            proc = subprocess.run(cmd, cwd=str(root / "iic_booking"), capture_output=True, text=True, timeout=600)
            test_results = {
                "exit_code": proc.returncode,
                "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
                "stderr_tail": proc.stderr[-1000:] if proc.stderr else "",
                "pass": proc.returncode == 0,
            }
        except Exception as exc:
            test_results = {"pass": False, "error": str(exc)}

    sha = ""
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except (OSError, subprocess.SubprocessError):
        pass

    ok = not missing and not forbidden_hits and test_results.get("pass", True)
    return {
        "ok": ok,
        "git_sha": sha,
        "required_files_present": len(REQUIRED_FILES) - len(missing),
        "required_files_total": len(REQUIRED_FILES),
        "missing_files": missing,
        "forbidden_hits": forbidden_hits,
        "migrations_present": {
            "0101": (root / "iic_booking/users/migrations/0101_migration_booking_settlement.py").exists(),
            "0102": (root / "iic_booking/users/migrations/0102_legacy_equipment_booking_bridge.py").exists(),
            "0103": (root / "iic_booking/users/migrations/0103_migration_notification_batch.py").exists(),
        },
        "tests": test_results,
        "verdict": "PREFLIGHT PASS" if ok else "PREFLIGHT BLOCKED",
    }


class Command(BaseCommand):
    help = "Phase 10.1 pre-deploy file/migration safety check (optional --run-tests)."

    def add_arguments(self, parser):
        parser.add_argument("--run-tests", action="store_true", help="Run Phase 8 + REAL pytest suites")
        parser.add_argument("--json-out", type=str, default="")

    def handle(self, *args, **options):
        report = run_preflight(run_tests=options.get("run_tests"))
        payload = json.dumps(report, indent=2)
        self.stdout.write(payload)
        out = (options.get("json_out") or "").strip()
        if out:
            Path(out).write_text(payload, encoding="utf-8")
        if report["ok"]:
            self.stdout.write(self.style.SUCCESS(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))
            for m in report.get("missing_files") or []:
                self.stdout.write(f"  missing: {m}")
            for f in report.get("forbidden_hits") or []:
                self.stdout.write(f"  forbidden: {f}")
