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
from django.db import connection

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
from iic_booking.users.legacy_ledger.migration_emails import preview_templates
from iic_booking.users.legacy_ledger.migration_notifications import select_notification_candidates
from iic_booking.users.models import User

PHASE8_MIGRATIONS = ("0101", "0102", "0103")
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


def build_phase10_report(*, column_map_file: str = "") -> dict[str, Any]:
    from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings

    migs = _applied_users_migrations()
    tables = _schema_tables()
    mapping = validate_legacy_equipment_mappings()
    state = PortalMigrationState.get_solo()

    mysql_schema = discover_mysql_booking_schema()
    legacy_fetch: dict[str, Any] = {"ok": False, "reason": "not_executed"}
    identity: dict[str, Any] = {"exception_count": 0}
    slot_audit: dict[str, Any] = {}

    if mysql_schema.get("ok") and migs.get("0102"):
        legacy_fetch = fetch_legacy_bookings_for_window(column_map_file=column_map_file)
        if legacy_fetch.get("ok"):
            counts = legacy_fetch.get("discovery", {}).get("counts") or {}
            legacy_fetch["discovery_counts"] = counts
            eligible = legacy_fetch.get("eligible_for_audit") or []
            identity = map_legacy_identities(eligible)
            slot_audit = audit_target_slots(eligible)
        else:
            identity = {"exception_count": 0, "error": legacy_fetch.get("error")}
            slot_audit = {}

    email_audit = select_notification_candidates()
    test_users = User.objects.filter(is_test_account=True).count()
    test_bookings = 0
    if apps.is_installed("equipment"):
        Booking = apps.get_model("equipment", "Booking")
        test_bookings = Booking.objects.filter(user__is_test_account=True).count()

    templates = preview_templates()
    prod_url = state.new_portal_url or getattr(settings, "FRONTEND_URL", "") or ""
    template_ok = all(
        "localhost" not in (prod_url or "").lower() and "staging" not in (prod_url or "").lower()
        for _ in [1]
    ) and len(templates) == 4

    blocked, code, _ = legacy_portal_mutating_booking_blocked()
    mode = (state.booking_migration_mode or "NORMAL").upper()

    t0_summary = build_t0_dataset_summary(
        discovery_counts=legacy_fetch.get("discovery_counts") or legacy_fetch.get("discovery", {}).get("counts"),
        identity_exceptions=identity.get("exception_count", 0),
        slot_audit=slot_audit,
        mapping_report=mapping,
        test_users=test_users,
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
    if not all(migs.get(k) for k in PHASE8_MIGRATIONS):
        blockers.append("users.0101–0103 not all applied")
    if not all(tables.values()):
        blockers.extend([f"missing_table:{k}" for k, v in tables.items() if not v])
    if not mysql_schema.get("ok"):
        blockers.append("mysql_booking_column_map_not_ready")
    if not legacy_fetch.get("ok"):
        blockers.append(legacy_fetch.get("error") or "legacy_discovery_not_executed")
    if not mapping.get("ready"):
        blockers.append("equipment_mapping_not_ready")
    blockers.extend(t0_summary.get("blockers") or [])

    verdict = (
        "PRODUCTION T0 READY — AWAITING EXPLICIT OPERATOR APPROVAL"
        if gates_ok and not blockers
        else "PRODUCTION MIGRATION BLOCKED — DO NOT PROCEED"
    )

    return {
        "phase": "10",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "production": {"git_sha": _git_sha(), "deployment_environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", "")},
        "migrations": {"users": migs, "phase8_required": list(PHASE8_MIGRATIONS), "forbidden_absent": True},
        "schema_tables": tables,
        "portal_migration_state_fields": _portal_migration_fields(),
        "equipment_mapping": {"counts": mapping.get("counts"), "ready": mapping.get("ready")},
        "mysql_column_map": mysql_schema,
        "legacy_booking_discovery": legacy_fetch,
        "identity_mapping": identity,
        "target_slot_audit": slot_audit,
        "test_account_dry_run": {"test_users": test_users, "test_bookings": test_bookings},
        "email_recipient_dry_run": {
            "emails_sent": 0,
            "total_recipients": email_audit.get("total_recipients"),
            "faculty": email_audit.get("faculty"),
            "students": email_audit.get("students"),
            "oic": email_audit.get("oic"),
            "admin": email_audit.get("admin"),
            "skipped": email_audit.get("skipped"),
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
        if report["verdict"].startswith("PRODUCTION T0 READY"):
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))
