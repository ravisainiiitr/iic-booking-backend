"""Phase 10L — operator-gated production migration (READ-ONLY consolidation). No T0."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10l_readiness_closure import (
    build_phase10l_final_readiness,
    build_release_candidate_prep,
    write_phase10l_artifacts,
)


class Command(BaseCommand):
    help = (
        "Phase 10L read-only operator-gated production migration consolidation. "
        "Continues independent RO/prep stages when some gates are OPERATOR_REQUIRED. "
        "Does NOT migrate, activate T0, freeze, email, refund, cleanup, approve datetime, "
        "invent window dates, or run discovery unless datetime APPROVED AND window configured."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument("--default-artifact", action="store_true")
        parser.add_argument("--backup-verified", action="store_true")
        parser.add_argument("--finance-reviewed", action="store_true")
        parser.add_argument("--schema-migrate-authorized", action="store_true")
        parser.add_argument("--equipment-mapping-authorized", action="store_true")

    def handle(self, *args, **options):
        from iic_booking.users.legacy_ledger.legacy_datetime_validation import (
            validate_legacy_datetime_readonly,
        )
        from iic_booking.users.legacy_ledger.legacy_equipment_inventory import (
            fetch_legacy_equipment_inventory,
        )
        from iic_booking.users.legacy_ledger.phase10h_readiness_closure import _probe_mysql
        from iic_booking.users.legacy_ledger.phase10i_readiness_closure import (
            build_datetime_review as build_10i_datetime_review,
        )
        from iic_booking.users.legacy_ledger.phase10j_readiness_closure import (
            inspect_operator_gates,
        )
        from iic_booking.users.legacy_ledger.test_account_dry_run import test_account_cleanup_dry_run

        datetime_validation = validate_legacy_datetime_readonly()
        mysql_probe = _probe_mysql()
        datetime_review = build_10i_datetime_review(datetime_validation=datetime_validation)
        datetime_review["phase"] = "10L"

        gates = inspect_operator_gates(
            backup_verified=bool(options.get("backup_verified")),
            release_reviewed=False,
            schema_migrate_authorized=bool(options.get("schema_migrate_authorized")),
            equipment_mapping_authorized=bool(options.get("equipment_mapping_authorized")),
            finance_reviewed=bool(options.get("finance_reviewed")),
            datetime_validation=datetime_validation,
            explicit_mappings=0,
        )

        self.stdout.write(
            self.style.WARNING(
                "Phase 10L state machine — continuing independent RO/prep even if "
                f"datetime={gates['datetime_contract_approval']['status']} "
                f"window_configured={gates['migration_window']['configured']}"
            )
        )
        if not gates["discovery_allowed"]:
            self.stdout.write(
                "Discovery BLOCKED — exact datetime: UI /admin/portal-migration or "
                'POST /api/portal-migration/admin/datetime-contract/ {"confirm":true,"approval_reason":"..."}'
            )
            self.stdout.write(
                "Exact window: UI Phase 8B or PATCH /api/portal-migration/admin/state/ "
                "(migration_start_at + migration_window_end_at) — dates NOT invented"
            )
            self.stdout.write("POST datetime-contract: NOT called")

        null_rows: list = []
        zero_rows: list = []
        wallet_reconciliation = None
        finance_register = None
        try:
            from iic_booking.users.legacy_ledger.reader import OldMySQLReader

            with OldMySQLReader() as reader:
                null_rows = reader.fetchall(
                    """
                    SELECT id, user_id, equipment_id, booking_date, time_required,
                           status, is_deleted, is_active, charge
                    FROM booking WHERE booking_date IS NULL ORDER BY id
                    """
                )
                zero_rows = reader.fetchall(
                    """
                    SELECT id, user_id, equipment_id, booking_date, time_required,
                           status, is_deleted, is_active, charge
                    FROM booking WHERE time_required = 0 ORDER BY id
                    """
                )
                for row in null_rows + zero_rows:
                    if hasattr(row.get("booking_date"), "isoformat"):
                        row["booking_date"] = row["booking_date"].isoformat()
                    if row.get("charge") is not None:
                        row["charge"] = str(row["charge"])

                datetime_review = build_10i_datetime_review(
                    datetime_validation=datetime_validation,
                    null_rows=null_rows,
                    zero_rows=zero_rows,
                )
                datetime_review["phase"] = "10L"
                datetime_review["approval_endpoint_called"] = False

                audit = reader.live_financial_audit()
                probe = reader.connection_probe()
                orphans = reader.fetchone(
                    """
                    SELECT COUNT(*) AS c FROM user_wallet w
                    LEFT JOIN users u ON u.id = w.user_id
                    WHERE u.id IS NULL
                    """
                )
                mism_count = reader.fetchone(
                    """
                    SELECT COUNT(*) AS c FROM (
                      SELECT w.id, w.balance AS sb,
                        (SELECT COALESCE(SUM(
                          CASE WHEN transaction_type=1 THEN ABS(amount)
                               WHEN transaction_type=2 THEN -ABS(amount)
                               ELSE 0 END), 0)
                         FROM wallet_transactions t WHERE t.user_id = w.user_id) AS lb
                      FROM user_wallet w
                      HAVING ABS(sb - lb) > 0.01
                    ) x
                    """
                )
                booking_total = reader.fetchone("SELECT COUNT(*) AS c FROM booking")
                ledger = audit.get("calculated_closing_balance")
                stored = audit.get("sum_user_wallet_balance_column")
                try:
                    gap = float(ledger) - float(stored)
                except (TypeError, ValueError):
                    gap = None
                txn_range = probe.get("wallet_transaction_id_range") or {}
                wallet_reconciliation = {
                    "ok": True,
                    "audit_mode": "READ_ONLY",
                    "writes": 0,
                    "phase": "10L",
                    "acceptability_decided": False,
                    "compared_to_phase10k": {
                        "wallets": 1748,
                        "transactions": 64321,
                        "max_transaction_id": 64327,
                        "mismatch_count": 41,
                        "orphan_wallets": 18,
                    },
                    "wallet_count": audit.get("wallet_count"),
                    "transaction_count": audit.get("transaction_count"),
                    "max_transaction_id": txn_range.get("max_id"),
                    "watermark": txn_range.get("max_id"),
                    "ledger_balance": ledger,
                    "stored_balance": stored,
                    "ledger_vs_stored_gap": gap,
                    "mismatch_count": int((mism_count or {}).get("c") or 0),
                    "orphan_wallets": int((orphans or {}).get("c") or 0),
                    "booking_total": int((booking_total or {}).get("c") or 0),
                    "live_user_audit": {
                        "users_total": audit.get("users_total"),
                        "users_with_employee_id": audit.get("users_with_employee_id"),
                        "users_without_employee_id": audit.get("users_without_employee_id"),
                        "duplicate_employee_id_groups": audit.get("duplicate_employee_id_groups"),
                        "duplicate_employee_id_rows": audit.get("duplicate_employee_id_rows"),
                    },
                    "suspicious_transactions": {
                        "outlier_abs_gt_10m": audit.get("outlier_abs_gt_10m"),
                    },
                    "auto_correction": False,
                    "note": (
                        "Finance exception register only — do not decide acceptability, "
                        "correct balances, or create opening balances."
                    ),
                }
                finance_register = {
                    "phase": "10L",
                    "auto_correction": False,
                    "writes": 0,
                    "acceptability_decided": False,
                    "mismatch_count": wallet_reconciliation["mismatch_count"],
                    "orphan_wallets": wallet_reconciliation["orphan_wallets"],
                    "ledger_vs_stored_gap": gap,
                    "exceptions_preserved": True,
                    "source_evidence": "live RO refresh + prior phase10k register",
                    "recommended_action": "FINANCE_REVIEW_NO_AUTO_CORRECT_NO_ACCEPTABILITY_DECISION",
                    "approver": "Account In Charge / Main Administrator",
                }
                if mysql_probe and isinstance(mysql_probe, dict):
                    mysql_probe = {
                        **mysql_probe,
                        "live_financial_audit": audit,
                        "row_counts": probe.get("row_counts"),
                        "wallet_transaction_id_range": txn_range,
                    }
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"MySQL enrichment skipped: {exc}"))

        equipment_inventory = None
        try:
            equipment_inventory = fetch_legacy_equipment_inventory()
            # Strip full rows from readiness embedding size; keep count + sample
            if equipment_inventory and equipment_inventory.get("ok"):
                rows = equipment_inventory.get("legacy_equipment") or []
                equipment_inventory = {
                    **{k: v for k, v in equipment_inventory.items() if k != "legacy_equipment"},
                    "count": len(rows),
                    "sample_ids": [r.get("legacy_id") for r in rows[:20]],
                    "eligible_window_set": "UNKNOWN — discovery blocked",
                    "explicit_mappings": 0,
                    "auto_mapped": False,
                }
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"Equipment inventory skipped: {exc}"))

        ta = test_account_cleanup_dry_run()
        ta["environment"] = "staging_or_local"
        email_dry_run = {
            "environment": "staging_or_local",
            "smtp_sends": 0,
            "note": "Staging dry-run only; production dry-run OPERATOR REQUIRED",
        }
        try:
            from iic_booking.users.legacy_ledger.migration_notifications import (
                create_notification_batch,
            )

            _batch, report_email = create_notification_batch(dry_run=True)
            email_dry_run.update(
                {
                    "smtp_sends": 0,
                    "dry_run": True,
                    "total_recipients": report_email.get("total_recipients"),
                    "by_template": report_email.get("by_template"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            email_dry_run["error"] = str(exc)

        release_plan = build_release_candidate_prep(
            production_sha="6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b",
            backend_sha="84aa6e5+uncommitted",
            frontend_sha="de71188+uncommitted",
        )

        staging_schema_status = {
            "source": "iic-booking-staging-django showmigrations / migrate --plan (Phase 10L)",
            "users_0101_applied": True,
            "users_0102_applied": True,
            "users_0103_applied": True,
            "users_0104_applied": True,
            "migrate_plan": "No planned migration operations",
            "note": "Staging already has 0101–0104; production still pending per Phase 10H audit",
            "production_pending": ["0101", "0102", "0103", "0104"],
            "migrate_executed_this_phase": False,
        }

        backup_report = {
            "backup_verified": bool(options.get("backup_verified")),
            "status": "PASS" if options.get("backup_verified") else "BLOCKED",
            "missing_permission": "rds:DescribeDBInstances / rds:DescribeDBSnapshots (AccessDenied)",
            "do_not_change_iam_automatically": True,
            "iam_probe": {
                "principal": "arn:aws:iam::267366138117:user/iic-booking-S3-user",
                "region_tried": "ap-south-1",
                "describe_db_snapshots": "AccessDenied",
                "describe_db_instances": "AccessDenied",
                "iam_auto_change": False,
            },
            "aws_console_procedure": [
                "Sign in to AWS Console with authorized operator account",
                "Navigate to RDS → Databases",
                "Select the IIC booking production DB instance",
                "Note DB identifier, status, Multi-AZ, backup retention period",
                "Open Maintenance & backups / Snapshots tab",
                "Record latest automated backup timestamp and status",
                "Record any recent manual snapshot name/timestamp/status",
                "Confirm restore availability",
                "Re-run migration_phase10l_readiness_closure --backup-verified after confirmation",
            ],
            "t0_refuses_without_backup": True,
            "create_or_delete_backups": False,
        }

        raa_regression = {
            "status": "BLOCKED",
            "reason": (
                "Production missing users.0102 (migration_start_at). "
                "RAA HTTP 500 linked to this gap — do not patch around; do not ALTER DB manually."
            ),
            "regression_executed": False,
            "prerequisite_sequence": [
                "release authorized + deployed",
                "backup verified",
                "explicit schema authorization",
                "migrate users 0101–0104 (Django only)",
                "then RAA booking regression",
            ],
            "separate_from_t0": True,
        }

        discovery_result = None
        if gates["discovery_allowed"]:
            self.stdout.write(
                self.style.WARNING(
                    "Datetime APPROVED + window configured — RO discovery eligible. "
                    "Not auto-running; invoke migration_production_legacy_qualification."
                )
            )

        report = build_phase10l_final_readiness(
            backup_verified=bool(options.get("backup_verified")),
            mysql_probe=mysql_probe,
            datetime_validation=datetime_validation,
            datetime_review=datetime_review,
            wallet_reconciliation=wallet_reconciliation,
            production_migrate_plan={
                "source": "production Phase 10H audit + staging Phase 10L plan-only",
                "applied": ["0096", "0097", "0098", "0099", "0100"],
                "pending_on_production_image": ["0101", "0102", "0103"],
                "pending_after_phase10d_deploy": ["0104"],
                "migrate_executed": False,
                "schema_migrate_authorized": bool(options.get("schema_migrate_authorized")),
                "users_0102_provides_migration_start_at": True,
                "exact_commands": {
                    "showmigrations": (
                        "docker exec iic-booking-backend-django-1 "
                        "python manage.py showmigrations users"
                    ),
                    "plan": (
                        "docker exec iic-booking-backend-django-1 python manage.py migrate --plan"
                    ),
                    "migrate": "DO NOT RUN without separate explicit schema authorization",
                },
            },
            test_account_dry_run=ta,
            email_dry_run=email_dry_run,
            release_plan=release_plan,
            explicit_evidence={
                "legacy_equipment_ids": (equipment_inventory or {}).get("count") or 48,
                "explicit_mappings": 0,
                "security_tests_pass": False,
            },
            finance_reviewed=bool(options.get("finance_reviewed")),
            schema_migrate_authorized=bool(options.get("schema_migrate_authorized")),
            equipment_mapping_authorized=bool(options.get("equipment_mapping_authorized")),
            discovery_result=discovery_result,
            staging_schema_status=staging_schema_status,
            raa_regression=raa_regression,
            equipment_inventory=equipment_inventory,
            backup_report=backup_report,
            security_tests={
                "ok": False,
                "note": "Filled after regression suite — Main Admin only for control endpoints",
            },
            regression_tests={
                "pending_fill": True,
                "note": "Filled by Phase 10L test runner after suite completes",
            },
        )

        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        if options.get("default_artifact"):
            paths = write_phase10l_artifacts(
                report,
                datetime_validation=datetime_validation,
                datetime_review=datetime_review,
                wallet_reconciliation=wallet_reconciliation,
                finance_register=finance_register,
                equipment_inventory=equipment_inventory,
                backup_report=backup_report,
                release_plan=release_plan,
            )
            for p in paths:
                self.stdout.write(self.style.SUCCESS(f"Wrote {p}"))

        out = (options.get("json_out") or "").strip()
        if out:
            Path(out).write_text(payload + "\n", encoding="utf-8")

        if report["verdict"] == VERDICT_READY:
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))

        self.stdout.write("--- stage machine ---")
        for s in report.get("stage_machine") or []:
            self.stdout.write(f"  {s.get('stage')}: {s.get('status')}")

        for k, v in (report.get("production_safety") or {}).items():
            self.stdout.write(f"  {k}: {v}")
