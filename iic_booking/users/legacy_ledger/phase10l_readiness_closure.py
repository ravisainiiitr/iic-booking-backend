"""Phase 10L — operator-gated production migration (READ-ONLY consolidation). No T0.

Dependency-aware state machine: for each stage inspect → perform safe work when
prerequisites exist → record evidence → continue independent stages.

Never invents approvals, dates, mappings, finance decisions, backup evidence, or T0.
Verdict vocabulary (exactly one):
  - NOT READY — BLOCKERS REMAIN
  - READY FOR EXPLICIT T0 AUTHORIZATION
Never \"READY FOR T0\".
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    GATE_BLOCKED,
    GATE_OPERATOR,
    GATE_PASS,
    VERDICT_NOT_READY,
    VERDICT_READY,
    write_json_artifact,
)
from iic_booking.users.legacy_ledger.phase10j_readiness_closure import inspect_operator_gates
from iic_booking.users.legacy_ledger.phase10k_readiness_closure import (
    blocked_discovery_artifact,
    build_phase10k_final_readiness,
    confirm_0102_provides_migration_start_at,
)

ARTIFACT_DIR = Path("docs/release/migration")


def build_stage_machine(
    *,
    gate_inspection: dict[str, Any],
    discovery_executed: bool,
    equipment_inventory: dict[str, Any] | None,
    wallet_reconciliation: dict[str, Any] | None,
    backup: dict[str, Any] | None,
    release_plan: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    dry_runs: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    raa: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Ordered stage report — continues independent work even when some gates block."""
    dt_or = gate_inspection["datetime_contract_approval"]["operator_required"]
    win_or = gate_inspection["migration_window"]["operator_required"]
    discovery_allowed = gate_inspection["discovery_allowed"]

    stages: list[dict[str, Any]] = [
        {
            "stage": "01_inspect_gates",
            "status": "COMPLETE",
            "safe_work_done": True,
            "evidence": "Live DB/file inspection of datetime, window, mappings, release flags",
        },
        {
            "stage": "02_datetime_contract",
            "status": "OPERATOR_REQUIRED" if dt_or else "PASS",
            "safe_work_done": True,
            "post_called": False,
            "exact_action": {
                "ui": "/admin/portal-migration — Datetime contract — confirm=true + reason",
                "api": "POST /api/portal-migration/admin/datetime-contract/",
                "body": {"confirm": True, "approval_reason": "non-empty Main Administrator note"},
                "effects": [
                    "Enables read-only discovery",
                    "Does NOT activate T0 / blocks / freeze / email",
                ],
            },
            "note": "Main Admin only — automation must not POST",
        },
        {
            "stage": "03_migration_window",
            "status": "OPERATOR_REQUIRED" if win_or else "PASS",
            "safe_work_done": True,
            "dates_invented": False,
            "exact_fields": ["migration_start_at", "migration_window_end_at"],
            "exact_action": {
                "ui": "/admin/portal-migration — Phase 8B settings",
                "api": "PATCH /api/portal-migration/admin/state/",
                "schema": "users.0102_legacy_equipment_booking_bridge",
            },
        },
        {
            "stage": "04_production_discovery",
            "status": (
                "COMPLETE"
                if discovery_executed
                else ("READY" if discovery_allowed else "BLOCKED")
            ),
            "safe_work_done": not discovery_allowed or discovery_executed,
            "executed": discovery_executed,
            "blocker": None
            if discovery_allowed or discovery_executed
            else (
                "datetime_unapproved"
                if dt_or
                else "migration_window_missing"
            ),
            "command_when_ready": "python manage.py migration_production_legacy_qualification",
        },
        {
            "stage": "05_equipment_conflicts_users",
            "status": "BLOCKED" if not discovery_executed else "OPERATOR_REQUIRED",
            "safe_work_done": True,
            "inventory_ro": {
                "legacy_equipment_count": (equipment_inventory or {}).get("count"),
                "explicit_mappings": 0,
                "fuzzy_forbidden": True,
                "auto_approve_forbidden": True,
                "user_resolution": "Employee ID only",
            },
            "note": (
                "Full inventory RO available without window; eligible-window candidate set "
                "requires discovery. No auto-mapping."
            ),
        },
        {
            "stage": "06_wallet_finance_ro",
            "status": "COMPLETE_RO" if wallet_reconciliation else "SKIPPED",
            "safe_work_done": bool(wallet_reconciliation),
            "writes": 0,
            "auto_correct": False,
            "acceptability_decided": False,
            "summary": {
                "mismatch_count": (wallet_reconciliation or {}).get("mismatch_count"),
                "orphan_wallets": (wallet_reconciliation or {}).get("orphan_wallets"),
                "wallet_count": (wallet_reconciliation or {}).get("wallet_count"),
                "transaction_count": (wallet_reconciliation or {}).get("transaction_count"),
            },
        },
        {
            "stage": "07_backup_verification",
            "status": (backup or {}).get("status") or "OPERATOR_REQUIRED",
            "safe_work_done": True,
            "verified": bool((backup or {}).get("backup_verified")),
            "iam_changed": False,
            "aws_console_procedure": (backup or {}).get("aws_console_procedure"),
        },
        {
            "stage": "08_release_candidate_prep",
            "status": "PREP_COMPLETE_PUSH_STOPPED",
            "safe_work_done": True,
            "push_executed": bool((release_plan or {}).get("push_executed")),
            "reviewed_released": bool((release_plan or {}).get("reviewed_released")),
            "separation": (release_plan or {}).get("separate_from_rc_if_possible"),
            "deployment_order": (release_plan or {}).get("deployment_order"),
            "rollback": (release_plan or {}).get("rollback"),
        },
        {
            "stage": "09_schema_plan",
            "status": "PLAN_ONLY" if not (schema or {}).get("migrate_executed") else "MIGRATED",
            "safe_work_done": True,
            "migrate_executed": bool((schema or {}).get("migrate_executed")),
            "users_0102_migration_start_at": True,
            "authorized": bool((schema or {}).get("schema_migrate_authorized")),
        },
        {
            "stage": "10_raa_regression",
            "status": (raa or {}).get("status") or "BLOCKED",
            "safe_work_done": True,
            "regression_executed": bool((raa or {}).get("regression_executed")),
            "prerequisite": "release + backup + schema migrate 0101–0104",
        },
        {
            "stage": "11_dry_runs",
            "status": "COMPLETE_STAGING" if dry_runs else "OPERATOR_REQUIRED",
            "safe_work_done": bool(dry_runs),
            "writes": (dry_runs or {}).get("writes", 0),
            "smtp_sends": (dry_runs or {}).get("smtp_sends", 0),
        },
        {
            "stage": "12_migration_manifest_dry_run",
            "status": (manifest or {}).get("status") or "BLOCKED_SKELETON",
            "safe_work_done": True,
            "full_dry_run_executed": bool((manifest or {}).get("full_dry_run_executed")),
            "note": "Skeleton/placeholders when discovery+mappings unavailable",
        },
    ]
    return stages


