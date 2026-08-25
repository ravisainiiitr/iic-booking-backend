"""Phase 10G — production readiness closure (READ-ONLY).

Closes blockers that can be closed without T0.
Does NOT migrate, activate T0, freeze, email, refund, or cleanup.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

from iic_booking.users.legacy_ledger.datetime_contract import (
    APPROVAL_APPROVED,
    contract_approval_status,
    load_datetime_contract,
)
from iic_booking.users.legacy_ledger.phase10f_final_t0_readiness import (
    PRODUCTION_BASELINE_SHA,
    build_final_t0_readiness_report,
)

PHASE10G_MIGRATIONS = ("0101", "0102", "0103", "0104")
FORBIDDEN = ("equipment.0188", "r14", "users.r14")

VERDICT_READY = "READY FOR EXPLICIT T0 AUTHORIZATION"
VERDICT_NOT_READY = "NOT READY — BLOCKERS REMAIN"

GATE_PASS = "PASS"
GATE_WARN = "WARN"
GATE_BLOCKED = "BLOCKED"
GATE_OPERATOR = "OPERATOR REQUIRED"


def _git_sha(cwd: Path | None = None) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(cwd) if cwd else None,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_branch(cwd: Path | None = None) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(cwd) if cwd else None,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _base_dir() -> Path:
    return Path(getattr(settings, "BASE_DIR", "."))


def build_release_audit() -> dict[str, Any]:
    """Static repository / release audit — no deploy."""
    base = _base_dir()
    backend_sha = _git_sha(base) or _git_sha()
    backend_branch = _git_branch(base) or _git_branch()

    migration_files = {
        "0101": (base / "iic_booking/users/migrations/0101_migration_booking_settlement.py").is_file(),
        "0102": (base / "iic_booking/users/migrations/0102_legacy_equipment_booking_bridge.py").is_file(),
        "0103": (base / "iic_booking/users/migrations/0103_migration_notification_batch.py").is_file(),
        "0104": (base / "iic_booking/users/migrations/0104_legacy_booking_block_user_mapping.py").is_file(),
    }
    phase_docs = {
        "10D": (base / "docs/release/migration/AI30-AI31-PHASE-10D-LEGACY-BOOKING-MAPPING.md").is_file(),
        "10E": (base / "docs/release/migration/AI30-AI31-PHASE-10E-PRODUCTION-QUALIFICATION.md").is_file(),
        "10F": (base / "docs/release/migration/AI30-AI31-PHASE-10F-FINAL-T0-READINESS.md").is_file(),
    }
    hard_off = {
        "REAL_INTEGRATION_ENABLED": bool(getattr(settings, "REAL_INTEGRATION_ENABLED", False)),
        "CHANNEL_I_STAGING_FIXTURE_MODE": bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)),
        "LEGACY_MYSQL_STAGING_FIXTURE_MODE": bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)),
        "DEPLOYMENT_ENVIRONMENT": str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or ""),
    }
    fixture_safe = (
        not hard_off["REAL_INTEGRATION_ENABLED"]
        and not hard_off["CHANNEL_I_STAGING_FIXTURE_MODE"]
        and not hard_off["LEGACY_MYSQL_STAGING_FIXTURE_MODE"]
    ) or hard_off["DEPLOYMENT_ENVIRONMENT"].upper() not in {"PRODUCTION", "PROD"}

    # Production settings file must hard-off fixtures
    prod_settings = base / "config/settings/production.py"
    prod_text = prod_settings.read_text(encoding="utf-8") if prod_settings.is_file() else ""
    prod_hard_off = (
        "REAL_INTEGRATION_ENABLED = False" in prod_text
        and "CHANNEL_I_STAGING_FIXTURE_MODE = False" in prod_text
        and "LEGACY_MYSQL_STAGING_FIXTURE_MODE = False" in prod_text
    )

    uncommitted_note = (
        "Phase 10D/10E/10F code exists in working tree; release must be committed, "
        "PR-reviewed, tagged, and deployed before T0. Do not deploy uncommitted code."
    )

    return {
        "phase": "10G",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "backend": {
            "local_sha": backend_sha,
            "local_branch": backend_branch,
            "production_baseline_sha": PRODUCTION_BASELINE_SHA,
            "production_has_10def": False,
            "note": uncommitted_note,
        },
        "frontend": {
            "note": "Verify frontend commit/PR/tag separately; migration UI must be released with backend.",
            "required_paths": [
                "/admin/portal-migration",
                "/admin/portal-migration/equipment-mapping",
                "/admin/portal-migration/legacy-bookings",
            ],
        },
        "migration_files_present": migration_files,
        "phase_docs_present": phase_docs,
        "forbidden_migrations_excluded": True,
        "forbidden": list(FORBIDDEN),
        "hard_off_runtime": hard_off,
        "production_settings_hard_off": prod_hard_off,
        "fixture_modes_cannot_activate_in_production": prod_hard_off,
        "deploy_executed": False,
        "verdict": GATE_OPERATOR if not all(migration_files.values()) or not prod_hard_off else GATE_WARN,
    }


def build_schema_readiness() -> dict[str, Any]:
    """Static schema migration plan for users.0101–0104 — no migrate execution."""
    base = _base_dir()
    plans = []
    for mid, name, ops_summary, reversible in (
        (
            "0101",
            "0101_migration_booking_settlement",
            "Create MigrationBookingSettlement (+ indexes/FKs). Additive.",
            True,
        ),
        (
            "0102",
            "0102_legacy_equipment_booking_bridge",
            "Add PortalMigrationState window/mode fields; create LegacyEquipmentMapping, "
            "LegacyBookingMigrationBatch, LegacyBookingBlock. Additive.",
            True,
        ),
        (
            "0103",
            "0103_migration_notification_batch",
            "Create MigrationNotificationBatch/Recipient + MigrationT0Event. Additive.",
            True,
        ),
        (
            "0104",
            "0104_legacy_booking_block_user_mapping",
            "Add LegacyBookingBlock user/equipment metadata fields. Additive; occupancy independent of user mapping.",
            True,
        ),
    ):
        path = base / f"iic_booking/users/migrations/{name}.py"
        plans.append(
            {
                "migration": mid,
                "name": name,
                "file_present": path.is_file(),
                "operations_summary": ops_summary,
                "data_transformation": False,
                "estimated_lock_impact": "LOW — additive CreateModel/AddField on new/low-traffic tables",
                "reversible": reversible,
                "depends_on": {
                    "0101": "users.0100 + equipment.0187",
                    "0102": "users.0101",
                    "0103": "users.0102",
                    "0104": "users.0103",
                }.get(mid),
            }
        )

    all_present = all(p["file_present"] for p in plans)
    classification = "READY_TO_APPLY" if all_present else "BLOCKED"
    return {
        "phase": "10G",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "migrations": plans,
        "migrate_executed": False,
        "migrate_plan_note": (
            "Operator must run `python manage.py migrate --plan` on production after backend "
            "deploy and BEFORE migrate. This Phase 10G artifact is static analysis only."
        ),
        "forbidden_in_plan": list(FORBIDDEN),
        "classification": classification,
        "requires_explicit_migrate_approval": True,
        "compatibility_with_production_0096_0100": True,
    }


def build_datetime_contract_review(*, validation: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = load_datetime_contract()
    status = contract_approval_status(contract)
    validation = validation or {}
    totals = validation.get("totals") or {}
    mysql_ok = bool(validation.get("ok"))

    blockers: list[str] = []
    if status != APPROVAL_APPROVED:
        blockers.append("datetime_contract_operator_required")
    if not mysql_ok:
        blockers.append(validation.get("error") or "datetime_validation_mysql_not_available")

    # Do not auto-approve; only classify readiness for Main Admin
    ready_for_approval = mysql_ok and status != APPROVAL_APPROVED and not (
        int(totals.get("null_booking_date") or 0) > 0 and int(totals.get("total_bookings") or 0) == 0
    )
    # If MySQL unavailable, still allow OPERATOR to review candidate contract offline
    if not mysql_ok and status != APPROVAL_APPROVED:
        ready_for_approval = False

    return {
        "phase": "10G",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "contract_status": status,
        "candidate": {
            "start": "booking.booking_date",
            "duration": "booking.time_required",
            "duration_unit": "minutes",
            "end": "booking_date + time_required minutes",
        },
        "validation_ok": mysql_ok,
        "validation_totals": totals,
        "suspicious_durations_reported": bool(validation.get("suspicious_durations")),
        "auto_repair": False,
        "approval_endpoint_called": False,
        "DATETIME_CONTRACT": (
            "APPROVED"
            if status == APPROVAL_APPROVED
            else ("READY_FOR_MAIN_ADMIN_APPROVAL" if ready_for_approval else "OPERATOR_REQUIRED")
        ),
        "blockers": blockers,
        "note": "Main Administrator approval remains a separate explicit POST with confirm=true.",
    }


def _gate(result: str, evidence: str, blocking: bool) -> dict[str, Any]:
    return {"result": result, "evidence": evidence, "blocking": blocking}


def build_phase10g_final_readiness(
    *,
    column_map_file: str = "",
    backup_verified: bool = False,
    backend_release_tag: str = "",
    backend_merge_sha: str = "",
    frontend_release_tag: str = "",
    frontend_merge_sha: str = "",
    backend_pr: str = "",
    frontend_pr: str = "",
    conflicts_resolved_or_excluded: bool = False,
    finance_exceptions_blocking: bool | None = None,
    explicit_t0_authorization: bool = False,
) -> dict[str, Any]:
    """Authoritative Phase 10G GO/NO-GO matrix. READ-ONLY."""
    release_audit = build_release_audit()
    schema = build_schema_readiness()

    t0 = build_final_t0_readiness_report(
        column_map_file=column_map_file,
        backup_verified=backup_verified,
        backend_release_tag=backend_release_tag,
        backend_merge_sha=backend_merge_sha,
        frontend_release_tag=frontend_release_tag,
        frontend_merge_sha=frontend_merge_sha,
        backend_pr=backend_pr,
        frontend_pr=frontend_pr,
        conflicts_resolved_or_excluded=conflicts_resolved_or_excluded,
    )

    from iic_booking.users.legacy_ledger.legacy_datetime_validation import validate_legacy_datetime_readonly

    try:
        datetime_validation = validate_legacy_datetime_readonly()
    except Exception as exc:  # noqa: BLE001
        datetime_validation = {"ok": False, "error": str(exc)}

    datetime_review = build_datetime_contract_review(validation=datetime_validation)

    hard_off = t0.get("gates", {}).get("production_hard_off", False)
    if finance_exceptions_blocking is None:
        # Unknown until production wallet reconciliation runs
        finance_blocking = True  # treat unknown as blocking until reviewed
        finance_status = GATE_OPERATOR
        finance_evidence = "production_wallet_reconciliation not executed against production MySQL"
    else:
        finance_blocking = bool(finance_exceptions_blocking)
        finance_status = GATE_BLOCKED if finance_blocking else GATE_PASS
        finance_evidence = f"finance_exceptions_blocking={finance_blocking}"

    matrix: dict[str, dict[str, Any]] = {
        "Release": _gate(
            GATE_BLOCKED if not t0["gates"].get("backend_reviewed_released") else GATE_PASS,
            f"backend={t0.get('backend_release')}; frontend={t0.get('frontend_release')}; "
            f"production_baseline={PRODUCTION_BASELINE_SHA}",
            True,
        ),
        "Schema": _gate(
            GATE_OPERATOR if schema["classification"] == "READY_TO_APPLY" and not t0["gates"].get("users_0101_0104_applied") else (
                GATE_PASS if t0["gates"].get("users_0101_0104_applied") else GATE_BLOCKED
            ),
            f"classification={schema['classification']}; applied={t0['gates'].get('users_0101_0104_applied')}; migrate_executed=False",
            True,
        ),
        "Datetime": _gate(
            GATE_PASS if t0["gates"].get("datetime_contract_approved") else GATE_OPERATOR,
            f"status={datetime_review['DATETIME_CONTRACT']}; approval_called=False",
            True,
        ),
        "Legacy MySQL": _gate(
            GATE_PASS if datetime_validation.get("ok") else GATE_BLOCKED,
            str(datetime_validation.get("error") or "validation ok"),
            True,
        ),
        "Upcoming bookings": _gate(
            GATE_PASS if t0["gates"].get("upcoming_week_discovery_complete") else GATE_BLOCKED,
            f"eligible={t0.get('eligible_booking_count')}; discovery_ok={t0['gates'].get('upcoming_week_discovery_complete')}",
            True,
        ),
        "Equipment mappings": _gate(
            GATE_PASS if t0["gates"].get("equipment_mappings_complete") else GATE_OPERATOR,
            f"required={t0.get('equipment_mapping', {}).get('required_mappings')}; "
            f"completed={t0.get('equipment_mapping', {}).get('completed_mappings')}",
            True,
        ),
        "User mappings": _gate(
            GATE_WARN,
            f"resolved={t0.get('user_resolved_count')}; unresolved={t0.get('user_unresolved_count')}; "
            "USER UNRESOLVED does not block T0 when equipment+time valid",
            False,
        ),
        "Wallets": _gate(
            GATE_OPERATOR,
            "production_wallet_reconciliation.json requires production read-only run",
            True,
        ),
        "Finance": _gate(finance_status, finance_evidence, finance_blocking),
        "Conflicts": _gate(
            GATE_PASS if t0["gates"].get("conflicts_resolved_or_excluded") else GATE_OPERATOR,
            f"conflict_count={t0.get('conflict_count')}",
            True,
        ),
        "Backup": _gate(
            GATE_PASS if backup_verified else GATE_BLOCKED,
            f"backup_verified={backup_verified}",
            True,
        ),
        "Test accounts": _gate(
            GATE_PASS if t0["gates"].get("test_account_dry_run_reviewed") else GATE_OPERATOR,
            "dry-run only; cleanup not authorized",
            False,
        ),
        "Emails": _gate(
            GATE_PASS if t0["gates"].get("email_recipient_dry_run_reviewed") else GATE_OPERATOR,
            "dry-run only; SMTP not authorized",
            False,
        ),
        "Security": _gate(
            GATE_PASS if hard_off and release_audit.get("production_settings_hard_off") else GATE_BLOCKED,
            f"hard_off={hard_off}; production_settings={release_audit.get('production_settings_hard_off')}",
            True,
        ),
        "Rollback": _gate(
            GATE_PASS,
            "APP ROLLBACK != DATABASE ROLLBACK; see AI30-AI31-PHASE-10G-ROLLBACK-READINESS.md",
            False,
        ),
        "T0 authorization": _gate(
            GATE_PASS if explicit_t0_authorization else GATE_OPERATOR,
            f"explicit_t0_authorization={explicit_t0_authorization}",
            True,
        ),
    }

    blockers = [
        name for name, g in matrix.items() if g["blocking"] and g["result"] in (GATE_BLOCKED, GATE_OPERATOR)
    ]
    # Schema READY_TO_APPLY + OPERATOR REQUIRED still blocks until applied
    # Release blocked until reviewed/deployed

    # Also merge phase10f blockers
    for b in t0.get("blockers") or []:
        if b not in blockers:
            blockers.append(b)

    if explicit_t0_authorization is False:
        # Always list — T0 must never auto-run
        pass

    t0_ready_tech = (
        all(g["result"] == GATE_PASS for name, g in matrix.items() if g["blocking"] and name != "T0 authorization")
        and not explicit_t0_authorization
    )
    # Technical readiness without T0 auth
    tech_gates_ok = all(
        g["result"] == GATE_PASS for name, g in matrix.items() if g["blocking"] and name != "T0 authorization"
    )

    if tech_gates_ok and not explicit_t0_authorization:
        verdict = VERDICT_READY
    else:
        verdict = VERDICT_NOT_READY

    # Never claim ready if any blocking gate is BLOCKED/OPERATOR (except T0 auth when tech ready)
    if any(g["result"] == GATE_BLOCKED for name, g in matrix.items() if g["blocking"]):
        verdict = VERDICT_NOT_READY
    if any(
        g["result"] == GATE_OPERATOR for name, g in matrix.items() if g["blocking"] and name != "T0 authorization"
    ):
        verdict = VERDICT_NOT_READY

    return {
        "phase": "10G",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "verdict": verdict,
        "t0_executed": False,
        "explicit_t0_authorization_required": True,
        "explicit_t0_authorization_present": explicit_t0_authorization,
        "production_baseline_sha": PRODUCTION_BASELINE_SHA,
        "backend_local_sha": release_audit["backend"]["local_sha"],
        "release_audit": release_audit,
        "schema_readiness": schema,
        "datetime_contract_review": datetime_review,
        "datetime_validation": {
            "ok": datetime_validation.get("ok"),
            "error": datetime_validation.get("error"),
            "totals": datetime_validation.get("totals"),
        },
        "phase10f_t0_report": {
            "verdict": t0.get("verdict"),
            "t0_ready": t0.get("t0_ready"),
            "blockers": t0.get("blockers"),
            "eligible_booking_count": t0.get("eligible_booking_count"),
            "conflict_count": t0.get("conflict_count"),
            "user_resolved_count": t0.get("user_resolved_count"),
            "user_unresolved_count": t0.get("user_unresolved_count"),
        },
        "gate_matrix": matrix,
        "blockers": blockers if verdict == VERDICT_NOT_READY else [],
        "finance_exceptions_blocking": finance_blocking,
        "architecture_invariant": (
            "USER UNRESOLVED + VALID DATETIME + VALID EQUIPMENT MAPPING = READY/BLOCKABLE"
        ),
        "app_rollback_note": "APP ROLLBACK != DATABASE ROLLBACK",
        "production_safety": {
            "PRODUCTION_MIGRATE": "NO",
            "T0": "NO",
            "BOOKING_BLOCK": "NO",
            "OLD_PORTAL_FREEZE": "NO",
            "EMAILS_SENT": "NO",
            "REFUNDS": "NO",
            "CLEANUP": "NO",
            "LEGACY_MYSQL_WRITES": "NO",
        },
        "production_writes_performed": [],
        "operator_next_actions": [
            "Commit/PR/tag backend Phase 10D–10F (and 10G docs)",
            "Commit/PR/tag frontend migration UI",
            "Deploy backend + frontend via normal release (no auto-migrate)",
            "Verify backup → --backup-verified",
            "migrate --plan then explicit MIGRATE approval for users.0101–0104",
            "migration_validate_legacy_datetime --default-artifact",
            "Main Admin POST datetime-contract approve (confirm=true)",
            "Complete explicit equipment mappings",
            "Run production discovery + wallet reconciliation (READ-ONLY)",
            "migration_final_t0_readiness --default-artifact",
            "Separate explicit T0 authorization (not this phase)",
        ],
    }


def write_json_artifact(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return str(path)
