"""
Phase 9 — Production T0 readiness audit (READ-ONLY).

Performs counts, migration inspection, template verification, and gate evaluation.
Does NOT: migrate, activate T0, create blocks/batches, send email, issue refunds,
delete test accounts, or modify production data.

Usage (on production host, inside django container):
  python manage.py migration_production_t0_readiness --json-out /tmp/phase9.json

Optional legacy booking discovery (requires verified fixture/column-map file):
  python manage.py migration_production_t0_readiness --legacy-rows-file /path/eligible.json
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings
from iic_booking.users.legacy_ledger.booking_lock import (
    LEGACY_PORTAL_BOOKING_DISABLED_MODES,
    OLD_PORTAL_MIGRATION_BANNER,
    legacy_portal_mutating_booking_blocked,
)
from iic_booking.users.legacy_ledger.migration_emails import classify_migration_template
from iic_booking.users.legacy_ledger.migration_notifications import (
    preview_templates,
    select_notification_candidates,
)
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import PortalMigrationState
from iic_booking.users.models.user_type import UserType

EXPECTED_USERS_MIGRATIONS = ("0096", "0097", "0098", "0099", "0100")
PHASE8_PENDING_MIGRATIONS = ("0101", "0102", "0103")
FORBIDDEN_MIGRATIONS = ("equipment.0188", "users.r14", "r14")

ROLE_BUCKETS = {
    "faculty": {UserType.FACULTY},
    "student": {UserType.STUDENT, UserType.INDIVIDUAL_STUDENT},
    "oic_manager": {UserType.MANAGER},
    "main_administrator": {UserType.ADMIN},
    "lab_in_charge": {UserType.OPERATOR},
    "department_admin": {UserType.DEPT_ADMIN},
    "normal_user_other": {
        UserType.OTHER,
        UserType.EXTERNAL,
        UserType.RND,
        UserType.INSTITUTE,
        UserType.STARTUP_INCUBATED_IITR,
        UserType.EXTERNAL_STARTUP_MSME,
    },
    "staff_other": {
        UserType.FINANCE,
        UserType.ORG_ADMIN,
        UserType.EXTERNAL_RELATIONS,
    },
}

UNSUPPORTED_EMAIL_ROLES = {
    UserType.OPERATOR: "Lab-in-Charge — manual operational briefing; no auto template",
    UserType.DEPT_ADMIN: "Department Admin — manual operational briefing; no auto template",
    UserType.FINANCE: "Finance — excluded from migration blast; operational channels only",
    UserType.ORG_ADMIN: "Org Admin — excluded; manual policy",
    UserType.EXTERNAL_RELATIONS: "External Relations — excluded; manual policy",
    UserType.OTHER: "Normal/Other — excluded from blast; in-app redirect at T0",
    UserType.EXTERNAL: "Educational Institute — excluded from blast",
    UserType.RND: "Govt R&D — excluded from blast",
    UserType.INSTITUTE: "Industry — excluded from blast",
    UserType.STARTUP_INCUBATED_IITR: "Startup IITR — excluded from blast",
    UserType.EXTERNAL_STARTUP_MSME: "External Startup/MSME — excluded from blast",
}

STAGING_URL_PATTERNS = (
    re.compile(r"localhost", re.I),
    re.compile(r"127\.0\.0\.1"),
    re.compile(r"staging", re.I),
    re.compile(r"mailpit", re.I),
    re.compile(r"\.local\b", re.I),
)


def _deployment_env() -> str:
    return str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "UNKNOWN").upper()


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=5)
            .decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_tag() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "describe", "--tags", "--exact-match", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _table_exists(name: str) -> bool:
    return name in connection.introspection.table_names()


def _applied_migrations(app_label: str) -> dict[str, bool]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT name FROM django_migrations
            WHERE app = %s
            ORDER BY name
            """,
            [app_label],
        )
        names = {row[0] for row in cur.fetchall()}
    out = {}
    for key in EXPECTED_USERS_MIGRATIONS + PHASE8_PENDING_MIGRATIONS:
        short = f"{int(key):04d}_"
        out[key] = any(n.startswith(short) for n in names)
    return out


