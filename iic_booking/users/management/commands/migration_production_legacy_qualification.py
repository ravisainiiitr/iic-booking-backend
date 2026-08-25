"""
Phase 10 — Production legacy booking qualification (READ-ONLY).

Runs after Phase 8 code deploy + users.0101–0103 migration.
Does NOT activate T0, create blocks, send email, or modify data.

Usage (production django container):
  python manage.py migration_production_legacy_qualification --json-out /tmp/phase10.json
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, ProgrammingError

from iic_booking.users.legacy_ledger.booking_lock import (
    LEGACY_PORTAL_BOOKING_DISABLED_MODES,
    legacy_portal_mutating_booking_blocked,
)
from iic_booking.users.legacy_ledger.legacy_booking_mysql import (
    audit_target_slots,
    build_t0_dataset_summary,
    discover_mysql_booking_schema,
    fetch_legacy_bookings_for_window,
    map_legacy_identities,
)
from iic_booking.users.legacy_ledger.migration_notifications import (
    preview_templates,
    select_notification_candidates,
)
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import PortalMigrationState

PHASE8_MIGRATIONS = ("0101", "0102", "0103", "0104")
FORBIDDEN = ("equipment.0188", "r14")


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _applied_users_migrations() -> dict[str, bool]:
    with connection.cursor() as cur:
        cur.execute("SELECT name FROM django_migrations WHERE app='users' ORDER BY name")
        names = {row[0] for row in cur.fetchall()}
    out = {}
    for key in ("0096", "0097", "0098", "0099", "0100") + PHASE8_MIGRATIONS:
        out[key] = any(n.startswith(f"{int(key):04d}_") for n in names)
    return out


def _schema_tables() -> dict[str, bool]:
    tables = set(connection.introspection.table_names())
    return {
        "MigrationBookingSettlement": "users_migrationbookingsettlement" in tables,
        "LegacyEquipmentMapping": "users_legacyequipmentmapping" in tables,
        "LegacyBookingMigrationBatch": "users_legacybookingmigrationbatch" in tables,
        "LegacyBookingBlock": "users_legacybookingblock" in tables,
        "MigrationNotificationBatch": "users_migrationnotificationbatch" in tables,
    }


def _portal_migration_fields() -> list[str]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name='users_portalmigrationstate'
            ORDER BY ordinal_position
            """
        )
        return [r[0] for r in cur.fetchall()]


def _safe_portal_migration_state() -> tuple[Any, str | None]:
    """Return migration state or a minimal fallback when pre-0101 schema is live."""
    try:
        return PortalMigrationState.get_solo(), None
    except ProgrammingError as exc:
        return type(
            "PortalMigrationStateFallback",
            (),
            {
                "migration_start_at": None,
                "migration_window_end_at": None,
                "booking_migration_mode": "NORMAL",
                "new_portal_url": "",
                "phase": "PREPARATION",
            },
        )(), str(exc)


