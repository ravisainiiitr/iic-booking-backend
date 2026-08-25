"""Phase 10K — operator gate execution + real migration discovery (READ-ONLY). No T0."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10k_readiness_closure import (
    VERDICT_NOT_READY_OPERATOR_GATES,
    build_phase10k_final_readiness,
    write_phase10k_artifacts,
)


class Command(BaseCommand):
    help = (
        "Phase 10K read-only operator gate execution + discovery eligibility. "
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
        datetime_review["phase"] = "10K"

        gates = inspect_operator_gates(
            backup_verified=bool(options.get("backup_verified")),
            release_reviewed=False,
            schema_migrate_authorized=bool(options.get("schema_migrate_authorized")),
            equipment_mapping_authorized=bool(options.get("equipment_mapping_authorized")),
            finance_reviewed=bool(options.get("finance_reviewed")),
            datetime_validation=datetime_validation,
            explicit_mappings=0,
        )

        if not gates["discovery_allowed"]:
            self.stdout.write(
                self.style.WARNING(
                    "OPERATOR_REQUIRED — discovery BLOCKED "
                    f"(datetime={gates['datetime_contract_approval']['status']}; "
                    f"window_configured={gates['migration_window']['configured']})"
                )
            )
            self.stdout.write(
                "Exact datetime approval: UI /admin/portal-migration or "
                "POST /api/portal-migration/admin/datetime-contract/ "
                '{"confirm": true, "approval_reason": "..."}'
            )
            self.stdout.write(
                "Exact window config: UI Phase 8B settings or "
                "PATCH /api/portal-migration/admin/state/ "
                "(migration_start_at + migration_window_end_at)"
            )
            self.stdout.write("POST datetime-contract: NOT called (auto-approve forbidden)")

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
                datetime_review["phase"] = "10K"
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
                    "phase": "10K",
                    "compared_to_phase10j": {
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
                    "suspicious_transactions": {
                        "outlier_abs_gt_10m": audit.get("outlier_abs_gt_10m"),
                    },
                    "auto_correction": False,
                    "note": "Finance review only — do not correct balances or create opening balances.",
                }
                finance_register = {
                    "phase": "10K",
                    "auto_correction": False,
                    "writes": 0,
                    "mismatch_count": wallet_reconciliation["mismatch_count"],
                    "orphan_wallets": wallet_reconciliation["orphan_wallets"],
                    "ledger_vs_stored_gap": gap,
                    "source_evidence": "phase10j_finance_exception_register.json + live RO refresh",
                    "recommended_action": "FINANCE_REVIEW_NO_AUTO_CORRECT",
                    "approver": "Account In Charge / Main Administrator",
                    "exceptions_preserved": True,
                }
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"MySQL enrichment skipped: {exc}"))

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

        release_plan = {
            "production_sha": "6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b",
            "local_backend_sha": "84aa6e5+uncommitted",
            "local_frontend_sha": "de71188+uncommitted",
            "uncommitted_phases": ["10D", "10E", "10F", "10G", "10H", "10I", "10J", "10K"],
            "reviewed_released": False,
            "push_executed": False,
            "deploy_executed": False,
            "pr_number": None,
            "release_tag": None,
            "note": "OPERATOR ACTION REQUIRED to push/PR — do not fabricate PR numbers",
            "deployment_order": [
                "commit/PR/tag backend (incl. 0104)",
                "commit/PR/tag frontend migration UI",
                "deploy backend (no auto-migrate)",
                "deploy frontend",
                "showmigrations + migrate --plan",
                "explicit MIGRATE 0101–0104 (separate auth — provides migration_start_at via 0102)",
                "RAA booking regression after schema",
            ],
            "rollback": "APP ROLLBACK != DATABASE ROLLBACK — see Phase 10G rollback doc",
        }

        staging_schema_status = {
            "source": "iic-booking-staging-django showmigrations / migrate --plan (Phase 10K)",
            "users_0101_applied": True,
            "users_0102_applied": True,
            "users_0103_applied": True,
            "users_0104_applied": True,
            "migrate_plan": "No planned migration operations",
            "note": "Staging already has 0101–0104; production still pending per Phase 10H audit",
            "production_pending": ["0101", "0102", "0103", "0104"],
            "migrate_executed_this_phase": False,
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

        # Absolute: discovery only if gates allow — and we still do not invent a run
        discovery_result = None
        if gates["discovery_allowed"]:
            self.stdout.write(
                self.style.WARNING(
                    "Datetime APPROVED + window configured — RO discovery eligible. "
                    "Not auto-running in this closure; invoke migration_production_legacy_qualification."
                )
            )
        else:
            discovery_result = None  # build_phase10k emits blocked artifact

        report = build_phase10k_final_readiness(
            backup_verified=bool(options.get("backup_verified")),
            mysql_probe=mysql_probe,
            datetime_validation=datetime_validation,
            datetime_review=datetime_review,
            wallet_reconciliation=wallet_reconciliation,
            production_migrate_plan={
                "source": "production docker exec migrate --plan (Phase 10H 2026-08-25) + staging Phase 10K plan-only",
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
                "legacy_equipment_ids": 45,
                "explicit_mappings": 0,
                "security_tests_pass": False,
            },
            finance_reviewed=bool(options.get("finance_reviewed")),
            schema_migrate_authorized=bool(options.get("schema_migrate_authorized")),
            equipment_mapping_authorized=bool(options.get("equipment_mapping_authorized")),
            discovery_result=discovery_result,
            staging_schema_status=staging_schema_status,
            raa_regression=raa_regression,
        )

        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        if options.get("default_artifact"):
            paths = write_phase10k_artifacts(
                report,
                datetime_validation=datetime_validation,
                datetime_review=datetime_review,
                wallet_reconciliation=wallet_reconciliation,
                finance_register=finance_register,
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
            if report["verdict"] != VERDICT_NOT_READY_OPERATOR_GATES:
                self.stdout.write(
                    self.style.WARNING(
                        f"Expected {VERDICT_NOT_READY_OPERATOR_GATES} when gates incomplete"
                    )
                )

        for k, v in (report.get("production_safety") or {}).items():
            self.stdout.write(f"  {k}: {v}")