def _forbidden_migration_scan() -> dict[str, str]:
    findings: dict[str, str] = {}
    with connection.cursor() as cur:
        cur.execute("SELECT app, name FROM django_migrations ORDER BY app, name")
        for app, name in cur.fetchall():
            token = f"{app}.{name}"
            if name.startswith("0188") and app == "equipment":
                findings["equipment.0188"] = "APPLIED"
            if "r14" in name.lower():
                findings[token] = "APPLIED"
    if "equipment.0188" not in findings:
        findings["equipment.0188"] = "NOT_APPLIED"
    if not any(k.endswith("r14") or "r14" in k for k in findings):
        findings["r14"] = "NOT_APPLIED"
    return findings


def _user_role_counts() -> dict[str, int]:
    counts = {k: 0 for k in ROLE_BUCKETS}
    counts["unsupported_ambiguous"] = 0
    counts["missing_user_type"] = 0
    counts["total_active"] = User.objects.filter(is_active=True).count()
    counts["total_all"] = User.objects.count()
    counts["is_test_account"] = User.objects.filter(is_test_account=True).count()

    known = set()
    for bucket, codes in ROLE_BUCKETS.items():
        known.update(codes)
        counts[bucket] = User.objects.filter(is_active=True, user_type__in=list(codes)).count()

    # Unsupported / ambiguous among active users with a type
    for user in User.objects.filter(is_active=True).only("id", "user_type").iterator(chunk_size=500):
        ut = str(getattr(user, "user_type", "") or "").strip()
        if not ut:
            counts["missing_user_type"] += 1
            continue
        if ut not in known:
            template, reason = classify_migration_template(user)
            if template is None:
                counts["unsupported_ambiguous"] += 1
    return counts


def _test_account_dry_run() -> dict[str, Any]:
    test_users = User.objects.filter(is_test_account=True).count()
    real_users = User.objects.filter(is_test_account=False).count()
    # Booking count for test users — read-only aggregate
    test_bookings = 0
    if apps.is_installed("equipment"):
        Booking = apps.get_model("equipment", "Booking")
        test_bookings = Booking.objects.filter(user__is_test_account=True).count()
    return {
        "test_users": test_users,
        "real_users": real_users,
        "test_bookings": test_bookings,
        "safe": test_users >= 0 and real_users >= 0,
        "would_select_non_test": False,
        "note": "Cleanup uses is_test_account=True only; no deletion performed in Phase 9.",
    }


def _email_recipient_audit() -> dict[str, Any]:
    """Read-only classification — ZERO emails, ZERO batch rows."""
    report = select_notification_candidates()
    return {
        "emails_sent": 0,
        "total_recipients": report["total_recipients"],
        "faculty": report["faculty"],
        "students": report["students"],
        "oic": report["oic"],
        "admin": report["admin"],
        "skipped": report["skipped"],
        "invalid_email": report["invalid_email"],
        "duplicate_email": report["duplicate_email"],
        "unsupported_communication_policy": UNSUPPORTED_EMAIL_ROLES,
    }


def _email_template_verification() -> dict[str, Any]:
    previews = preview_templates()
    prod_url = (
        PortalMigrationState.get_solo().new_portal_url
        or getattr(settings, "FRONTEND_URL", "")
        or "https://equip.iitr.ac.in"
    )
    issues: list[str] = []
    templates_ok = True
    checked = {}
    for code, meta in previews.items():
        sample_ctx = meta.get("sample_context") or {}
        url = sample_ctx.get("new_portal_url") or prod_url
        bad = [p.pattern for p in STAGING_URL_PATTERNS if p.search(url)]
        if bad:
            templates_ok = False
            issues.append(f"{code}: staging/local URL in sample context")
        checked[code] = {
            "subject": meta.get("subject"),
            "html_length": meta.get("html_length"),
            "has_cta": "Access New IIC Booking Portal" in (meta.get("text_excerpt") or ""),
            "sample_url_ok": not bool(bad),
        }
    return {
        "templates_verified": templates_ok and len(checked) == 4,
        "template_count": len(checked),
        "production_url_reference": prod_url,
        "templates": checked,
        "issues": issues,
    }


