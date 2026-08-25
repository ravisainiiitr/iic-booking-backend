"""Phase 10M — operator gate clearance + migration discovery (READ-ONLY). No T0."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10l_readiness_closure import build_release_candidate_prep
from iic_booking.users.legacy_ledger.phase10m_readiness_closure import (
    VERDICT_OPERATOR_GATES,
    build_phase10m_final_readiness,
    maybe_run_production_discovery,
    write_phase10m_artifacts,
)


class Command(BaseCommand):
    help = (
        "Phase 10M operator gate clearance checkpoint. "
        "Inspects LIVE clearance; auto-runs RO discovery ONLY when datetime APPROVED AND window configured. "
        "Never POST datetime, invent dates, migrate schema, or execute T0."
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
        datetime_review["phase"] = "10M"
        datetime_review["approval_endpoint_called"] = False

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
                "Phase 10M CLEARANCE — "
                f"datetime={gates['datetime_contract_approval']['status']} "
                f"window={gates['migration_window']['configured']} "
                f"discovery_allowed={gates['discovery_allowed']}"
            )
        )

        # Auto-run discovery AS SOON AS both gates clear
        auto_discovery = maybe_run_production_discovery(
            discovery_allowed=bool(gates["discovery_allowed"])
        )
        if gates["discovery_allowed"]:
            self.stdout.write(self.style.SUCCESS(f"Auto discovery: {auto_discovery.get('status')}"))
        else:
            self.stdout.write(
                "Discovery NOT run — Main Admin must approve datetime AND configure window first. "
                "POST datetime-contract: NOT called. Dates: NOT invented."
            )

        wallet_reconciliation = None
        finance_register = None
        try:
            from iic_booking.users.legacy_ledger.reader import OldMySQLReader

            with OldMySQLReader() as reader:
                null_rows = reader.fetchall(
                    "SELECT id, user_id, equipment_id, booking_date, time_required, "
                    "status, is_deleted, is_active, charge FROM booking "
                    "WHERE booking_date IS NULL ORDER BY id"
                )
                zero_rows = reader.fetchall(
                    "SELECT id, user_id, equipment_id, booking_date, time_required, "
                    "status, is_deleted, is_active, charge FROM booking "
                    "WHERE time_required = 0 ORDER BY id"
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
                datetime_review["phase"] = "10M"
                datetime_review["approval_endpoint_called"] = False

                audit = reader.live_financial_audit()
                probe = reader.connection_probe()
                orphans = reader.fetchone(
                    "SELECT COUNT(*) AS c FROM user_wallet w "
                    "LEFT JOIN users u ON u.id = w.user_id WHERE u.id IS NULL"
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
                    "phase": "10M",
                    "acceptability_decided": False,
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
                    },
                    "auto_correction": False,
                }
                finance_register = {
                    "phase": "10M",
                    "auto_correction": False,
                    "writes": 0,
                    "acceptability_decided": False,
                    "mismatch_count": wallet_reconciliation["mismatch_count"],
                    "orphan_wallets": wallet_reconciliation["orphan_wallets"],
                    "ledger_vs_stored_gap": gap,
                    "exceptions_preserved": True,
                    "recommended_action": "FINANCE_REVIEW_NO_AUTO_CORRECT",
                }
                if mysql_probe:
                    mysql_probe = {
                        **mysql_probe,
                        "live_financial_audit": audit,
                        "row_counts": probe.get("row_counts"),
                        "wallet_transaction_id_range": txn_range,
                        "ok": True,
                    }
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"MySQL enrichment skipped: {exc}"))

        equipment_inventory = None
        try:
            inv = fetch_legacy_equipment_inventory()
            if inv and inv.get("ok"):
                rows = inv.get("legacy_equipment") or []
                equipment_inventory = {
                    "ok": True,
                    "count": len(rows),
                    "sample_ids": [r.get("legacy_id") for r in rows[:20]],
                    "eligible_window_set": "UNKNOWN" if not gates["discovery_allowed"] else "SEE_DISCOVERY",
                    "explicit_mappings": 0,
                    "auto_mapped": False,
                }
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"Equipment inventory skipped: {exc}"))

        ta = test_account_cleanup_dry_run()
        ta["environment"] = "staging_or_local"
        email_dry_run = {"environment": "staging_or_local", "smtp_sends": 0, "dry_run": True}
        try:
            from iic_booking.users.legacy_ledger.migration_notifications import (
                create_notification_batch,
            )

            _b, er = create_notification_batch(dry_run=True)
            email_dry_run.update(
                {
                    "smtp_sends": 0,
                    "total_recipients": er.get("total_recipients"),
                    "by_template": er.get("by_template"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            email_dry_run["error"] = str(exc)

        release_plan = build_release_candidate_prep(
            production_sha="6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b",
            backend_sha="84aa6e5+uncommitted",
            frontend_sha="de71188+uncommitted",
        )
        release_plan["phase"] = "10M"
        release_plan["uncommitted_phases"] = list(release_plan.get("uncommitted_phases") or []) + ["10M"]

        backup_report = {
            "backup_verified": bool(options.get("backup_verified")),
            "status": "PASS" if options.get("backup_verified") else "BLOCKED",
            "iam_probe": {
                "principal": "arn:aws:iam::267366138117:user/iic-booking-S3-user",
                "region_tried": "ap-south-1",
                "result": "AccessDenied",
            },
            "do_not_change_iam_automatically": True,
            "aws_console_procedure": [
                "AWS Console → RDS → Databases → production instance → Snapshots",
                "Record latest automated/manual snapshot",
                "Re-run with --backup-verified after visual confirmation",
            ],
        }

        staging_schema_status = {
            "users_0101_applied": True,
            "users_0102_applied": True,
            "users_0103_applied": True,
            "users_0104_applied": True,
            "migrate_plan": "No planned migration operations",
            "production_pending": ["0101", "0102", "0103", "0104"],
            "migrate_executed_this_phase": False,
        }

        report = build_phase10m_final_readiness(
            backup_verified=bool(options.get("backup_verified")),
            mysql_probe=mysql_probe,
            datetime_validation=datetime_validation,
            datetime_review=datetime_review,
            wallet_reconciliation=wallet_reconciliation,
            production_migrate_plan={
                "source": "Phase 10H prod audit + staging Phase 10M plan-only",
                "applied": ["0096", "0097", "0098", "0099", "0100"],
                "pending_on_production_image": ["0101", "0102", "0103"],
                "pending_after_phase10d_deploy": ["0104"],
                "migrate_executed": False,
                "schema_migrate_authorized": bool(options.get("schema_migrate_authorized")),
                "users_0102_provides_migration_start_at": True,
            },
            test_account_dry_run=ta,
            email_dry_run=email_dry_run,
            release_plan=release_plan,
            explicit_evidence={
                "explicit_mappings": 0,
                "legacy_equipment_ids": (equipment_inventory or {}).get("count") or 48,
            },
            finance_reviewed=bool(options.get("finance_reviewed")),
            schema_migrate_authorized=bool(options.get("schema_migrate_authorized")),
            equipment_mapping_authorized=bool(options.get("equipment_mapping_authorized")),
            discovery_result=None,
            staging_schema_status=staging_schema_status,
            raa_regression={
                "status": "BLOCKED",
                "regression_executed": False,
                "reason": "Production users.0102 not applied — no RAA workaround",
            },
            equipment_inventory=equipment_inventory,
            backup_report=backup_report,
            auto_discovery_result=auto_discovery,
            regression_tests={"pending_fill": True},
        )

        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        if options.get("default_artifact"):
            for p in write_phase10m_artifacts(
                report,
                datetime_validation=datetime_validation,
                datetime_review=datetime_review,
                wallet_reconciliation=wallet_reconciliation,
                finance_register=finance_register,
            ):
                self.stdout.write(self.style.SUCCESS(f"Wrote {p}"))

        out = (options.get("json_out") or "").strip()
        if out:
            Path(out).write_text(payload + "\n", encoding="utf-8")

        if report["verdict"] == VERDICT_READY:
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))

        self.stdout.write("--- CLEARED ---")
        for g in report.get("gates_cleared") or []:
            self.stdout.write(f"  PASS {g}")
        self.stdout.write("--- NOT CLEARED ---")
        for g in report.get("gates_not_cleared") or []:
            self.stdout.write(f"  FAIL {g}")
        self.stdout.write("--- REMAINING ACTIONS ---")
        for a in report.get("remaining_operator_actions") or []:
            self.stdout.write(f"  [{a.get('priority')}] {a.get('gate')}: {a.get('action')}")

        for k, v in (report.get("production_safety") or {}).items():
            self.stdout.write(f"  {k}: {v}")

        if report["verdict"] == VERDICT_OPERATOR_GATES:
            self.stdout.write(
                self.style.WARNING(
                    "Operator gates (datetime/window) still block discovery — "
                    "independent RO/prep refreshed anyway."
                )
            )
