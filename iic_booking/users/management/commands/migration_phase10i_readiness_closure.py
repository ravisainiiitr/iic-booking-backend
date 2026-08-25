"""Phase 10I — final production qualification report (READ-ONLY). No T0."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10i_readiness_closure import (
    build_datetime_review,
    build_phase10i_final_readiness,
    write_phase10i_artifacts,
)


class Command(BaseCommand):
    help = (
        "Phase 10I read-only final production qualification. "
        "Does NOT migrate, activate T0, freeze, email, refund, cleanup, or approve datetime."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument("--default-artifact", action="store_true")
        parser.add_argument("--backup-verified", action="store_true")

    def handle(self, *args, **options):
        from iic_booking.users.legacy_ledger.legacy_datetime_validation import (
            validate_legacy_datetime_readonly,
        )
        from iic_booking.users.legacy_ledger.phase10h_readiness_closure import _probe_mysql
        from iic_booking.users.legacy_ledger.test_account_dry_run import test_account_cleanup_dry_run

        datetime_validation = validate_legacy_datetime_readonly()
        mysql_probe = _probe_mysql()
        datetime_review = build_datetime_review(datetime_validation=datetime_validation)

        # Enrich datetime review with live null/zero rows when MySQL reachable
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
                for row in null_rows:
                    if hasattr(row.get("booking_date"), "isoformat"):
                        row["booking_date"] = row["booking_date"].isoformat()
                    if row.get("charge") is not None:
                        row["charge"] = str(row["charge"])
                for row in zero_rows:
                    if hasattr(row.get("booking_date"), "isoformat"):
                        row["booking_date"] = row["booking_date"].isoformat()
                    if row.get("charge") is not None:
                        row["charge"] = str(row["charge"])

                datetime_review = build_datetime_review(
                    datetime_validation=datetime_validation,
                    null_rows=null_rows,
                    zero_rows=zero_rows,
                )

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
                ledger = audit.get("calculated_closing_balance")
                stored = audit.get("sum_user_wallet_balance_column")
                gap = None
                try:
                    gap = float(ledger) - float(stored)
                except (TypeError, ValueError):
                    gap = None
                txn_range = probe.get("wallet_transaction_id_range") or {}
                wallet_reconciliation = {
                    "ok": True,
                    "audit_mode": "READ_ONLY",
                    "writes": 0,
                    "compared_to_phase10h": {
                        "users": 4336,
                        "wallets": 1748,
                        "transactions": 64319,
                        "max_transaction_id": 64325,
                        "ledger": "6170488.2800015",
                        "stored": "6135703.279999999",
                    },
                    "wallet_count": audit.get("wallet_count"),
                    "transaction_count": audit.get("transaction_count"),
                    "max_transaction_id": txn_range.get("max_id"),
                    "watermark": txn_range.get("max_id"),
                    "credits": audit.get("total_credits"),
                    "debits": audit.get("total_debits"),
                    "credit_count": audit.get("credit_count"),
                    "debit_count": audit.get("debit_count"),
                    "ledger_balance": ledger,
                    "stored_balance": stored,
                    "ledger_vs_stored_gap": gap,
                    "mismatch_count": int((mism_count or {}).get("c") or 0),
                    "orphan_wallets": int((orphans or {}).get("c") or 0),
                    "suspicious_transactions": {
                        "outlier_abs_gt_10m": audit.get("outlier_abs_gt_10m"),
                        "type1_negative_count": audit.get("type1_negative_count"),
                        "type2_negative_count": audit.get("type2_negative_count"),
                        "type2_zero_count": audit.get("type2_zero_count"),
                    },
                    "auto_correction": False,
                    "note": "Any change vs Phase 10H must be explained; do not correct balances.",
                }

                mism_rows = reader.fetchall(
                    """
                    SELECT w.id AS wallet_id, w.user_id, u.emp_id,
                           w.balance AS stored_balance,
                      (SELECT COALESCE(SUM(
                        CASE WHEN transaction_type=1 THEN ABS(amount)
                             WHEN transaction_type=2 THEN -ABS(amount)
                             ELSE 0 END), 0)
                       FROM wallet_transactions t WHERE t.user_id = w.user_id) AS ledger_balance
                    FROM user_wallet w
                    LEFT JOIN users u ON u.id = w.user_id
                    HAVING ABS(stored_balance - ledger_balance) > 0.01
                    ORDER BY ABS(stored_balance - ledger_balance) DESC
                    LIMIT 50
                    """
                )
                exceptions = []
                for m in mism_rows:
                    sb = float(m.get("stored_balance") or 0)
                    lb = float(m.get("ledger_balance") or 0)
                    tx = reader.fetchall(
                        "SELECT id FROM wallet_transactions WHERE user_id=%s ORDER BY id DESC LIMIT 8",
                        (m["user_id"],),
                    )
                    exceptions.append(
                        {
                            "user": m.get("user_id"),
                            "employee_id": m.get("emp_id") or None,
                            "wallet": m.get("wallet_id"),
                            "transaction_ids": [t["id"] for t in tx],
                            "amount": None,
                            "stored_balance": str(m.get("stored_balance")),
                            "calculated_balance": str(m.get("ledger_balance")),
                            "difference": round(sb - lb, 4),
                            "source_evidence": "SELECT-only OldMySQLReader wallet vs wallet_transactions",
                            "recommended_action": "FINANCE_REVIEW_NO_AUTO_CORRECT",
                            "approver": "Account In Charge / Main Administrator",
                            "approval_required": True,
                        }
                    )
                finance_register = {
                    "auto_correction": False,
                    "writes": 0,
                    "mismatch_count": wallet_reconciliation["mismatch_count"],
                    "orphan_wallets": wallet_reconciliation["orphan_wallets"],
                    "ledger_vs_stored_gap": gap,
                    "exceptions": exceptions,
                    "suspicious_untouched": True,
                    "note": "Do not infer financial corrections or reverse transactions.",
                }
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"MySQL enrichment skipped: {exc}"))

        ta = test_account_cleanup_dry_run()
        ta["environment"] = "staging_or_local"
        email_dry_run = {
            "environment": "staging_or_local",
            "smtp_sends": 0,
            "note": "Staging dry-run only in this command host; production dry-run OPERATOR REQUIRED",
        }
        try:
            from iic_booking.users.legacy_ledger.migration_notifications import (
                create_notification_batch,
            )

            _batch, report = create_notification_batch(dry_run=True)
            email_dry_run.update(
                {
                    "smtp_sends": 0,
                    "dry_run": True,
                    "total_recipients": report.get("total_recipients"),
                    "by_template": report.get("by_template"),
                    "faculty": report.get("faculty"),
                    "students": report.get("students"),
                    "oic": report.get("oic"),
                    "admin": report.get("admin"),
                    "duplicate_email": report.get("duplicate_email"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            email_dry_run["error"] = str(exc)

        release_plan = {
            "production_sha": "6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b",
            "local_backend_sha": "84aa6e5+uncommitted",
            "local_frontend_sha": "de71188+uncommitted",
            "uncommitted_phases": ["10D", "10E", "10F", "10G", "10H", "10I"],
            "reviewed_released": False,
            "push_executed": False,
            "deploy_executed": False,
            "pr_number": None,
            "release_tag": None,
            "note": "OPERATOR ACTION REQUIRED to push/PR — do not fabricate PR numbers",
            "required_migration_release": [
                "users/0101–0104",
                "legacy_ledger datetime/equipment/booking bridge",
                "portal migration admin APIs",
                "AdminPortalMigration + LegacyEquipment/Booking mapping UI",
            ],
            "separate_from_rc_if_possible": [
                "RAA / Copilot unrelated changes",
                "R14 / analysis UI if not required for migration",
            ],
            "deployment_order": [
                "commit/PR/tag backend (incl. 0104)",
                "commit/PR/tag frontend migration UI",
                "deploy backend (no auto-migrate)",
                "deploy frontend",
                "showmigrations + migrate --plan",
                "explicit MIGRATE 0101–0104 (separate auth)",
            ],
            "rollback": "APP ROLLBACK != DATABASE ROLLBACK — see Phase 10G rollback doc",
        }

        backup_procedure = {
            "backup_verified": bool(options.get("backup_verified")),
            "status": "PASS" if options.get("backup_verified") else "BLOCKED",
            "missing_permission": "rds:DescribeDBInstances / rds:DescribeDBSnapshots (AccessDenied on EC2 role)",
            "do_not_change_iam_automatically": True,
            "aws_console_procedure": [
                "Sign in to AWS Console with authorized operator account",
                "Navigate to RDS → Databases",
                "Select the IIC booking production DB instance",
                "Note DB identifier, status, Multi-AZ, backup retention period",
                "Open Maintenance & backups / Snapshots tab",
                "Record latest automated backup timestamp and status",
                "Record any recent manual snapshot name/timestamp/status",
                "Confirm restore availability (restore snapshot action visible / documented)",
                "Update readiness with --backup-verified only after visual confirmation",
            ],
            "t0_refuses_without_backup": True,
            "create_or_delete_backups": False,
        }

        report = build_phase10i_final_readiness(
            backup_verified=bool(options.get("backup_verified")),
            mysql_probe=mysql_probe,
            datetime_validation=datetime_validation,
            datetime_review=datetime_review,
            wallet_reconciliation=wallet_reconciliation,
            production_migrate_plan={
                "source": "production docker exec migrate --plan (Phase 10H 2026-08-25)",
                "applied": ["0096", "0097", "0098", "0099", "0100"],
                "pending_on_production_image": ["0101", "0102", "0103"],
                "pending_after_phase10d_deploy": ["0104"],
                "migrate_executed": False,
                "forbidden_absent": True,
                "exact_commands": {
                    "showmigrations": "docker exec iic-booking-backend-django-1 python manage.py showmigrations users",
                    "plan": "docker exec iic-booking-backend-django-1 python manage.py migrate --plan",
                    "migrate": "DO NOT RUN without separate explicit schema authorization",
                },
                "risk": "0101–0104 add migration tables/columns; app rollback does not undo schema",
            },
            test_account_dry_run=ta,
            email_dry_run=email_dry_run,
            release_plan=release_plan,
            explicit_evidence={
                "legacy_equipment_ids": 45,
                "explicit_mappings": 0,
                "security_tests_pass": False,
            },
        )

        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        if options.get("default_artifact"):
            paths = write_phase10i_artifacts(
                report,
                datetime_validation=datetime_validation,
                datetime_review=datetime_review,
                wallet_reconciliation=wallet_reconciliation,
                finance_register=finance_register,
                release_plan=release_plan,
                backup_procedure=backup_procedure,
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

        for k, v in (report.get("production_safety") or {}).items():
            self.stdout.write(f"  {k}: {v}")