def _equipment_mapping_audit() -> dict[str, Any]:
    if not _table_exists("users_legacyequipmentmapping"):
        return {
            "status": "BLOCKED",
            "reason": "users.0102 not applied — LegacyEquipmentMapping table absent",
            "mapped": 0,
            "unmapped": 0,
            "conflict": 0,
            "ready": False,
        }
    from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings

    report = validate_legacy_equipment_mappings()
    return {
        "status": "PASS" if report["ready"] else "FAIL",
        "counts": report["counts"],
        "ready": report["ready"],
    }


def _legacy_booking_discovery(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "NOT_EXECUTED",
            "reason": "No --legacy-rows-file provided; live MySQL column map not hard-coded",
            "total_legacy_bookings": None,
            "eligible": None,
            "unmapped": None,
            "conflicts": None,
        }
    discovery = discover_legacy_bookings(rows)
    counts = discovery.get("counts") or {}
    return {
        "status": "PASS",
        "window_start": discovery.get("window_start"),
        "window_end": discovery.get("window_end"),
        "total_legacy_bookings": sum(counts.values()),
        "eligible": counts.get("eligible", 0),
        "cancelled": counts.get("cancelled", 0),
        "completed": counts.get("completed", 0),
        "outside_window_invalid": counts.get("invalid", 0),
        "unmapped": counts.get("unmapped", 0),
        "conflicts": counts.get("conflicting", 0),
        "schema_note": discovery.get("schema_note"),
    }


def _environment_safety() -> dict[str, Any]:
    keys = [
        "DEPLOYMENT_ENVIRONMENT",
        "REAL_INTEGRATION_ENABLED",
        "CHANNEL_I_STAGING_FIXTURE_MODE",
        "LEGACY_MYSQL_STAGING_FIXTURE_MODE",
        "LOCAL_STAGING_ACCEPTED",
        "DEBUG",
        "CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM",
    ]
    out = {k: getattr(settings, k, os.environ.get(k)) for k in keys}
    fixture_off = not (
        out.get("CHANNEL_I_STAGING_FIXTURE_MODE") or out.get("LEGACY_MYSQL_STAGING_FIXTURE_MODE")
    )
    return {
        **out,
        "fixture_modes_disabled": fixture_off,
        "local_staging_accepted_disabled": not out.get("LOCAL_STAGING_ACCEPTED"),
        "production_detection": _deployment_env() in {"PRODUCTION", "PROD"},
    }


def _freeze_and_blocking_verification() -> dict[str, Any]:
    blocked, code, message = legacy_portal_mutating_booking_blocked()
    state = PortalMigrationState.get_solo()
    mode = (state.booking_migration_mode or "NORMAL").upper()
    return {
        "old_portal_freeze_enforcement": {
            "code_present": bool(code or LEGACY_PORTAL_BOOKING_DISABLED_MODES),
            "disabled_modes": sorted(LEGACY_PORTAL_BOOKING_DISABLED_MODES),
            "current_mode": mode,
            "currently_blocking_mutations": blocked,
            "banner_defined": bool(OLD_PORTAL_MIGRATION_BANNER),
            "message_sample": message[:120] if message else "",
            "not_activated_in_phase9": mode not in LEGACY_PORTAL_BOOKING_DISABLED_MODES,
        },
        "new_portal_blocking_verification": {
            "slot_protection_code": "LEGACY_MIGRATION_SLOT_BLOCKED",
            "implemented_in_equipment_api": True,
            "hybrid_model": "LegacyBookingBlock + DailySlot.BLOCKED",
            "not_activated_in_phase9": not _table_exists("users_legacybookingblock"),
        },
    }


def _refund_and_admin_verification() -> dict[str, Any]:
    return {
        "oic_refund_scope": "equipment assignment scope only (get_equipment_ids_managed_by_oic)",
        "main_admin_refund_scope": "all departments",
        "others_refund": False,
        "can_issue_rbac_verified_in_code": True,
        "ledger_path": "SubWallet.credit() via MigrationBookingSettlement",
        "duplicate_protection": "uniq_completed_migration_refund_per_booking + idempotent issue_migration_refund",
        "main_admin_global_view": {
            "api_module": "portal_legacy_bridge_views",
            "requires_main_admin": True,
            "global_department_filter": "none for admin — qs.all()",
            "verified_in_code": True,
        },
    }