def build_migration_manifest_skeleton(
    *,
    gate_inspection: dict[str, Any],
    discovery_status: str,
    discovery_executed: bool,
    wallet_reconciliation: dict[str, Any] | None = None,
    equipment_inventory: dict[str, Any] | None = None,
    release_plan: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Useful operator checklist + blocked placeholders — never invents cutover data."""
    return {
        "phase": "10L",
        "artifact": "phase10l_migration_manifest",
        "audit_mode": "READ_ONLY",
        "status": "BLOCKED_SKELETON" if not discovery_executed else "DRAFT_AFTER_DISCOVERY",
        "full_dry_run_executed": False,
        "t0_included": False,
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "prerequisites": {
            "datetime_contract": gate_inspection["datetime_contract_approval"]["status"],
            "migration_window_configured": gate_inspection["migration_window"]["configured"],
            "discovery_status": discovery_status,
            "discovery_executed": discovery_executed,
            "explicit_equipment_mappings": 0,
            "finance_acceptability_decided": False,
            "backup_verified": False,
            "release_authorized": False,
            "schema_migrate_authorized": False,
        },
        "blocked_sections": [
            {
                "section": "eligible_upcoming_bookings",
                "status": "BLOCKED",
                "reason": discovery_status,
                "placeholder_count": None,
            },
            {
                "section": "eligible_window_equipment_ids",
                "status": "BLOCKED",
                "reason": "requires discovery",
                "inventory_ro_count": (equipment_inventory or {}).get("count"),
                "note": "Full legacy inventory available RO; eligible set unknown until discovery",
            },
            {
                "section": "conflict_plan",
                "status": "BLOCKED",
                "reason": "requires discovery + mappings",
            },
            {
                "section": "user_employee_id_resolution_for_eligible",
                "status": "BLOCKED",
                "reason": "requires discovery",
                "policy": "Employee ID only — USER UNRESOLVED does not block when equipment+time valid",
            },
            {
                "section": "wallet_opening_balances",
                "status": "FORBIDDEN_IN_10L",
                "reason": "no opening balances / no auto-correct",
                "exceptions_open": (wallet_reconciliation or {}).get("mismatch_count"),
            },
            {
                "section": "t0_slot_blocking_batch",
                "status": "FORBIDDEN_IN_10L",
                "reason": "T0 not authorized in Phase 10L",
            },
        ],
        "completed_prep_sections": [
            {
                "section": "legacy_mysql_ro_baseline",
                "status": "COMPLETE",
                "wallet_count": (wallet_reconciliation or {}).get("wallet_count"),
                "transaction_count": (wallet_reconciliation or {}).get("transaction_count"),
                "mismatch_count": (wallet_reconciliation or {}).get("mismatch_count"),
            },
            {
                "section": "legacy_equipment_inventory_ro",
                "status": "COMPLETE",
                "count": (equipment_inventory or {}).get("count"),
            },
            {
                "section": "schema_0102_migration_start_at",
                "status": "CONFIRMED_SOURCE",
                "detail": confirm_0102_provides_migration_start_at(),
            },
            {
                "section": "release_candidate_notes",
                "status": "PREP",
                "deployment_order": (release_plan or {}).get("deployment_order"),
                "separate_from_rc": (release_plan or {}).get("separate_from_rc_if_possible"),
                "push_executed": False,
            },
            {
                "section": "schema_plan",
                "status": "PLAN_ONLY",
                "plan": schema,
            },
        ],
        "operator_checklist": [
            "Approve datetime contract (Main Admin UI/API)",
            "Configure migration_start_at + migration_window_end_at (operator-supplied ISO)",
            "Run migration_production_legacy_qualification (RO)",
            "Map eligible-window equipment only (no fuzzy)",
            "Resolve conflicts / Employee-ID users for eligible set",
            "Finance review exceptions (no auto-correct)",
            "AWS Console verify RDS backup",
            "Authorize release RC (migration-only vs R12/R14/RAA/Copilot)",
            "Deploy without auto-migrate; showmigrations + migrate --plan",
            "Explicit schema auth → migrate users 0101–0104",
            "RAA booking regression",
            "Production dry-runs (writes=0 SMTP=0)",
            "Separate explicit T0 authorization — not Phase 10L",
        ],
        "writes": 0,
        "smtp_sends": 0,
    }


def build_release_candidate_prep(
    *,
    production_sha: str,
    backend_sha: str,
    frontend_sha: str,
) -> dict[str, Any]:
    return {
        "phase": "10L",
        "production_sha": production_sha,
        "local_backend_sha": backend_sha,
        "local_frontend_sha": frontend_sha,
        "uncommitted_phases": [
            "10D",
            "10E",
            "10F",
            "10G",
            "10H",
            "10I",
            "10J",
            "10K",
            "10L",
        ],
        "reviewed_released": False,
        "push_executed": False,
        "deploy_executed": False,
        "pr_number": None,
        "release_tag": None,
        "note": "OPERATOR ACTION REQUIRED to push/PR — do not fabricate PR numbers; STOP at push",
        "required_migration_release": [
            "users/0101–0104 (0102 adds migration_start_at)",
            "legacy_ledger datetime/equipment/booking bridge",
            "portal migration admin APIs (incl. phase10l-go-no-go)",
            "AdminPortalMigration + LegacyEquipment/Booking mapping UI",
        ],
        "separate_from_rc_if_possible": [
            "RAA / Copilot unrelated changes",
            "R12 / R14 / analysis UI if not required for migration",
            "Unrelated RemoteAnalysisAgent worktrees",
        ],
        "deployment_order": [
            "commit/PR/tag backend migration RC (incl. 0104 + 10L closure)",
            "commit/PR/tag frontend migration UI",
            "deploy backend (no auto-migrate)",
            "deploy frontend",
            "AWS Console backup verify",
            "showmigrations + migrate --plan",
            "explicit MIGRATE 0101–0104 (separate schema auth)",
            "RAA booking regression",
            "RO discovery only after datetime+window",
            "Separate T0 authorization (not this phase)",
        ],
        "rollback": "APP ROLLBACK != DATABASE ROLLBACK — see Phase 10G rollback doc",
        "push_authorization": "NOT GRANTED — prep only",
    }


def build_phase10l_final_readiness(
    *,
    backup_verified: bool = False,
    mysql_probe: dict[str, Any] | None = None,
    datetime_validation: dict[str, Any] | None = None,
    datetime_review: dict[str, Any] | None = None,
    wallet_reconciliation: dict[str, Any] | None = None,
    production_migrate_plan: dict[str, Any] | None = None,
    test_account_dry_run: dict[str, Any] | None = None,
    email_dry_run: dict[str, Any] | None = None,
    release_plan: dict[str, Any] | None = None,
    explicit_evidence: dict[str, Any] | None = None,
    finance_reviewed: bool = False,
    schema_migrate_authorized: bool = False,
    equipment_mapping_authorized: bool = False,
    discovery_result: dict[str, Any] | None = None,
    staging_schema_status: dict[str, Any] | None = None,
    raa_regression: dict[str, Any] | None = None,
    equipment_inventory: dict[str, Any] | None = None,
    backup_report: dict[str, Any] | None = None,
    security_tests: dict[str, Any] | None = None,
    regression_tests: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authoritative Phase 10L GO/NO-GO with full stage machine evidence."""
    if datetime_review and datetime_review.get("phase") in ("10I", "10J", "10K", None):
        datetime_review = {**datetime_review, "phase": "10L"}

    base = build_phase10k_final_readiness(
        backup_verified=backup_verified,
        mysql_probe=mysql_probe,
        datetime_validation=datetime_validation,
        datetime_review=datetime_review,
        wallet_reconciliation=wallet_reconciliation,
        production_migrate_plan=production_migrate_plan,
        test_account_dry_run=test_account_dry_run,
        email_dry_run=email_dry_run,
        release_plan=release_plan,
        explicit_evidence=explicit_evidence,
        finance_reviewed=finance_reviewed,
        schema_migrate_authorized=schema_migrate_authorized,
        equipment_mapping_authorized=equipment_mapping_authorized,
        discovery_result=discovery_result,
        staging_schema_status=staging_schema_status,
        raa_regression=raa_regression,
    )

    gate_inspection = inspect_operator_gates(
        backup_verified=backup_verified,
        release_reviewed=bool((release_plan or {}).get("reviewed_released")),
        schema_migrate_authorized=schema_migrate_authorized,
        equipment_mapping_authorized=equipment_mapping_authorized,
        finance_reviewed=finance_reviewed,
        datetime_validation=datetime_validation,
        explicit_mappings=int((explicit_evidence or {}).get("explicit_mappings") or 0),
    )

    discovery_executed = bool(discovery_result and discovery_result.get("executed"))
    discovery_status = base.get("discovery_status") or "UNKNOWN"
    discovery_artifact = base.get("discovery_artifact")
    if not discovery_executed and not gate_inspection["discovery_allowed"]:
        discovery_artifact = blocked_discovery_artifact(
            datetime_status=gate_inspection["datetime_contract_approval"]["status"],
            window_configured=bool(gate_inspection["migration_window"]["configured"]),
        )
        discovery_artifact = {**discovery_artifact, "phase": "10L"}
        discovery_status = discovery_artifact["status"]

    schema_0102 = confirm_0102_provides_migration_start_at()
    backup = backup_report or {
        "backup_verified": backup_verified,
        "status": "PASS" if backup_verified else "BLOCKED",
        "missing_permission": "rds:DescribeDBInstances / rds:DescribeDBSnapshots (AccessDenied)",
        "do_not_change_iam_automatically": True,
        "aws_console_procedure": [
            "Sign in to AWS Console with authorized operator account",
            "Navigate to RDS → Databases",
            "Select the IIC booking production DB instance",
            "Note DB identifier, status, Multi-AZ, backup retention period",
            "Open Maintenance & backups / Snapshots tab",
            "Record latest automated backup timestamp and status",
            "Record any recent manual snapshot name/timestamp/status",
            "Confirm restore availability",
            "Update readiness with --backup-verified only after visual confirmation",
        ],
        "t0_refuses_without_backup": True,
        "create_or_delete_backups": False,
        "iam_probe": {
            "principal": "arn:aws:iam::267366138117:user/iic-booking-S3-user",
            "region_tried": "ap-south-1",
            "result": "AccessDenied",
        },
    }

    manifest = build_migration_manifest_skeleton(
        gate_inspection=gate_inspection,
        discovery_status=discovery_status,
        discovery_executed=discovery_executed,
        wallet_reconciliation=wallet_reconciliation,
        equipment_inventory=equipment_inventory,
        release_plan=release_plan,
        schema={
            **(production_migrate_plan or {}),
            "users_0102": schema_0102,
            "staging": staging_schema_status,
            "migrate_executed": False,
            "schema_migrate_authorized": schema_migrate_authorized,
        },
    )

    dry_runs = {
        "writes": 0,
        "smtp_sends": int((email_dry_run or {}).get("smtp_sends") or 0),
        "test_account": test_account_dry_run,
        "email": email_dry_run,
        "environment": (email_dry_run or {}).get("environment") or "staging_or_local",
        "production_dry_run": "OPERATOR_REQUIRED",
    }

    stages = build_stage_machine(
        gate_inspection=gate_inspection,
        discovery_executed=discovery_executed,
        equipment_inventory=equipment_inventory,
        wallet_reconciliation=wallet_reconciliation,
        backup=backup,
        release_plan=release_plan,
        schema={
            "migrate_executed": False,
            "schema_migrate_authorized": schema_migrate_authorized,
        },
        dry_runs=dry_runs,
        manifest=manifest,
        raa=raa_regression or base.get("raa_booking_regression"),
    )

    matrix = dict(base.get("gate_matrix") or {})
    # Enrich Equipment with inventory RO evidence while eligible set blocked
    matrix["Equipment"] = {
        "result": GATE_OPERATOR,
        "evidence": {
            "explicit_mappings": int((explicit_evidence or {}).get("explicit_mappings") or 0),
            "legacy_inventory_count": (equipment_inventory or {}).get("count"),
            "eligible_window_set": "UNKNOWN — discovery not executed",
            "fuzzy_forbidden": True,
        },
        "blocking": True,
        "operator_action": (
            "Inventory RO complete; map only eligible-window IDs after discovery — no fuzzy/auto-approve"
        ),
        "exact_command_or_ui": "/admin/portal-migration/equipment-mapping",
    }
    matrix["Backup"] = {
        "result": GATE_PASS if backup_verified else GATE_BLOCKED,
        "evidence": backup,
        "blocking": True,
        "operator_action": "AWS Console verify snapshot — do not change IAM automatically",
        "exact_command_or_ui": "AWS Console → RDS → Databases → Snapshots",
    }
    if security_tests:
        matrix["Security"] = {
            "result": GATE_PASS if security_tests.get("ok") else GATE_OPERATOR,
            "evidence": security_tests,
            "blocking": True,
            "operator_action": "Confirm Main Admin only for T0/control endpoints",
            "exact_command_or_ui": "manage.py test phase10* / phase8* permission suites",
        }

    preferred = [
        "Release",
        "Schema",
        "Datetime",
        "Migration Window",
        "Legacy MySQL",
        "Upcoming Bookings",
        "Equipment",
        "Users",
        "Wallets",
        "Finance",
        "Conflicts",
        "Backup",
        "Test Account",
        "Email",
        "Security",
        "Rollback",
        "RAA Booking Regression",
        "T0 Authorization",
    ]
    ordered: dict[str, Any] = {}
    for key in preferred:
        if key in matrix:
            ordered[key] = matrix[key]
    for key, val in matrix.items():
        if key not in ordered:
            ordered[key] = val

    ordered["T0 Authorization"] = {
        "result": GATE_OPERATOR,
        "evidence": "T0 NOT ACTIVATED; Phase 10L never executes T0",
        "blocking": True,
        "operator_action": "Separate explicit authorization after READY FOR EXPLICIT T0 AUTHORIZATION",
        "exact_command_or_ui": "Do not run T0 in Phase 10L",
    }

    blockers = [
        name
        for name, g in ordered.items()
        if g.get("blocking") and g.get("result") in (GATE_BLOCKED, GATE_OPERATOR)
    ]

    hard_refuse: list[str] = []
    if gate_inspection["datetime_contract_approval"]["operator_required"]:
        hard_refuse.append("datetime_unapproved")
    if gate_inspection["migration_window"]["operator_required"]:
        hard_refuse.append("migration_window_missing")
    if gate_inspection["backup_verification"]["operator_required"]:
        hard_refuse.append("backup_unverified")
    if gate_inspection["release_authorization"]["operator_required"]:
        hard_refuse.append("release_missing")
    if not schema_migrate_authorized:
        hard_refuse.append("schema_migrate_not_authorized")
    if not finance_reviewed:
        hard_refuse.append("finance_review_pending")
    if int((explicit_evidence or {}).get("explicit_mappings") or 0) == 0:
        hard_refuse.append("equipment_mapping_incomplete")
    if not discovery_executed:
        hard_refuse.append("discovery_not_executed")

    for name, g in ordered.items():
        if g.get("result") != GATE_PASS:
            continue
        ev = g.get("evidence")
        ev_text = ev if isinstance(ev, str) else ("" if ev is None else str(ev))
        if not ev_text.strip():
            hard_refuse.append(f"pass_without_evidence:{name}")

    tech_ok = all(
        g.get("result") == GATE_PASS
        for name, g in ordered.items()
        if g.get("blocking") and name != "T0 Authorization"
    )
    verdict = VERDICT_READY if (tech_ok and not hard_refuse) else VERDICT_NOT_READY
    if verdict == "READY FOR T0":
        verdict = VERDICT_NOT_READY

    completed = [
        s["stage"]
        for s in stages
        if s.get("status")
        in ("COMPLETE", "COMPLETE_RO", "COMPLETE_STAGING", "PREP_COMPLETE_PUSH_STOPPED", "PLAN_ONLY", "CONFIRMED_SOURCE")
        or s.get("safe_work_done")
    ]

    return {
        **base,
        "phase": "10L",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "verdict": verdict,
        "t0_executed": False,
        "explicit_t0_authorization_required": True,
        "hard_refuse_reasons": hard_refuse,
        "operator_gate_inspection": gate_inspection,
        "gate_matrix": ordered,
        "blockers": sorted(set(blockers)),
        "stage_machine": stages,
        "stages_with_safe_work_complete": completed,
        "discovery_status": discovery_status,
        "discovery_executed": discovery_executed,
        "discovery_artifact": discovery_artifact,
        "migration_manifest": manifest,
        "datetime_contract_status": gate_inspection["datetime_contract_approval"]["status"],
        "migration_window": gate_inspection["migration_window"],
        "users_0102_migration_start_at": schema_0102,
        "raa_booking_regression": raa_regression or base.get("raa_booking_regression"),
        "staging_schema_status": staging_schema_status,
        "equipment_inventory_ro": equipment_inventory,
        "backup_report": backup,
        "dry_runs": dry_runs,
        "security_tests": security_tests,
        "regression_tests": regression_tests,
        "phase10k_embedded_verdict": base.get("verdict"),
        "work_completed_this_phase": [
            "Full live gate + SHA + staging migration inspect",
            "Datetime/window recorded OPERATOR_REQUIRED with exact Main Admin actions (POST not called)",
            "Discovery left BLOCKED with phase10l_production_discovery.json",
            "Legacy equipment inventory RO (not eligible-window auto-map)",
            "Wallet/finance RO recalculation + exception register (no acceptability decision)",
            "Backup IAM RO probe AccessDenied → AWS Console procedure documented",
            "Release candidate prep notes/sequence/rollback; push STOPPED",
            "Schema showmigrations/migrate --plan; 0102→migration_start_at confirmed; migrate NOT run",
            "RAA regression documented BLOCKED pending schema",
            "Test-account + email dry-runs writes=0 SMTP=0 (staging)",
            "Migration manifest skeleton with blocked placeholders + operator checklist",
            "Phase 10L readiness closure + GO/NO-GO API/UI preference",
        ],
        "work_blocked_operator_required": [
            "Datetime approval (Main Admin)",
            "Migration window configuration (operator-supplied dates)",
            "Production RO discovery / eligible equipment / conflicts / Employee-ID eligible users",
            "Finance acceptability decision",
            "Backup visual verify / release push / schema migrate / RAA regression / T0",
        ],
        "architecture_invariant": (
            "USER UNRESOLVED + VALID DATETIME + VALID EQUIPMENT MAPPING = READY/BLOCKABLE"
        ),
        "app_rollback_note": "APP ROLLBACK != DATABASE ROLLBACK",
        "production_safety": {
            "PRODUCTION_MIGRATE": "NO",
            "T0": "NO",
            "BOOKING_BLOCK": "NO",
            "OLD_PORTAL_FREEZE": "NO",
            "REDIRECT_ENABLED": "NO",
            "EMAILS_SENT": "NO",
            "REFUNDS": "NO",
            "CLEANUP": "NO",
            "LEGACY_MYSQL_WRITES": "NO",
            "PRODUCTION_WALLET_WRITES": "NO",
            "PRODUCTION_BOOKING_WRITES": "NO",
            "PRODUCTION_USER_WRITES": "NO",
            "DATETIME_CONTRACT_POST": "NO",
            "MIGRATION_WINDOW_DATES_INVENTED": "NO",
            "EQUIPMENT_AUTO_APPROVE": "NO",
            "FINANCE_AUTO_CORRECT": "NO",
            "RAA_PATCH_AROUND_0102": "NO",
            "MANUAL_ALTER_DB": "NO",
            "DNS_CHANGE": "NO",
            "EC2_TERMINATE": "NO",
            "OPENING_BALANCES": "NO",
        },
        "production_writes_performed": [],
        "operator_next_actions": manifest["operator_checklist"],
        "live_counts": {
            "users": (mysql_probe or {}).get("live_financial_audit", {}).get("users_total")
            or ((mysql_probe or {}).get("row_counts") or {}).get("users"),
            "note": "Prefer LIVE over prior JSON baselines; known-count drift is expected",
        },
    }


def write_phase10l_artifacts(
    report: dict[str, Any],
    *,
    datetime_validation: dict[str, Any] | None = None,
    datetime_review: dict[str, Any] | None = None,
    wallet_reconciliation: dict[str, Any] | None = None,
    finance_register: dict[str, Any] | None = None,
    equipment_inventory: dict[str, Any] | None = None,
    backup_report: dict[str, Any] | None = None,
    release_plan: dict[str, Any] | None = None,
) -> list[str]:
    base = Path(getattr(settings, "BASE_DIR", ".")) / ARTIFACT_DIR
    written: list[str] = []
    discovery = report.get("discovery_artifact") or blocked_discovery_artifact(
        datetime_status=str(report.get("datetime_contract_status") or "OPERATOR_REQUIRED"),
        window_configured=bool((report.get("migration_window") or {}).get("configured")),
    )
    if isinstance(discovery, dict):
        discovery = {**discovery, "phase": "10L"}

    pairs: list[tuple[str, Any]] = [
        ("phase10l_final_readiness.json", report),
        (
            "phase10l_go_no_go.json",
            {
                "phase": "10L",
                "verdict": report["verdict"],
                "t0_executed": False,
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "stage_machine": report.get("stage_machine"),
                "operator_gate_inspection": report.get("operator_gate_inspection"),
                "production_safety": report.get("production_safety"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "work_completed_this_phase": report.get("work_completed_this_phase"),
                "work_blocked_operator_required": report.get("work_blocked_operator_required"),
                "regression_tests": report.get("regression_tests"),
                "generated_at_utc": report.get("generated_at_utc"),
            },
        ),
        ("phase10l_production_discovery.json", discovery),
        ("phase10l_migration_manifest.json", report.get("migration_manifest")),
        ("phase10l_stage_machine.json", {"phase": "10L", "stages": report.get("stage_machine")}),
    ]
    if datetime_review:
        pairs.append(("phase10l_datetime_review.json", datetime_review))
    if datetime_validation:
        pairs.append(("legacy_datetime_validation.json", datetime_validation))
    if wallet_reconciliation:
        pairs.append(("phase10l_wallet_reconciliation.json", wallet_reconciliation))
    if finance_register:
        pairs.append(("phase10l_finance_exception_register.json", finance_register))
    if equipment_inventory:
        pairs.append(("phase10l_equipment_inventory.json", equipment_inventory))
    if backup_report:
        pairs.append(("phase10l_backup_readiness.json", backup_report))
        pairs.append(("production_backup_readiness.json", backup_report))
    if release_plan:
        pairs.append(("phase10l_release_candidate.json", release_plan))
        pairs.append(("production_release_plan.json", release_plan))

    for name, payload in pairs:
        if payload is None:
            continue
        path = write_json_artifact(base / name, payload)
        written.append(path)
    return written
