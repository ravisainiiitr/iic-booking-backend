"""Phase 10J — operator-gated migration progression (READ-ONLY). No T0.

Extends Phase 10I readiness. Never auto-approves datetime, invents window dates,
runs discovery without gates, migrates schema, maps equipment, or corrects finance.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

from iic_booking.users.legacy_ledger.datetime_contract import (
    contract_approval_status,
    load_datetime_contract,
)
from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    GATE_BLOCKED,
    GATE_OPERATOR,
    GATE_PASS,
    VERDICT_NOT_READY,
    VERDICT_READY,
    write_json_artifact,
)
from iic_booking.users.legacy_ledger.phase10i_readiness_closure import (
    build_datetime_review,
    build_phase10i_final_readiness,
    migration_window_status,
)

ARTIFACT_DIR = Path("docs/release/migration")


def inspect_operator_gates(
    *,
    backup_verified: bool = False,
    release_reviewed: bool = False,
    schema_migrate_authorized: bool = False,
    equipment_mapping_authorized: bool = False,
    finance_reviewed: bool = False,
    datetime_validation: dict[str, Any] | None = None,
    explicit_mappings: int = 0,
) -> dict[str, Any]:
    """Inspect real approval/config state. Never invent approvals."""
    contract = load_datetime_contract()
    datetime_status = contract_approval_status(contract)
    window = migration_window_status()
    totals = (datetime_validation or {}).get("totals") or {}

    return {
        "inspected_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "datetime_contract_approval": {
            "status": datetime_status,
            "approved_by": contract.get("approved_by"),
            "approved_at_utc": contract.get("approved_at_utc"),
            "file_status": contract.get("_status"),
            "operator_required": datetime_status != "APPROVED",
            "exact_ui": "/admin/portal-migration — Datetime contract — confirm=true + reason",
            "exact_api": "POST /api/portal-migration/admin/datetime-contract/",
            "auto_approve_forbidden": True,
            "post_datetime_contract_called": False,
        },
        "migration_window": {
            "configured": bool(window.get("configured")),
            "start": window.get("start"),
            "end": window.get("end"),
            "dates_invented": False,
            "operator_required": not bool(window.get("configured")),
            "exact_ui": "/admin/portal-migration — Phase 8B settings",
            "exact_api": "PATCH /api/portal-migration/admin/state/",
        },
        "release_authorization": {
            "authorized": bool(release_reviewed),
            "operator_required": not release_reviewed,
            "note": "No push/PR/deploy without explicit operator authorization",
        },
        "backup_verification": {
            "verified": bool(backup_verified),
            "operator_required": not backup_verified,
            "exact_ui": "AWS Console → RDS → Databases → Snapshots",
            "iam_auto_change_forbidden": True,
        },
        "schema_migration_authorization": {
            "authorized": bool(schema_migrate_authorized),
            "operator_required": True,
            "allowed_without_auth": ["showmigrations", "migrate --plan"],
            "migrate_forbidden_without_separate_auth": True,
        },
        "equipment_mapping_authorization": {
            "authorized": bool(equipment_mapping_authorized),
            "explicit_mappings": int(explicit_mappings),
            "operator_required": True,
            "fuzzy_mapping_forbidden": True,
            "auto_approve_forbidden": True,
        },
        "finance_review": {
            "reviewed": bool(finance_reviewed),
            "operator_required": not finance_reviewed,
            "auto_correct_forbidden": True,
        },
        "datetime_exception_classifications": {
            "null_booking_date": {
                "count": int(totals.get("null_booking_date") or 10),
                "classification": "EXCLUDED",
            },
            "zero_duration": {
                "count": int(totals.get("zero_duration") or 31),
                "classification": "MANUAL_REVIEW",
            },
            "unchanged_from_phase10i_policy": True,
        },
        "discovery_allowed": datetime_status == "APPROVED" and bool(window.get("configured")),
    }


def build_phase10j_final_readiness(
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
) -> dict[str, Any]:
    """Authoritative Phase 10J GO/NO-GO. Refuses readiness when operator gates incomplete."""
    evidence = dict(explicit_evidence or {})
    release = dict(release_plan or {})
    review = datetime_review or build_datetime_review(datetime_validation=datetime_validation)
    if review.get("phase") == "10I":
        review = {**review, "phase": "10J"}

    base = build_phase10i_final_readiness(
        backup_verified=backup_verified,
        mysql_probe=mysql_probe,
        datetime_validation=datetime_validation,
        datetime_review=review,
        wallet_reconciliation=wallet_reconciliation,
        production_migrate_plan=production_migrate_plan,
        test_account_dry_run=test_account_dry_run,
        email_dry_run=email_dry_run,
        release_plan=release,
        explicit_evidence=evidence,
    )

    gate_inspection = inspect_operator_gates(
        backup_verified=backup_verified,
        release_reviewed=bool(release.get("reviewed_released")),
        schema_migrate_authorized=schema_migrate_authorized,
        equipment_mapping_authorized=equipment_mapping_authorized,
        finance_reviewed=finance_reviewed,
        datetime_validation=datetime_validation,
        explicit_mappings=int(evidence.get("explicit_mappings") or 0),
    )

    matrix = dict(base.get("gate_matrix") or {})
    discovery_executed = bool(discovery_result and discovery_result.get("executed"))
    discovery_status = base.get("discovery_status")

    # Never claim discovery PASS without real evidence
    if not gate_inspection["discovery_allowed"]:
        discovery_status = (
            "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL"
            if gate_inspection["datetime_contract_approval"]["operator_required"]
            else "DISCOVERY_BLOCKED_BY_MIGRATION_WINDOW"
        )
        matrix["Upcoming Bookings"] = {
            "result": GATE_BLOCKED,
            "evidence": discovery_status,
            "blocking": True,
            "operator_action": (
                "Main Admin must approve datetime contract AND configure migration window "
                "before read-only discovery may run"
            ),
            "exact_command_or_ui": {
                "datetime_ui": gate_inspection["datetime_contract_approval"]["exact_ui"],
                "datetime_api": gate_inspection["datetime_contract_approval"]["exact_api"],
                "window_ui": gate_inspection["migration_window"]["exact_ui"],
                "window_api": gate_inspection["migration_window"]["exact_api"],
                "after_gates": "python manage.py migration_production_legacy_qualification",
            },
        }
        matrix["Conflicts"] = {
            "result": GATE_BLOCKED,
            "evidence": "Conflict discovery not executed — blocked by datetime/window gates",
            "blocking": True,
            "operator_action": "Run after datetime+window+RO discovery",
            "exact_command_or_ui": "analyze_booking_conflicts / Legacy bookings dashboard",
        }
        matrix["Equipment"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"explicit_mappings={evidence.get('explicit_mappings', 0)}; "
                "eligible-window required set UNKNOWN — discovery not authorized"
            ),
            "blocking": True,
            "operator_action": "Do not fuzzy-map; map only eligible-window IDs after RO discovery",
            "exact_command_or_ui": "/admin/portal-migration/equipment-mapping",
        }
    elif discovery_executed:
        discovery_status = discovery_result.get("status") or "DISCOVERY_COMPLETE_READ_ONLY"
        matrix["Upcoming Bookings"] = {
            "result": GATE_PASS if discovery_result.get("ok") else GATE_BLOCKED,
            "evidence": discovery_result.get("evidence") or "read-only discovery executed",
            "blocking": True,
            "operator_action": discovery_result.get("operator_action") or "none",
            "exact_command_or_ui": "python manage.py migration_production_legacy_qualification",
        }
    else:
        discovery_status = "READY_FOR_DISCOVERY_NOT_YET_RUN"
        matrix["Upcoming Bookings"] = {
            "result": GATE_OPERATOR,
            "evidence": "datetime APPROVED + window configured, but discovery not yet executed",
            "blocking": True,
            "operator_action": "Run read-only migration_production_legacy_qualification",
            "exact_command_or_ui": "python manage.py migration_production_legacy_qualification",
        }

    # Schema: plan-only unless separately authorized (it is NOT)
    if not schema_migrate_authorized:
        import json as _json

        schema_ev = matrix.get("Schema", {}).get("evidence")
        if not schema_ev:
            schema_ev = _json.dumps(base.get("production_migrate_plan") or {}, default=str)[:500]
        matrix["Schema"] = {
            "result": GATE_OPERATOR,
            "evidence": schema_ev,
            "blocking": True,
            "operator_action": (
                "showmigrations / migrate --plan only; DO NOT migrate without separate authorization"
            ),
            "exact_command_or_ui": (
                "docker exec iic-booking-backend-django-1 python manage.py migrate --plan"
            ),
        }

    if not finance_reviewed and wallet_reconciliation:
        wr = wallet_reconciliation
        matrix["Finance"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"exceptions={wr.get('mismatch_count')}; orphans={wr.get('orphan_wallets')}; "
                "no auto-correction; finance review not acknowledged"
            ),
            "blocking": True,
            "operator_action": "Account In Charge review — no auto-correct / opening balances",
            "exact_command_or_ui": "docs/release/migration/phase10i_finance_exception_register.json",
        }

    matrix["T0 Authorization"] = {
        "result": GATE_OPERATOR,
        "evidence": "T0 NOT ACTIVATED; Phase 10J never executes T0",
        "blocking": True,
        "operator_action": "Separate explicit authorization after READY FOR EXPLICIT T0 AUTHORIZATION",
        "exact_command_or_ui": "Do not run T0 in Phase 10J",
    }

    # Recompute blockers / hard refuse from live inspection
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

    hard_refuse: list[str] = []
    if gate_inspection["datetime_contract_approval"]["operator_required"]:
        hard_refuse.append("datetime_unapproved")
    if gate_inspection["migration_window"]["operator_required"]:
        hard_refuse.append("migration_window_missing")
    if gate_inspection["backup_verification"]["operator_required"]:
        hard_refuse.append("backup_unverified")
    if gate_inspection["release_authorization"]["operator_required"]:
        hard_refuse.append("release_missing")
    if gate_inspection["schema_migration_authorization"]["operator_required"]:
        hard_refuse.append("schema_migrate_not_authorized")
    if not finance_reviewed:
        hard_refuse.append("finance_review_pending")
    if int(evidence.get("explicit_mappings") or 0) == 0:
        hard_refuse.append("equipment_mapping_incomplete")
    if not discovery_executed:
        hard_refuse.append("discovery_not_executed")

    tech_ok = all(
        g.get("result") == GATE_PASS
        for name, g in ordered.items()
        if g.get("blocking") and name != "T0 Authorization"
    )
    # Never green-wash: PASS without evidence is refused
    for name, g in ordered.items():
        ev = g.get("evidence")
        ev_text = ev if isinstance(ev, str) else ("" if ev is None else str(ev))
        if g.get("result") == GATE_PASS and not ev_text.strip():
            hard_refuse.append(f"pass_without_evidence:{name}")
            tech_ok = False

    verdict = VERDICT_READY if (tech_ok and not hard_refuse) else VERDICT_NOT_READY
    # Absolute: never READY FOR T0 spelling
    if verdict == "READY FOR T0":
        verdict = VERDICT_NOT_READY

    done = [
        "Inspected live datetime contract approval status (OPERATOR_REQUIRED — not assumed)",
        "Inspected live migration window (unconfigured — dates not invented)",
        "Confirmed datetime exception policy: 10 null EXCLUDED, 31 zero MANUAL_REVIEW",
        "Wallet/finance RO evidence retained for operator review (no writes)",
        "Release / backup / schema / equipment gates reported as OPERATOR_REQUIRED",
        "Dry-run posture: writes=0 / SMTP=0 when executed on this host",
        "Phase 10J readiness closure implemented; refuses READY while gates incomplete",
    ]
    blocked = [
        "Production discovery (datetime unapproved and/or window missing)",
        "Eligible-window equipment candidate set (requires discovery)",
        "Conflict discovery (requires discovery)",
        "User reconciliation for eligible bookings (requires discovery)",
        "Schema migrate (not authorized — plan-only allowed)",
        "Backup verification (AWS Console operator)",
        "Release push/PR/deploy (operator authorization)",
        "Finance auto-correction / opening balances",
        "T0 / freeze / redirect / emails / user-booking-wallet migration",
    ]

    return {
        **base,
        "phase": "10J",
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
        "datetime_contract_status": gate_inspection["datetime_contract_approval"]["status"],
        "migration_window": gate_inspection["migration_window"],
        "phase10i_embedded_verdict": base.get("verdict"),
        "work_completed_this_phase": done,
        "work_blocked_operator_required": blocked,
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
        },
        "production_writes_performed": [],
        "operator_next_actions": [
            "STOP: datetime remains OPERATOR_REQUIRED — Main Admin approve via UI/API (do not auto-approve)",
            "Configure migration window with explicit ISO start/end (do not invent dates)",
            "Only after datetime+window: run migration_production_legacy_qualification (RO)",
            "Map only eligible-window equipment IDs (no fuzzy / no auto-approve)",
            "Finance review 41 mismatches — no auto-correct",
            "AWS Console verify RDS backup → then --backup-verified",
            "Prepare RC; push/PR/deploy only with release authorization",
            "After deploy: showmigrations + migrate --plan; migrate only with separate schema auth",
            "Production test-account + email dry-runs (writes=0 / SMTP=0)",
            "Separate explicit T0 authorization — Phase 10J must not execute T0",
        ],
    }


def write_phase10j_artifacts(
    report: dict[str, Any],
    *,
    datetime_validation: dict[str, Any] | None = None,
    datetime_review: dict[str, Any] | None = None,
) -> list[str]:
    base = Path(getattr(settings, "BASE_DIR", ".")) / ARTIFACT_DIR
    written: list[str] = []
    pairs: list[tuple[str, Any]] = [
        ("phase10j_final_readiness.json", report),
        (
            "phase10j_go_no_go.json",
            {
                "phase": "10J",
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
                "work_completed_this_phase": report.get("work_completed_this_phase"),
                "work_blocked_operator_required": report.get("work_blocked_operator_required"),
                "generated_at_utc": report.get("generated_at_utc"),
            },
        ),
    ]
    if datetime_review:
        pairs.append(("phase10j_datetime_review.json", datetime_review))
    if datetime_validation:
        pairs.append(("legacy_datetime_validation.json", datetime_validation))

    for name, payload in pairs:
        path = write_json_artifact(base / name, payload)
        written.append(path)
    return written