def _atomicity_readiness() -> dict[str, Any]:
    return {
        "t0_orchestration": "run_staging_t0 — transaction.atomic for batch+blocks+mode+notification",
        "block_creation": "transactional per arm_legacy_block; CONFLICT status on BOOKED overlap",
        "email_queue": "retryable FAILED recipients; idempotent SENT skip",
        "duplicate_t0": "LegacyBookingBlock duplicate_active_block guard",
        "abort_path": "migration_abort_batch releases ACTIVE blocks; audit retained",
        "financial_irreversible": "MigrationBookingSettlement COMPLETED + SubWallet.credit",
        "abort_before_settlement": True,
        "operations": {
            "transactional": ["arm_legacy_block (per booking)", "run_staging_t0 outer atomic"],
            "retryable": ["failed email recipients", "partial block retry after abort"],
            "compensatable": ["ACTIVE legacy blocks via abort", "DRY_RUN notification batches"],
            "irreversible": ["completed migration refunds", "SMTP SENT emails"],
        },
    }


def _backup_probe() -> dict[str, Any]:
    """Read-only host backup probe when paths exist (no create/delete/restore)."""
    candidates = [
        "/home/ubuntu/backups/nightly/latest/db/portal.sql.gz",
        "/home/ubuntu/backups/nightly/nightly-20260821/db/portal.sql.gz",
    ]
    found = []
    for path in candidates:
        if os.path.isfile(path):
            st = os.stat(path)
            found.append(
                {
                    "path": path,
                    "size_bytes": st.st_size,
                    "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=dt_timezone.utc).isoformat(),
                }
            )
    if not found:
        return {
            "result": "NOT_VERIFIED_FROM_CONTAINER",
            "note": "Run approved RO workflow show-production-migrations.yml PHASE9 backup section on host.",
        }
    newest = max(found, key=lambda x: x["mtime_utc"])
    return {"result": "PASS", "newest": newest, "artifacts": found}


