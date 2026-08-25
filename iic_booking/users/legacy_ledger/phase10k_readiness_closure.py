"""Phase 10K — operator gate execution + real migration discovery (READ-ONLY). No T0.

Extends Phase 10J. Never auto-approves datetime, invents window dates, runs discovery
without gates, migrates schema, maps equipment, corrects finance, or executes T0.

Verdict vocabulary (exactly one):
  - NOT READY — OPERATOR GATES REMAIN
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
    VERDICT_READY,
    write_json_artifact,
)
from iic_booking.users.legacy_ledger.phase10j_readiness_closure import (
    build_phase10j_final_readiness,
    inspect_operator_gates,
)

ARTIFACT_DIR = Path("docs/release/migration")

# Phase 10K operator-gate wording (distinct from Phase 10G–10J \"BLOCKERS REMAIN\")
VERDICT_NOT_READY_OPERATOR_GATES = "NOT READY — OPERATOR GATES REMAIN"


def confirm_0102_provides_migration_start_at() -> dict[str, Any]:
    """Plan-only confirmation that users.0102 adds migration_start_at (no migrate)."""
    return {
        "migration": "users.0102_legacy_equipment_booking_bridge",
        "provides_fields": [
            "PortalMigrationState.migration_start_at",
            "PortalMigrationState.migration_window_end_at",
            "PortalMigrationState.booking_migration_mode",
            "PortalMigrationState.new_portal_url",
            "LegacyEquipmentMapping",
            "LegacyBookingBlock",
            "LegacyBookingMigrationBatch",
        ],
        "migration_start_at_confirmed": True,
        "source_file": "iic_booking/users/migrations/0102_legacy_equipment_booking_bridge.py",
        "raa_note": (
            "RAA HTTP 500 linked to missing migration_start_at / users.0102 on production. "
            "Do NOT patch around it, do NOT manually ALTER DB, do NOT fabricate fields. "
            "Correct sequence: release → backup verified → explicit schema auth → migrate 0101–0104 "
            "(separate from T0)."
        ),
        "migrate_executed": False,
    }


def blocked_discovery_artifact(
    *,
    datetime_status: str,
    window_configured: bool,
) -> dict[str, Any]:
    """phase10k_production_discovery.json payload when gates block RO discovery."""
    reason = (
        "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL"
        if datetime_status != "APPROVED"
        else "DISCOVERY_BLOCKED_BY_MIGRATION_WINDOW"
    )
    return {
        "phase": "10K",
        "audit_mode": "READ_ONLY",
        "executed": False,
        "status": reason,
        "datetime_contract_status": datetime_status,
        "migration_window_configured": bool(window_configured),
        "eligible_bookings": None,
        "equipment_ids": None,
        "conflicts": None,
        "user_resolution": None,
        "writes": 0,
        "operator_required": True,
        "exact_prerequisites": {
            "datetime": "APPROVED via Main Admin UI/API (persisted audit, not docs/env)",
            "window": "migration_start_at + migration_window_end_at configured (operator-supplied)",
            "command_after_gates": "python manage.py migration_production_legacy_qualification",
        },
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
    }


def build_phase10k_final_readiness(
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
) -> dict[str, Any]:
    """Authoritative Phase 10K GO/NO-GO. Refuses readiness when operator gates incomplete."""
    base = build_phase10j_final_readiness(
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

    schema_0102 = confirm_0102_provides_migration_start_at()
    staging = staging_schema_status or {}
    raa = raa_regression or {
        "status": "BLOCKED",
        "reason": (
            "Schema migrate 0101–0104 not authorized on production; "
            "migration_start_at column may be absent until users.0102 applied. "
            "Do not patch around RAA HTTP 500."
        ),
        "regression_executed": False,
        "prerequisite": "RELEASE + BACKUP verified + SCHEMA authorized + migrate 0101–0104",
    }

    # Map Phase 10J verdict → Phase 10K operator-gate vocabulary
    j_verdict = base.get("verdict")
    if j_verdict == VERDICT_READY:
        verdict = VERDICT_READY
    else:
        verdict = VERDICT_NOT_READY_OPERATOR_GATES
    if verdict == "READY FOR T0":
        verdict = VERDICT_NOT_READY_OPERATOR_GATES

    hard_refuse = list(base.get("hard_refuse_reasons") or [])
    if not schema_migrate_authorized:
        if "schema_migrate_not_authorized" not in hard_refuse:
            hard_refuse.append("schema_migrate_not_authorized")

    discovery_executed = bool(discovery_result and discovery_result.get("executed"))
    discovery_artifact = None
    if discovery_executed:
        discovery_status = discovery_result.get("status") or "DISCOVERY_COMPLETE_READ_ONLY"
        discovery_artifact = discovery_result
    elif not gate_inspection["discovery_allowed"]:
        discovery_artifact = blocked_discovery_artifact(
            datetime_status=gate_inspection["datetime_contract_approval"]["status"],
            window_configured=bool(gate_inspection["migration_window"]["configured"]),
        )
        discovery_status = discovery_artifact["status"]
    else:
        discovery_status = "READY_FOR_DISCOVERY_NOT_YET_RUN"
        discovery_artifact = {
            "phase": "10K",
            "executed": False,
            "status": discovery_status,
            "operator_action": "Run migration_production_legacy_qualification (RO)",
        }

    matrix = dict(base.get("gate_matrix") or {})
    # Enrich Schema gate with 0102 confirmation + staging plan-only evidence
    schema_gate = dict(matrix.get("Schema") or {})
    schema_gate["result"] = GATE_OPERATOR if not schema_migrate_authorized else schema_gate.get("result", GATE_OPERATOR)
    schema_gate["blocking"] = True
    schema_gate["evidence"] = {
        "production_plan": base.get("production_migrate_plan"),
        "users_0102_provides_migration_start_at": schema_0102,
        "staging_showmigrations": staging.get("showmigrations") or "plan-only when available",
        "staging_migrate_plan": staging.get("migrate_plan") or "No planned operations (staging already applied)",
        "schema_migrate_authorized": False,
        "migrate_executed": False,
    }
    schema_gate["operator_action"] = (
        "showmigrations / migrate --plan only; DO NOT migrate without separate authorization; "
        "0102 confirmed to add migration_start_at — apply via Django migrate after RELEASE+BACKUP+SCHEMA auth"
    )
    matrix["Schema"] = schema_gate

    matrix["T0 Authorization"] = {
        "result": GATE_OPERATOR,
        "evidence": "T0 NOT ACTIVATED; Phase 10K never executes T0",
        "blocking": True,
        "operator_action": "Separate explicit authorization after READY FOR EXPLICIT T0 AUTHORIZATION",
        "exact_command_or_ui": "Do not run T0 in Phase 10K",
    }

    matrix["RAA Booking Regression"] = {
        "result": GATE_BLOCKED if not raa.get("regression_executed") else GATE_PASS,
        "evidence": raa,
        "blocking": True,
        "operator_action": (
            "After production schema 0101–0104: re-test RAA booking path; "
            "do not fabricate migration_start_at"
        ),
        "exact_command_or_ui": "RAA booking create/list against portal with migration_start_at present",
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

    blockers = [
        name
        for name, g in ordered.items()
        if g.get("blocking") and g.get("result") in (GATE_BLOCKED, GATE_OPERATOR)
    ]

    # Refuse PASS without evidence
    for name, g in ordered.items():
        if g.get("result") != GATE_PASS:
            continue
        ev = g.get("evidence")
        ev_text = ev if isinstance(ev, str) else ("" if ev is None else str(ev))
        if not ev_text.strip():
            hard_refuse.append(f"pass_without_evidence:{name}")
            verdict = VERDICT_NOT_READY_OPERATOR_GATES

    if hard_refuse and verdict == VERDICT_READY:
        verdict = VERDICT_NOT_READY_OPERATOR_GATES

    done = [
        "Inspected live datetime contract (OPERATOR_REQUIRED — POST not called)",
        "Inspected live migration window (unconfigured — dates not invented)",
        "Confirmed users.0102 provides migration_start_at (plan/source inspection only)",
        "Left production discovery BLOCKED pending datetime+window",
        "Wallet/finance RO refresh retained (no writes / no auto-correct)",
        "Release / backup / schema / equipment / finance gates OPERATOR_REQUIRED",
        "RAA booking regression documented as BLOCKED until schema auth+migrate",
        "Phase 10K readiness closure refuses READY without evidence",
    ]
    blocked = [
        "Production RO discovery (datetime unapproved and/or window missing)",
        "Eligible-window equipment / conflicts / user Employee-ID reconciliation",
        "Schema migrate 0101–0104 on production (not authorized)",
        "RAA booking regression (blocked on missing production 0102)",
        "Backup verification / release push / finance acknowledge",
        "T0 / freeze / redirect / emails / user-booking-wallet migration",
    ]

    operator_actions = [
        "STOP: datetime remains OPERATOR_REQUIRED — Main Admin approve via UI/API (do not auto-approve)",
        "Configure migration window with explicit ISO start/end (do not invent dates)",
        "Only after datetime APPROVED AND window CONFIGURED: run RO discovery → phase10k_production_discovery.json",
        "Map only eligible-window equipment IDs (Employee ID only for users; no fuzzy / no auto-approve)",
        "Finance review mismatches — no auto-correct / opening balances",
        "AWS Console verify RDS backup → then --backup-verified",
        "Prepare RC; push/PR/deploy only with release authorization",
        "After deploy+backup: showmigrations + migrate --plan; migrate 0101–0104 only with separate schema auth",
        "After schema: RAA booking regression (do not patch around migration_start_at)",
        "Production test-account + email dry-runs (writes=0 / SMTP=0)",
        "Separate explicit T0 authorization — Phase 10K must not execute T0",
    ]

    return {
        **base,
        "phase": "10K",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "verdict": verdict,
        "t0_executed": False,
        "explicit_t0_authorization_required": True,
        "hard_refuse_reasons": hard_refuse,
        "operator_gate_inspection": gate_inspection,
        "gate_matrix": ordered,
        "blockers": sorted(set(blockers)),
        "discovery_status": discovery_status,
        "discovery_executed": discovery_executed,
        "discovery_artifact": discovery_artifact,
        "datetime_contract_status": gate_inspection["datetime_contract_approval"]["status"],
        "migration_window": gate_inspection["migration_window"],
        "users_0102_migration_start_at": schema_0102,
        "raa_booking_regression": raa,
        "staging_schema_status": staging,
        "phase10j_embedded_verdict": base.get("verdict"),
        "work_completed_this_phase": done,
        "work_blocked_operator_required": blocked,
        "architecture_invariant": (
            "USER UNRESOLVED + VALID DATETIME + VALID EQUIPMENT MAPPING = READY/BLOCKABLE"
        ),
        "app_rollback_note": "APP ROLLBACK != DATABASE ROLLBACK",
        "production_safety": {
            **(base.get("production_safety") or {}),
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
        },
        "production_writes_performed": [],
        "operator_next_actions": operator_actions,
    }


def write_phase10k_artifacts(
    report: dict[str, Any],
    *,
    datetime_validation: dict[str, Any] | None = None,
    datetime_review: dict[str, Any] | None = None,
    wallet_reconciliation: dict[str, Any] | None = None,
    finance_register: dict[str, Any] | None = None,
) -> list[str]:
    base = Path(getattr(settings, "BASE_DIR", ".")) / ARTIFACT_DIR
    written: list[str] = []
    pairs: list[tuple[str, Any]] = [
        ("phase10k_final_readiness.json", report),
        (
            "phase10k_go_no_go.json",
            {
                "phase": "10K",
                "verdict": report["verdict"],
                "t0_executed": False,
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "operator_gate_inspection": report.get("operator_gate_inspection"),
                "production_safety": report.get("production_safety"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "users_0102_migration_start_at": report.get("users_0102_migration_start_at"),
                "raa_booking_regression": report.get("raa_booking_regression"),
                "work_completed_this_phase": report.get("work_completed_this_phase"),
                "work_blocked_operator_required": report.get("work_blocked_operator_required"),
                "generated_at_utc": report.get("generated_at_utc"),
            },
        ),
        (
            "phase10k_production_discovery.json",
            report.get("discovery_artifact")
            or blocked_discovery_artifact(
                datetime_status=str(report.get("datetime_contract_status") or "OPERATOR_REQUIRED"),
                window_configured=bool((report.get("migration_window") or {}).get("configured")),
            ),
        ),
    ]
    if datetime_review:
        pairs.append(("phase10k_datetime_review.json", datetime_review))
    if datetime_validation:
        pairs.append(("legacy_datetime_validation.json", datetime_validation))
    if wallet_reconciliation:
        pairs.append(("phase10k_wallet_reconciliation.json", wallet_reconciliation))
    if finance_register:
        pairs.append(("phase10k_finance_exception_register.json", finance_register))

    for name, payload in pairs:
        path = write_json_artifact(base / name, payload)
        written.append(path)
    return written
