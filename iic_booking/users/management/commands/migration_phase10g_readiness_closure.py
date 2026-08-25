"""Phase 10G — production readiness closure report (READ-ONLY)."""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    VERDICT_READY,
    build_phase10g_final_readiness,
    build_release_audit,
    build_schema_readiness,
    write_json_artifact,
)

ARTIFACT_DIR = Path("docs/release/migration")


class Command(BaseCommand):
    help = (
        "Phase 10G read-only readiness closure. "
        "Does NOT migrate, activate T0, freeze, email, refund, or cleanup."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument("--default-artifact", action="store_true")
        parser.add_argument("--backup-verified", action="store_true")
        parser.add_argument("--backend-release-tag", type=str, default="")
        parser.add_argument("--backend-merge-sha", type=str, default="")
        parser.add_argument("--backend-pr", type=str, default="")
        parser.add_argument("--frontend-release-tag", type=str, default="")
        parser.add_argument("--frontend-merge-sha", type=str, default="")
        parser.add_argument("--frontend-pr", type=str, default="")
        parser.add_argument("--conflicts-resolved", action="store_true")
        parser.add_argument(
            "--finance-exceptions-blocking",
            type=str,
            default="",
            help="yes|no — operator classification after finance review",
        )
        parser.add_argument(
            "--explicit-t0-authorization",
            action="store_true",
            help="ONLY set when a separate T0 authorization exists (Phase 10G should leave this false)",
        )

    def handle(self, *args, **options):
        finance_raw = (options.get("finance_exceptions_blocking") or "").strip().lower()
        finance_flag = None
        if finance_raw in ("yes", "true", "1"):
            finance_flag = True
        elif finance_raw in ("no", "false", "0"):
            finance_flag = False

        report = build_phase10g_final_readiness(
            backup_verified=bool(options.get("backup_verified")),
            backend_release_tag=(options.get("backend_release_tag") or "").strip(),
            backend_merge_sha=(options.get("backend_merge_sha") or "").strip(),
            frontend_release_tag=(options.get("frontend_release_tag") or "").strip(),
            frontend_merge_sha=(options.get("frontend_merge_sha") or "").strip(),
            backend_pr=(options.get("backend_pr") or "").strip(),
            frontend_pr=(options.get("frontend_pr") or "").strip(),
            conflicts_resolved_or_excluded=bool(options.get("conflicts_resolved")),
            finance_exceptions_blocking=finance_flag,
            explicit_t0_authorization=bool(options.get("explicit_t0_authorization")),
        )

        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        base = Path(getattr(settings, "BASE_DIR", "."))
        if options.get("default_artifact"):
            write_json_artifact(base / ARTIFACT_DIR / "phase10g_final_readiness.json", report)
            write_json_artifact(
                base / ARTIFACT_DIR / "phase10g_go_no_go.json",
                {
                    "verdict": report["verdict"],
                    "t0_executed": False,
                    "blockers": report.get("blockers"),
                    "gate_matrix": report.get("gate_matrix"),
                    "production_safety": report.get("production_safety"),
                    "generated_at_utc": report.get("generated_at_utc"),
                },
            )
            write_json_artifact(
                base / ARTIFACT_DIR / "phase10g_operator_checklist.json",
                {
                    "operator_next_actions": report.get("operator_next_actions"),
                    "explicit_t0_authorization_required": True,
                    "production_safety": report.get("production_safety"),
                },
            )
            write_json_artifact(base / ARTIFACT_DIR / "phase10g_schema_readiness.json", build_schema_readiness())
            write_json_artifact(
                base / ARTIFACT_DIR / "datetime_contract_review.json",
                report.get("datetime_contract_review") or {},
            )
            write_json_artifact(
                base / ARTIFACT_DIR / "production_release_plan.json",
                {
                    "sequence": [
                        "backend release review",
                        "frontend release review",
                        "deploy backend (no auto-migrate)",
                        "deploy frontend",
                        "verify health + hard-OFF",
                        "showmigrations + migrate --plan",
                        "explicit MIGRATE approval → users.0101–0104",
                        "datetime validation + Main Admin approve",
                        "equipment mapping approval",
                        "discovery + conflicts",
                        "wallet/finance review",
                        "backup verified",
                        "migration_final_t0_readiness",
                        "SEPARATE explicit T0 authorization",
                    ],
                    "executed": False,
                    "release_audit": build_release_audit(),
                },
            )
            write_json_artifact(
                base / ARTIFACT_DIR / "production_backup_readiness.json",
                {
                    "backup_verified": bool(options.get("backup_verified")),
                    "status": "PASS" if options.get("backup_verified") else "BLOCKED",
                    "note": "Operator must verify RDS/PostgreSQL snapshot before --backup-verified",
                    "t0_refuses_without_backup": True,
                },
            )
            # Placeholder discovery artifacts — filled when production MySQL reachable
            for name, body in (
                ("legacy_datetime_validation.json", report.get("datetime_validation") or {}),
                (
                    "production_upcoming_booking_discovery.json",
                    {
                        "ok": False,
                        "status": "BLOCKED",
                        "reason": "Requires datetime APPROVED + production MySQL + schema 0101–0104",
                        "writes": 0,
                    },
                ),
                (
                    "equipment_mapping_candidates.json",
                    {
                        "status": "OPERATOR REQUIRED",
                        "explicit_mappings": 0,
                        "fuzzy_matching": False,
                        "note": "No auto-map; Main Admin must approve explicit mappings",
                    },
                ),
                (
                    "production_user_mapping.json",
                    {
                        "identity_key": "Channel-I emp_id only",
                        "forbidden_keys": ["email", "name", "phone", "username"],
                        "user_unresolved_blocks_t0": False,
                        "status": "OPERATOR REQUIRED",
                    },
                ),
                (
                    "production_wallet_reconciliation.json",
                    {
                        "status": "OPERATOR REQUIRED",
                        "writes": 0,
                        "note": "Run existing read-only wallet reconciliation on production",
                    },
                ),
                (
                    "finance_exception_register.json",
                    {
                        "finance_exceptions_blocking": report.get("finance_exceptions_blocking"),
                        "status": "OPERATOR REQUIRED",
                        "auto_correction": False,
                    },
                ),
                (
                    "production_test_account_review.json",
                    {
                        "writes_performed": 0,
                        "cleanup_executed": False,
                        "basis": "is_test_account=True only",
                    },
                ),
                (
                    "production_email_dry_run.json",
                    {
                        "emails_sent": 0,
                        "templates": ["FACULTY", "STUDENT", "OIC", "ADMIN"],
                        "smtp": False,
                    },
                ),
            ):
                write_json_artifact(base / ARTIFACT_DIR / name, body)
            self.stdout.write(self.style.SUCCESS(f"Wrote Phase 10G artifacts under {base / ARTIFACT_DIR}"))

        out = (options.get("json_out") or "").strip()
        if out:
            Path(out).write_text(payload + "\n", encoding="utf-8")

        if report["verdict"] == VERDICT_READY:
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))

        safety = report.get("production_safety") or {}
        self.stdout.write("")
        for k, v in safety.items():
            self.stdout.write(f"  {k}: {v}")