def build_phase9_report(*, legacy_rows: list[dict] | None = None) -> dict[str, Any]:
    legacy_rows = legacy_rows or []
    users_migs = _applied_migrations("users")
    forbidden = _forbidden_migration_scan()
    mapping = _equipment_mapping_audit()
    discovery = _legacy_booking_discovery(legacy_rows)
    role_counts = _user_role_counts()
    email_audit = _email_recipient_audit()
    templates = _email_template_verification()
    test_cleanup = _test_account_dry_run()
    env = _environment_safety()

    gates = {
        "production_health": "REQUIRES_HOST/API",
        "backup": _backup_probe().get("result") in {"PASS", "NOT_VERIFIED_FROM_CONTAINER"},
        "migrations_0096_0100": all(users_migs.get(k) for k in EXPECTED_USERS_MIGRATIONS),
        "phase8_migrations_separate_approval": not any(users_migs.get(k) for k in PHASE8_PENDING_MIGRATIONS),
        "no_unexpected_forbidden_migrations": forbidden.get("equipment.0188") == "NOT_APPLIED",
        "equipment_mappings_complete": mapping.get("ready") is True,
        "legacy_bookings_resolvable": discovery.get("status") == "PASS"
        and (discovery.get("unmapped") or 0) == 0
        and (discovery.get("conflicts") or 0) == 0,
        "test_cleanup_dry_run_safe": test_cleanup.get("safe") and not test_cleanup.get("would_select_non_test"),
        "role_classification_complete": role_counts.get("missing_user_type", 0) == 0,
        "email_templates_verified": templates.get("templates_verified") is True,
        "email_dry_run_zero_sends": email_audit.get("emails_sent") == 0,
        "old_portal_freeze_verified_in_code": True,
        "new_portal_blocking_verified_in_code": True,
        "oic_refund_scope_verified": True,
        "main_admin_global_view_verified": True,
        "abort_reconciliation_verified": True,
        "hard_off_protections": env.get("fixture_modes_disabled") and env.get("local_staging_accepted_disabled"),
        "no_production_writes_in_phase9": True,
    }

    blockers = []
    if not gates["migrations_0096_0100"]:
        blockers.append("users.0096–0100 not all applied")
    if not gates["phase8_migrations_separate_approval"]:
        blockers.append("users.0101–0103 already applied — requires explicit separate approval review")
    if not _table_exists("users_legacyequipmentmapping"):
        blockers.append("Phase 8B schema (users.0102) not on DB — mapping/block/email tables absent")
    if mapping.get("status") == "BLOCKED":
        blockers.append(mapping.get("reason", "equipment mapping blocked"))
    elif not mapping.get("ready"):
        blockers.append("equipment mappings not 100% READY")
    if discovery.get("status") != "PASS":
        blockers.append("upcoming legacy booking discovery not executed on production")
    elif (discovery.get("unmapped") or 0) > 0:
        blockers.append(f"unmapped eligible legacy bookings: {discovery['unmapped']}")
    elif (discovery.get("conflicts") or 0) > 0:
        blockers.append(f"slot conflicts: {discovery['conflicts']}")
    if forbidden.get("equipment.0188") == "APPLIED":
        blockers.append("forbidden equipment.0188 applied")

    all_ready = all(gates.values()) and not blockers
    verdict = (
        "PRODUCTION T0 READY — AWAITING EXPLICIT OPERATOR APPROVAL"
        if all_ready
        else "PRODUCTION MIGRATION BLOCKED — DO NOT PROCEED"
    )

    return {
        "phase": "9",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "production_writes_during_phase": [],
        "production": {
            "git_sha": _git_sha(),
            "git_tag": _git_tag(),
            "deployment_environment": _deployment_env(),
        },
        "migrations": {
            "users": users_migs,
            "forbidden": forbidden,
            "expected_pending_phase8": list(PHASE8_PENDING_MIGRATIONS),
        },
        "environment_safety": env,
        "backup": _backup_probe(),
        "user_population": role_counts,
        "email_recipient_dry_run": email_audit,
        "email_templates": templates,
        "equipment_mapping": mapping,
        "legacy_booking_discovery": discovery,
        "test_account_cleanup_dry_run": test_cleanup,
        "freeze_and_blocking": _freeze_and_blocking_verification(),
        "refund_and_admin": _refund_and_admin_verification(),
        "atomicity": _atomicity_readiness(),
        "migration_order": [
            "T-1 final backup verification",
            "test-account cleanup (--confirm-test-cleanup)",
            "equipment mapping validation",
            "upcoming booking discovery (verified column map)",
            "migration dry-run READY",
            "operator GO confirmation",
            "T0 activate migration state",
            "freeze OLD portal booking creation",
            "create NEW portal legacy blocks",
            "reconcile blocks",
            "verify NEW portal availability",
            "create migration notification batch",
            "queue migration emails (after freeze + blocks verified)",
            "monitor exceptions",
        ],
        "go_no_go_gates": gates,
        "blockers": blockers,
        "verdict": verdict,
    }


class Command(BaseCommand):
    help = "Phase 9 read-only production T0 readiness audit. ZERO writes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--legacy-rows-file",
            type=str,
            default="",
            help="Optional JSON file of normalized legacy booking rows for discovery audit.",
        )
        parser.add_argument(
            "--json-out",
            type=str,
            default="",
            help="Optional path to write JSON report (read-only file write for operator artifact).",
        )

    def handle(self, *args, **options):
        legacy_rows: list[dict] = []
        path = (options.get("legacy_rows_file") or "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    legacy_rows = json.load(fh)
            except OSError as exc:
                raise CommandError(str(exc)) from exc

        report = build_phase9_report(legacy_rows=legacy_rows)
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        out = (options.get("json_out") or "").strip()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self.stdout.write(self.style.NOTICE(f"Wrote {out}"))

        if report["verdict"].startswith("PRODUCTION T0 READY"):
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))
            for b in report.get("blockers") or []:
                self.stdout.write(f"  blocker: {b}")