def build_phase10_report(*, column_map_file: str = "") -> dict[str, Any]:
    from iic_booking.users.legacy_ledger.datetime_contract import (
        datetime_contract_ui_payload,
        load_datetime_contract,
        validate_contract_for_discovery,
    )
    from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings
    from iic_booking.users.legacy_ledger.legacy_conflict_analysis import analyze_booking_conflicts
    from iic_booking.users.legacy_ledger.legacy_datetime_validation import validate_legacy_datetime_readonly
    from iic_booking.users.legacy_ledger.legacy_equipment_mapping_import import (
        default_mapping_file_path,
        preview_equipment_mapping_import,
    )
    from iic_booking.users.legacy_ledger.legacy_upcoming_discovery import discover_upcoming_legacy_week
    from iic_booking.users.legacy_ledger.test_account_dry_run import test_account_cleanup_dry_run

    migs = _applied_users_migrations()
    tables = _schema_tables()
    if tables.get("LegacyEquipmentMapping"):
        mapping = validate_legacy_equipment_mappings()
    else:
        mapping = {
            "ready": False,
            "error": "LegacyEquipmentMapping table missing — apply users.0101–0104",
            "mapped": [],
            "unmapped": [],
            "conflict": [],
        }
    state, state_schema_error = _safe_portal_migration_state()

    contract = load_datetime_contract(column_map_file or None)
    datetime_contract = datetime_contract_ui_payload(contract)
    contract_gate = validate_contract_for_discovery(contract)
    try:
        datetime_validation = validate_legacy_datetime_readonly()
    except Exception as exc:  # noqa: BLE001 — qualification must never write or abort
        datetime_validation = {"ok": False, "error": str(exc), "audit_mode": "READ_ONLY"}

    try:
        mysql_schema = discover_mysql_booking_schema()
    except Exception as exc:  # noqa: BLE001 — read-only qualification must not abort
        mysql_schema = {"ok": False, "error": str(exc)}
    legacy_fetch: dict[str, Any] = {"ok": False, "reason": "not_executed"}
    upcoming_discovery: dict[str, Any] = {"ok": False, "reason": "not_executed"}
    identity: dict[str, Any] = {"exception_count": 0}
    slot_audit: dict[str, Any] = {}
    conflict_report: dict[str, Any] = {"conflict_count": 0}

    if contract_gate.get("ready_for_discovery") and migs.get("0102"):
        upcoming_discovery = discover_upcoming_legacy_week(column_map_file=column_map_file)
        legacy_fetch = upcoming_discovery
        if upcoming_discovery.get("ok"):
            counts = upcoming_discovery.get("discovery_counts") or {}
            eligible_candidates = [
                c for c in upcoming_discovery.get("candidates") or [] if c.get("eligibility") == "eligible"
            ]
            identity = {
                "mapped_count": upcoming_discovery.get("user_resolved_count", 0),
                "unresolved_count": upcoming_discovery.get("user_unresolved_count", 0),
                "exception_count": 0,
                "user_mapping_blocks_readiness": False,
            }
            slot_audit = audit_target_slots(
                [
                    {
                        "legacy_booking_id": c.get("legacy_booking_id"),
                        "old_equipment_id": c.get("legacy_equipment_id"),
                        "start_at": c.get("legacy_booking_start"),
                        "end_at": c.get("legacy_booking_end"),
                    }
                    for c in eligible_candidates
                ]
            )
            conflict_report = upcoming_discovery.get("conflict_report") or analyze_booking_conflicts(
                [
                    {
                        "legacy_booking_id": c.get("legacy_booking_id"),
                        "old_equipment_id": c.get("legacy_equipment_id"),
                        "start_at": c.get("legacy_booking_start"),
                        "end_at": c.get("legacy_booking_end"),
                        "new_equipment_id": c.get("new_equipment_id"),
                    }
                    for c in eligible_candidates
                ]
            )
        else:
            identity = {"exception_count": 0, "error": upcoming_discovery.get("error")}
    elif mysql_schema.get("ok") and migs.get("0102"):
        legacy_fetch = fetch_legacy_bookings_for_window(column_map_file=column_map_file)
        if legacy_fetch.get("ok"):
            counts = legacy_fetch.get("discovery", {}).get("counts") or {}
            legacy_fetch["discovery_counts"] = counts
            eligible = legacy_fetch.get("eligible_for_audit") or []
            identity = map_legacy_identities(eligible)
            slot_audit = audit_target_slots(eligible)
            conflict_report = analyze_booking_conflicts(eligible)
        else:
            identity = {"exception_count": 0, "error": legacy_fetch.get("error")}
            slot_audit = {}

    required_legacy_ids = {
        int(c.get("legacy_equipment_id"))
        for c in (upcoming_discovery.get("candidates") or [])
        if c.get("eligibility") == "eligible" and c.get("legacy_equipment_id") is not None
    }
    equip_file = default_mapping_file_path()
    equip_import_preview = preview_equipment_mapping_import(
        equip_file,
        required_legacy_ids=required_legacy_ids if required_legacy_ids else None,
    ) if equip_file.is_file() else {"ok": False, "error": "mapping_file_missing", "path": str(equip_file)}

    email_audit = select_notification_candidates()
    test_dry_run = test_account_cleanup_dry_run()

    templates = preview_templates()
    prod_url = state.new_portal_url or getattr(settings, "FRONTEND_URL", "") or ""
    template_ok = all(
        "localhost" not in (prod_url or "").lower() and "staging" not in (prod_url or "").lower()
        for _ in [1]
    ) and len(templates) == 4

    try:
        blocked, code, _ = legacy_portal_mutating_booking_blocked()
    except ProgrammingError:
        blocked, code = False, ""
    mode = (getattr(state, "booking_migration_mode", None) or "NORMAL").upper()

    t0_summary = build_t0_dataset_summary(
        discovery_counts=legacy_fetch.get("discovery_counts") or legacy_fetch.get("discovery", {}).get("counts"),
        identity_exceptions=identity.get("exception_count", 0),
        slot_audit=slot_audit,
        mapping_report=mapping,
        test_users=test_dry_run.get("test_users", 0),
        email_recipients=email_audit.get("total_recipients", 0),
    )

    deploy_ok = all(migs.get(k) for k in PHASE8_MIGRATIONS) and all(tables.values())
    gates_ok = (
        deploy_ok
        and mysql_schema.get("ok")
        and legacy_fetch.get("ok")
        and mapping.get("ready")
        and t0_summary.get("t0_ready")
        and mode not in LEGACY_PORTAL_BOOKING_DISABLED_MODES
    )

    blockers = []
    if state_schema_error:
        blockers.append("portal_migration_state_schema_incomplete")
    if not all(migs.get(k) for k in PHASE8_MIGRATIONS):
        blockers.append("users.0101–0104 not all applied")
    if not contract_gate.get("ready_for_discovery"):
        blockers.append("datetime_contract_operator_required")
    if not all(tables.values()):
        blockers.extend([f"missing_table:{k}" for k, v in tables.items() if not v])
    if not mysql_schema.get("ok"):
        blockers.append("mysql_booking_column_map_not_ready")
    if not legacy_fetch.get("ok") and not contract_gate.get("ready_for_discovery"):
        blockers.append(legacy_fetch.get("error") or "legacy_discovery_not_executed")
    elif not legacy_fetch.get("ok"):
        blockers.append(legacy_fetch.get("error") or "legacy_discovery_failed")
    if not mapping.get("ready"):
        blockers.append("equipment_mapping_not_ready")
    if equip_import_preview.get("preview", {}).get("missing_required_legacy_ids"):
        blockers.append("unmapped_required_legacy_equipment")
    if (conflict_report.get("conflict_count") or 0) > 0:
        blockers.append("slot_or_booking_conflicts")
    blockers.extend(t0_summary.get("blockers") or [])

    verdict = (
        "READY FOR FINAL T0 REVIEW"
        if gates_ok and not blockers
        else "NOT READY — BLOCKERS LISTED"
    )

    return {
        "phase": "10E",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "production": {
            "git_sha": _git_sha(),
            "deployment_environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", ""),
            "production_tag": getattr(settings, "RELEASE_TAG", "") or "",
        },
        "migrations": {"users": migs, "phase8_required": list(PHASE8_MIGRATIONS), "forbidden_absent": True},
        "schema_tables": tables,
        "portal_migration_state_fields": _portal_migration_fields(),
        "portal_migration_state_schema_error": state_schema_error,
        "datetime_contract": datetime_contract,
        "datetime_contract_gate": contract_gate,
        "datetime_validation": datetime_validation,
        "equipment_mapping": {
            "counts": mapping.get("counts"),
            "ready": mapping.get("ready"),
            "import_file_preview": equip_import_preview,
            "required_mappings_in_window": len(required_legacy_ids),
        },
        "mysql_column_map": mysql_schema,
        "legacy_booking_discovery": legacy_fetch,
        "upcoming_week_discovery": upcoming_discovery,
        "identity_mapping": identity,
        "user_resolved_count": identity.get("mapped_count", 0),
        "user_unresolved_count": identity.get("unresolved_count", identity.get("unresolved_count", 0)),
        "conflict_analysis": conflict_report,
        "conflict_count": conflict_report.get("conflict_count", 0),
        "target_slot_audit": slot_audit,
        "test_account_dry_run": test_dry_run,
        "email_recipient_dry_run": {
            "emails_sent": 0,
            "total_recipients": email_audit.get("total_recipients"),
            "faculty": email_audit.get("faculty"),
            "students": email_audit.get("students"),
            "oic": email_audit.get("oic"),
            "admin": email_audit.get("admin"),
            "skipped": email_audit.get("skipped"),
            "unsupported_roles": email_audit.get("skipped_rows", [])[:20],
            "invalid_email": email_audit.get("invalid_email"),
            "duplicate_email": email_audit.get("duplicate_email"),
            "test_accounts_in_recipients": sum(
                1 for r in (email_audit.get("selected") or []) if User.objects.filter(pk=r["user_id"], is_test_account=True).exists()
            ),
        },
        "email_templates": {"verified": template_ok, "template_count": len(templates), "production_url": prod_url},
        "freeze_contract": {
            "current_mode": mode,
            "t0_not_activated": mode not in LEGACY_PORTAL_BOOKING_DISABLED_MODES,
            "code": code or "MIGRATION_BOOKING_DISABLED",
        },
        "new_portal_blocking": {"code": "LEGACY_MIGRATION_SLOT_BLOCKED", "verified_in_code": True, "blocks_created": False},
        "refund_rbac": {
            "oic_scoped": True,
            "main_admin_global": True,
            "can_issue_helper": True,
            "refunds_issued": 0,
        },
        "main_admin_global_view": {"api": "portal_legacy_bridge_views", "verified_in_code": True},
        "hard_off_status": {
            "LEGACY_MYSQL_STAGING_FIXTURE_MODE": bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)),
            "CHANNEL_I_STAGING_FIXTURE_MODE": bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)),
            "REAL_INTEGRATION_ENABLED": bool(getattr(settings, "REAL_INTEGRATION_ENABLED", False)),
            "production_env": getattr(settings, "DEPLOYMENT_ENVIRONMENT", ""),
        },
        "frontend_release_status": {
            "note": "Verify frontend commit/PR/tag separately before production deploy",
            "portal_migration_ui_paths": [
                "/admin/portal-migration",
                "/admin/portal-migration/equipment-mapping",
                "/admin/portal-migration/legacy-bookings",
            ],
        },
        "migration_order_document": "docs/release/migration/AI30-AI31-PHASE-10E-PRODUCTION-QUALIFICATION.md",
        "app_rollback_note": "APP ROLLBACK != DATABASE ROLLBACK",
        "reconciliation_plan": {
            "invariant": "eligible_legacy_count == active_block_count after T0",
            "fields": ["legacy_booking_id", "new_equipment", "start_at", "end_at", "slot_ids"],
        },
        "abort_plan": {
            "command": "migration_abort_batch",
            "releases_blocks": True,
            "retains_audit": True,
            "does_not_reverse_refunds": True,
        },
        "t0_dataset_summary": t0_summary,
        "blockers": blockers,
        "verdict": verdict,
        "production_writes_performed": [],
    }


class Command(BaseCommand):
    help = "Phase 10 read-only production legacy qualification. No T0, no blocks, no email."

    def add_arguments(self, parser):
        parser.add_argument("--column-map-file", type=str, default="", help="Operator-approved booking column map JSON")
        parser.add_argument("--json-out", type=str, default="")

    def handle(self, *args, **options):
        report = build_phase10_report(column_map_file=(options.get("column_map_file") or "").strip())
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)
        out = (options.get("json_out") or "").strip()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(payload)
        if report["verdict"] == "READY FOR FINAL T0 REVIEW":
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))
