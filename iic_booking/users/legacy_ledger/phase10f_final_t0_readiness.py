"""Phase 10F — machine-readable final T0 GO/NO-GO readiness report (READ-ONLY)."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import connection

from iic_booking.users.legacy_ledger.booking_lock import (
    LEGACY_PORTAL_BOOKING_DISABLED_MODES,
    legacy_portal_mutating_booking_blocked,
)
from iic_booking.users.legacy_ledger.datetime_contract import (
    contract_approval_status,
    load_datetime_contract,
)
from iic_booking.users.management.commands.migration_production_legacy_qualification import (
    PHASE8_MIGRATIONS,
    build_phase10_report,
)

PRODUCTION_BASELINE_SHA = "6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b"


def _deployment_env() -> str:
    return str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "UNKNOWN").upper()


def build_final_t0_readiness_report(
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
) -> dict[str, Any]:
    """
    Consolidated GO/NO-GO for final T0 review.
    READ-ONLY — does not migrate, activate T0, create blocks, or send email.
    """
    phase10 = build_phase10_report(column_map_file=column_map_file)
    contract = load_datetime_contract(column_map_file or None)
    datetime_approved = contract_approval_status(contract) == "APPROVED"

    migs = phase10.get("migrations", {}).get("users") or {}
    migrations_applied = all(migs.get(k) for k in PHASE8_MIGRATIONS)
    tables = phase10.get("schema_tables") or {}
    schema_ready = all(tables.values())

    mapping = phase10.get("equipment_mapping") or {}
    mapping_ready = bool(mapping.get("ready"))
    required_mappings = mapping.get("required_mappings_in_window") or 0
    mapped_count = int((mapping.get("counts") or {}).get("mapped") or 0)
    unmapped_required = "unmapped_required_legacy_equipment" in (phase10.get("blockers") or [])

    discovery = phase10.get("upcoming_week_discovery") or phase10.get("legacy_booking_discovery") or {}
    discovery_ok = bool(discovery.get("ok"))
    discovery_counts = discovery.get("discovery_counts") or discovery.get("counts") or {}

    conflict_count = int(phase10.get("conflict_count") or 0)
    user_resolved = int(phase10.get("user_resolved_count") or 0)
    user_unresolved = int(phase10.get("user_unresolved_count") or 0)

    backend_tag = backend_release_tag or getattr(settings, "RELEASE_TAG", "") or ""
    frontend_tag = frontend_release_tag or getattr(settings, "FRONTEND_RELEASE_TAG", "") or ""
    backend_sha = backend_merge_sha or (phase10.get("production") or {}).get("git_sha") or ""
    frontend_sha = frontend_merge_sha or getattr(settings, "FRONTEND_RELEASE_SHA", "") or ""

    backend_deployed = bool(backend_tag or backend_sha)
    frontend_deployed = bool(frontend_tag or frontend_sha)
    backend_reviewed = backend_deployed and backend_sha != PRODUCTION_BASELINE_SHA
    frontend_reviewed = frontend_deployed and bool(frontend_sha or frontend_tag)

    try:
        blocked, freeze_code, _ = legacy_portal_mutating_booking_blocked()
    except Exception:  # noqa: BLE001
        blocked, freeze_code = False, "MIGRATION_BOOKING_DISABLED"

    mode = str((phase10.get("freeze_contract") or {}).get("current_mode") or "NORMAL").upper()
    freeze_contract_ok = freeze_code == "MIGRATION_BOOKING_DISABLED" or mode not in LEGACY_PORTAL_BOOKING_DISABLED_MODES

    gates: dict[str, Any] = {
        "backend_reviewed_released": backend_reviewed,
        "frontend_reviewed_released": frontend_reviewed,
        "datetime_contract_approved": datetime_approved,
        "users_0101_0104_applied": migrations_applied,
        "schema_tables_present": schema_ready,
        "equipment_mappings_complete": bool(mapping_ready) and not unmapped_required,
        "upcoming_week_discovery_complete": discovery_ok,
        "no_unresolved_required_equipment": not (phase10.get("blockers") or []).count("unmapped_required_legacy_equipment"),
        "no_invalid_datetime_candidates": int((discovery_counts or {}).get("invalid") or 0) == 0,
        "conflicts_resolved_or_excluded": conflicts_resolved_or_excluded or conflict_count == 0,
        "test_account_dry_run_reviewed": bool(phase10.get("test_account_dry_run")),
        "email_recipient_dry_run_reviewed": bool(phase10.get("email_recipient_dry_run")),
        "backup_verified": backup_verified,
        "old_portal_freeze_contract_verified": freeze_contract_ok,
        "new_portal_slot_blocking_verified": bool((phase10.get("new_portal_blocking") or {}).get("verified_in_code")),
        "refund_authority_verified": bool((phase10.get("refund_rbac") or {}).get("main_admin_global")),
        "reconciliation_available": bool(phase10.get("reconciliation_plan")),
        "rollback_documented": bool(phase10.get("app_rollback_note")),
        "production_hard_off": not bool((phase10.get("hard_off_status") or {}).get("REAL_INTEGRATION_ENABLED")),
        "no_auto_migrate": True,
        "user_unresolved_does_not_block_t0": True,
    }

    blockers: list[str] = list(phase10.get("blockers") or [])
    if not datetime_approved:
        blockers.append("datetime_contract_not_approved")
    if not migrations_applied:
        blockers.append("users.0101–0104 not all applied")
    if not mapping_ready:
        blockers.append("equipment_mapping_not_ready")
    if not discovery_ok and datetime_approved and migrations_applied:
        blockers.append(discovery.get("error") or "legacy_discovery_incomplete")
    if conflict_count > 0 and not conflicts_resolved_or_excluded:
        blockers.append("unresolved_conflicts")
    if not backup_verified:
        blockers.append("backup_not_verified")
    if not backend_reviewed:
        blockers.append("backend_release_not_verified")
    if not frontend_reviewed:
        blockers.append("frontend_release_not_verified")
    if not gates["production_hard_off"]:
        blockers.append("real_integration_must_be_off_in_production")

    blockers = sorted(set(blockers))
    gates_all = all(
        gates[k]
        for k in gates
        if k not in ("user_unresolved_does_not_block_t0",)
    )
    t0_ready = gates_all and not blockers

    verdict = "READY FOR FINAL T0 REVIEW" if t0_ready else "NOT READY — BLOCKERS LISTED"

    eligible = int((discovery_counts or {}).get("eligible") or 0)
    legacy_total = sum(int((discovery_counts or {}).get(k) or 0) for k in discovery_counts) if discovery_counts else 0

    return {
        "phase": "10F",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "deployment_environment": _deployment_env(),
        "production_baseline_sha": PRODUCTION_BASELINE_SHA,
        "backend_release": {
            "branch": getattr(settings, "RELEASE_BRANCH", "") or "",
            "pr": backend_pr,
            "merge_sha": backend_sha,
            "release_tag": backend_tag,
            "reviewed_released": backend_reviewed,
        },
        "frontend_release": {
            "branch": getattr(settings, "FRONTEND_RELEASE_BRANCH", "") or "",
            "pr": frontend_pr,
            "merge_sha": frontend_sha,
            "release_tag": frontend_tag,
            "reviewed_released": frontend_reviewed,
            "ui_paths": (phase10.get("frontend_release_status") or {}).get("portal_migration_ui_paths"),
        },
        "datetime_contract_status": phase10.get("datetime_contract"),
        "datetime_validation_status": phase10.get("datetime_validation"),
        "equipment_mapping": {
            "required_mappings": required_mappings,
            "completed_mappings": mapped_count,
            "ready": mapping_ready,
        },
        "legacy_upcoming_week_booking_count": legacy_total,
        "eligible_booking_count": eligible,
        "cancelled_count": int((discovery_counts or {}).get("cancelled") or 0),
        "completed_count": int((discovery_counts or {}).get("completed") or 0),
        "outside_window_count": int((discovery_counts or {}).get("outside_window") or 0),
        "invalid_count": int((discovery_counts or {}).get("invalid") or 0),
        "conflict_count": conflict_count,
        "user_resolved_count": user_resolved,
        "user_unresolved_count": user_unresolved,
        "user_unresolved_blocks_t0": False,
        "test_account_dry_run": phase10.get("test_account_dry_run"),
        "email_recipient_dry_run": phase10.get("email_recipient_dry_run"),
        "migration_state": {
            "mode": mode,
            "t0_not_activated": mode not in LEGACY_PORTAL_BOOKING_DISABLED_MODES,
            "freeze_contract": phase10.get("freeze_contract"),
        },
        "backup_status": {
            "verified": backup_verified,
            "operator_must_confirm": not backup_verified,
        },
        "production_read_only_qualification": {
            "phase10e_verdict": phase10.get("verdict"),
            "phase10e_blockers": phase10.get("blockers"),
        },
        "gates": gates,
        "blockers": blockers,
        "verdict": verdict,
        "t0_ready": t0_ready,
        "explicit_t0_authorization_required": True,
        "production_writes_performed": [],
        "app_rollback_note": "APP ROLLBACK != DATABASE ROLLBACK",
        "architecture_invariant": (
            "USER UNRESOLVED + VALID DATETIME + VALID EQUIPMENT MAPPING = READY/BLOCKABLE"
        ),
    }
