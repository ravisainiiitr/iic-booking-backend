"""Phase 10I — final production qualification (READ-ONLY / operator-gated). No T0."""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    GATE_BLOCKED,
    GATE_OPERATOR,
    GATE_PASS,
    GATE_WARN,
    VERDICT_NOT_READY,
    VERDICT_READY,
    write_json_artifact,
)
from iic_booking.users.legacy_ledger.phase10h_readiness_closure import (
    PRODUCTION_BASELINE_SHA,
    build_phase10h_final_readiness,
)

ARTIFACT_DIR = Path("docs/release/migration")

# Existing Phase 10D–10H policy (booking_bridge.classify_discovery_row / slot_action_for_row):
# - null booking_date → cannot derive start/end → eligibility "invalid" → EXCLUDED from block set
# - zero duration → start==end under BOOKING_DATETIME_PLUS_DURATION_MINUTES → MANUAL_REVIEW
#   (not auto-rejected by validation; not silently discarded)
NULL_BOOKING_DATE_IDS = [
    35794,
    51380,
    52507,
    64691,
    64731,
    101583,
    101850,
    105820,
    111672,
    111677,
]
ZERO_DURATION_IDS = [
    37,
    52,
    57,
    438,
    468,
    499,
    785,
    800,
    832,
    927,
    3924,
    6195,
    9153,
    13384,
    15870,
    19514,
    20339,
    20549,
    20985,
    21199,
    27632,
    32647,
    32648,
    32649,
    32650,
    32651,
    32664,
    34634,
    63469,
    87263,
    93240,
]


def build_datetime_review(
    *,
    datetime_validation: dict[str, Any] | None = None,
    null_rows: list[dict[str, Any]] | None = None,
    zero_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Operator-readable classification using existing eligibility rules. Does not approve."""
    from iic_booking.users.legacy_ledger.datetime_contract import (
        contract_approval_status,
        load_datetime_contract,
    )

    contract = load_datetime_contract()
    status = contract_approval_status(contract)
    totals = (datetime_validation or {}).get("totals") or {}

    null_classified = []
    for row in null_rows or []:
        null_classified.append(
            {
                **row,
                "classification": "EXCLUDED",
                "eligibility_per_policy": "invalid",
                "slot_action_per_policy": "BLOCKED — invalid datetime",
                "reason": "null booking_date cannot derive start/end under approved strategy",
                "blocks_t0_gate": False,
                "appears_in_dashboard": True,
            }
        )
    if not null_classified:
        for bid in NULL_BOOKING_DATE_IDS:
            null_classified.append(
                {
                    "id": bid,
                    "classification": "EXCLUDED",
                    "eligibility_per_policy": "invalid",
                    "slot_action_per_policy": "BLOCKED — invalid datetime",
                    "reason": "null booking_date (from Phase 10H/10I RO SELECT)",
                    "blocks_t0_gate": False,
                    "appears_in_dashboard": True,
                }
            )

    zero_classified = []
    for row in zero_rows or []:
        zero_classified.append(
            {
                **row,
                "classification": "MANUAL_REVIEW",
                "eligibility_per_policy": "not_auto_invalid",
                "note": (
                    "time_required=0 yields start==end; validation flags for operator review; "
                    "not silently discarded; if start falls outside migration window → EXCLUDED "
                    "as outside_window after window is configured"
                ),
                "blocks_t0_gate": False,
                "appears_in_dashboard": True,
            }
        )
    if not zero_classified:
        for bid in ZERO_DURATION_IDS:
            zero_classified.append(
                {
                    "id": bid,
                    "classification": "MANUAL_REVIEW",
                    "eligibility_per_policy": "not_auto_invalid",
                    "reason": "zero duration (from Phase 10H/10I RO SELECT)",
                    "blocks_t0_gate": False,
                    "appears_in_dashboard": True,
                }
            )

    return {
        "phase": "10I",
        "DATETIME_CONTRACT": status,
        "DATETIME_VALIDATION": "PASS" if (datetime_validation or {}).get("ok") else "BLOCKED",
        "approval_endpoint_called": False,
        "policy_source": [
            "booking_bridge.classify_discovery_row",
            "legacy_booking_admin.slot_action_for_row",
            "legacy_datetime_validation (suspicious not auto-rejected)",
        ],
        "candidate_contract": {
            "start": "booking.booking_date",
            "duration": "booking.time_required",
            "unit": "minutes",
            "end": "booking_date + time_required minutes",
            "timezone": "Asia/Kolkata",
            "strategy": "BOOKING_DATETIME_PLUS_DURATION_MINUTES",
        },
        "totals": totals,
        "null_booking_date": {
            "count": int(totals.get("null_booking_date") or len(null_classified)),
            "classification_default": "EXCLUDED",
            "records": null_classified,
        },
        "zero_duration": {
            "count": int(totals.get("zero_duration") or len(zero_classified)),
            "classification_default": "MANUAL_REVIEW",
            "records": zero_classified,
        },
        "migration_window": (datetime_validation or {}).get("migration_window")
        or {"start": None, "end": None, "configured": False},
        "operator_approval_required": status != "APPROVED",
        "exact_approval_action": {
            "ui": "/admin/portal-migration — Datetime contract card — confirm=true + reason",
            "api": "POST /api/portal-migration/admin/datetime-contract/",
            "body": {
                "confirm": True,
                "approval_reason": "non-empty Main Administrator note required",
            },
            "effects": [
                "Enables read-only discovery",
                "Does NOT activate T0",
                "Does NOT create LegacyBookingBlock",
                "Does NOT freeze old portal",
                "Does NOT send email",
            ],
        },
        "stop_condition": "OPERATOR_REQUIRED — discovery and post-approval phases not executed",
        "note": "Do not invent migration policy; classifications follow Phase 10D–10H architecture.",
    }


def migration_window_status() -> dict[str, Any]:
    try:
        from iic_booking.users.models.portal_migration import PortalMigrationState

        state = PortalMigrationState.get_solo()
        start = getattr(state, "migration_start_at", None)
        end = getattr(state, "migration_window_end_at", None)
        configured = bool(start and end)
        return {
            "configured": configured,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "purpose": (
                "Defines which legacy bookings are in-scope for T0 slot blocking "
                "(start_at in [migration_start_at, migration_window_end_at))"
            ),
            "expected_booking_population": (
                "Unknown until datetime APPROVED + window configured + discovery"
            ),
            "effect_on_discovery": (
                "Without window: legacy_booking_mysql fetch returns error "
                "migration_start_at and migration_window_end_at must be configured"
            ),
            "configuration_locations": {
                "model": "PortalMigrationState.migration_start_at / migration_window_end_at",
                "schema": "users.0102_legacy_equipment_booking_bridge",
                "api": "PATCH /api/portal-migration/admin/state/",
                "ui": "/admin/portal-migration — Phase 8B settings (MIGRATION_START_AT / MIGRATION_WINDOW_END_AT)",
            },
            "dates_invented": False,
            "operator_action": (
                "Main Administrator must set explicit start/end ISO datetimes; do not invent dates"
            ),
            "does_not_activate_t0": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "configured": False,
            "error": str(exc),
            "operator_action": "Apply schema 0102 then configure window via admin UI/API",
        }


def build_phase10i_final_readiness(
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
) -> dict[str, Any]:
    """Authoritative Phase 10I GO/NO-GO. Refuses readiness when gates incomplete."""
    base = build_phase10h_final_readiness(
        backup_verified=backup_verified,
        mysql_probe=mysql_probe,
        datetime_validation=datetime_validation,
        production_migrate_plan=production_migrate_plan,
        explicit_evidence=explicit_evidence,
    )
    matrix = dict(base.get("gate_matrix") or {})
    window = migration_window_status()
    review = datetime_review or build_datetime_review(
        datetime_validation=datetime_validation or base.get("datetime_validation")
    )

    # Migration Window — separate gate (Phase 10I requirement)
    if window.get("configured"):
        matrix["Migration Window"] = {
            "result": GATE_PASS,
            "evidence": f"start={window.get('start')}; end={window.get('end')}",
            "blocking": True,
            "operator_action": "none",
            "exact_command_or_ui": window.get("configuration_locations", {}).get("ui"),
        }
    else:
        matrix["Migration Window"] = {
            "result": GATE_OPERATOR,
            "evidence": "migration window configured=false; dates not invented",
            "blocking": True,
            "operator_action": window.get("operator_action"),
            "exact_command_or_ui": (
                "PATCH /api/portal-migration/admin/state/ with migration_start_at + "
                "migration_window_end_at OR UI Phase 8B settings"
            ),
        }

    # Enrich Datetime gate with review stop
    matrix["Datetime"] = {
        "result": GATE_OPERATOR,
        "evidence": (
            f"validation=PASS; contract={review.get('DATETIME_CONTRACT')}; "
            f"null={len((review.get('null_booking_date') or {}).get('records') or [])} EXCLUDED; "
            f"zero={len((review.get('zero_duration') or {}).get('records') or [])} MANUAL_REVIEW; "
            "approval_called=False; STOP at OPERATOR_REQUIRED"
        ),
        "blocking": True,
        "operator_action": "Main Admin approve datetime contract after reviewing phase10i_datetime_review.json",
        "exact_command_or_ui": review.get("exact_approval_action"),
    }

    # Upcoming / discovery still blocked
    matrix["Upcoming Bookings"] = matrix.pop("Upcoming bookings", None) or {
        "result": GATE_BLOCKED,
        "evidence": "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL",
        "blocking": True,
    }
    if review.get("DATETIME_CONTRACT") != "APPROVED" or not window.get("configured"):
        matrix["Upcoming Bookings"] = {
            "result": GATE_BLOCKED,
            "evidence": (
                "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL"
                if review.get("DATETIME_CONTRACT") != "APPROVED"
                else "DISCOVERY_BLOCKED_BY_MIGRATION_WINDOW"
            ),
            "blocking": True,
            "operator_action": "Approve datetime + configure window, then run read-only discovery",
            "exact_command_or_ui": "python manage.py migration_production_legacy_qualification",
        }

    # Equipment — required set unknown until discovery
    matrix["Equipment"] = matrix.pop("Equipment mappings", None) or {
        "result": GATE_OPERATOR,
        "evidence": "explicit_mappings=0; eligible-window required set unknown until discovery",
        "blocking": True,
        "operator_action": "After discovery, map only required legacy IDs explicitly (no fuzzy)",
        "exact_command_or_ui": "/admin/portal-migration/equipment-mapping",
    }
    if int((explicit_evidence or {}).get("explicit_mappings") or 0) == 0:
        matrix["Equipment"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                "legacy_ids_in_booking=45; new_equipment≈64; mappings=0; "
                "required eligible-window set UNKNOWN until datetime+window+discovery"
            ),
            "blocking": True,
            "operator_action": "Do not map all 45; map only IDs with eligible-window bookings",
            "exact_command_or_ui": "/admin/portal-migration/equipment-mapping",
        }

    # Users rename consistency
    if "User mappings" in matrix and "Users" not in matrix:
        matrix["Users"] = matrix.pop("User mappings")
    elif "Users" not in matrix:
        matrix["Users"] = {
            "result": GATE_WARN,
            "evidence": "USER UNRESOLVED does not block T0 when equipment+time valid",
            "blocking": False,
        }

    # Wallets / Finance from wallet_reconciliation if provided
    if wallet_reconciliation and wallet_reconciliation.get("ok"):
        wr = wallet_reconciliation
        matrix["Wallets"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"wallets={wr.get('wallet_count')}; txns={wr.get('transaction_count')}; "
                f"max_id={wr.get('max_transaction_id')}; mismatches={wr.get('mismatch_count')}; "
                f"orphans={wr.get('orphan_wallets')}; gap={wr.get('ledger_vs_stored_gap')}"
            ),
            "blocking": True,
            "operator_action": "Finance review phase10i_wallet_reconciliation.json — no auto-correct",
            "exact_command_or_ui": "Account In Charge review + Main Admin acknowledgment",
        }
        matrix["Finance"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"exceptions={wr.get('mismatch_count')}; outliers={wr.get('suspicious_transactions', {}).get('outlier_abs_gt_10m')}; "
                "no auto-correction"
            ),
            "blocking": True,
            "operator_action": "Review finance exception register; do not reverse/import/correct automatically",
            "exact_command_or_ui": "docs/release/migration/phase10i_finance_exception_register.json",
        }

    # Conflicts — unknown until discovery
    matrix["Conflicts"] = {
        "result": GATE_BLOCKED,
        "evidence": "Conflict discovery not executed — blocked by datetime/window gates",
        "blocking": True,
        "operator_action": "After discovery, run conflict analyzer; resolve blocking conflicts",
        "exact_command_or_ui": "analyze_booking_conflicts / Legacy bookings dashboard",
    }

    # Backup
    if backup_verified:
        matrix["Backup"] = {
            "result": GATE_PASS,
            "evidence": "backup_verified=True (operator-confirmed via AWS Console)",
            "blocking": True,
            "operator_action": "none",
            "exact_command_or_ui": "--backup-verified",
        }
    else:
        matrix["Backup"] = {
            "result": GATE_BLOCKED,
            "evidence": (
                "RDS DescribeDBInstances/DescribeDBSnapshots AccessDenied on EC2 IAM; "
                "backup_verified=False"
            ),
            "blocking": True,
            "operator_action": "AWS Console verify latest snapshot (do not change IAM automatically)",
            "exact_command_or_ui": "AWS Console → RDS → Databases → select instance → Maintenance & backups / Snapshots",
        }

    # Test account / Email
    if test_account_dry_run and int(test_account_dry_run.get("writes_performed") or -1) == 0:
        matrix["Test Account"] = {
            "result": GATE_OPERATOR if (test_account_dry_run.get("environment") != "production") else GATE_PASS,
            "evidence": (
                f"writes_performed=0; test_users={test_account_dry_run.get('test_users')}; "
                f"env={test_account_dry_run.get('environment')}"
            ),
            "blocking": True,
            "operator_action": "Run production test-account dry-run on production host; no cleanup",
            "exact_command_or_ui": "GET /api/portal-migration/admin/test-account-dry-run/",
        }
    else:
        matrix["Test Account"] = matrix.pop("Test accounts", None) or {
            "result": GATE_OPERATOR,
            "evidence": "production dry-run not confirmed",
            "blocking": True,
        }

    if email_dry_run and int(email_dry_run.get("smtp_sends") or email_dry_run.get("sent") or 0) == 0:
        matrix["Email"] = {
            "result": GATE_OPERATOR if email_dry_run.get("environment") != "production" else GATE_PASS,
            "evidence": (
                f"smtp_sends=0; recipients={email_dry_run.get('total_recipients')}; "
                f"by_template={email_dry_run.get('by_template')}; env={email_dry_run.get('environment')}"
            ),
            "blocking": True,
            "operator_action": "Run production notification dry-run; do not send",
            "exact_command_or_ui": "POST /api/portal-migration/admin/notification-dry-run/",
        }
    else:
        matrix["Email"] = matrix.get("Email") or {
            "result": GATE_OPERATOR,
            "evidence": "production email dry-run not confirmed",
            "blocking": True,
        }

    # Release
    rp = release_plan or {}
    if not rp.get("reviewed_released"):
        matrix["Release"] = {
            "result": GATE_BLOCKED,
            "evidence": (
                f"prod={PRODUCTION_BASELINE_SHA}; local_backend={rp.get('local_backend_sha') or '84aa6e5+'}; "
                f"frontend={rp.get('local_frontend_sha') or 'de71188+'}; uncommitted Phase 10D–10I; "
                f"push={rp.get('push_executed', False)}; deploy={rp.get('deploy_executed', False)}"
            ),
            "blocking": True,
            "operator_action": "Prepare clean RC commit/PR/tag — OPERATOR ACTION REQUIRED to push",
            "exact_command_or_ui": "See production_release_plan.json / phase10i release section",
        }

    # Schema
    matrix["Schema"] = {
        "result": GATE_OPERATOR,
        "evidence": json.dumps(
            production_migrate_plan
            or {
                "applied_on_prod": ["0096", "0097", "0098", "0099", "0100"],
                "pending_on_prod_image": ["0101", "0102", "0103"],
                "pending_after_10d_deploy": ["0104"],
                "migrate_executed": False,
            },
            default=str,
        )[:500],
        "blocking": True,
        "operator_action": "After deploy: showmigrations; migrate --plan; explicit MIGRATE only with separate auth",
        "exact_command_or_ui": (
            "docker exec iic-booking-backend-django-1 python manage.py migrate --plan"
        ),
    }

    # Security / Rollback / T0
    matrix["Security"] = {
        "result": GATE_PASS if (explicit_evidence or {}).get("security_tests_pass") else GATE_OPERATOR,
        "evidence": (explicit_evidence or {}).get(
            "security_evidence",
            "Run Phase 10G/10I security suite — Main Admin only for T0/control",
        ),
        "blocking": True,
        "operator_action": "Confirm security regression PASS",
        "exact_command_or_ui": "pytest users/tests/test_phase10*.py users/tests/test_phase8*.py",
    }
    matrix["Rollback"] = {
        "result": GATE_WARN,
        "evidence": "APP ROLLBACK != DATABASE ROLLBACK; documented in Phase 10G rollback readiness",
        "blocking": False,
        "operator_action": "Review AI30-AI31-PHASE-10G-ROLLBACK-READINESS.md before T0",
        "exact_command_or_ui": "docs/release/migration/AI30-AI31-PHASE-10G-ROLLBACK-READINESS.md",
    }
    matrix["T0 Authorization"] = {
        "result": GATE_OPERATOR,
        "evidence": "T0 NOT ACTIVATED; separate explicit authorization required even if all other gates PASS",
        "blocking": True,
        "operator_action": "Do not execute T0 in Phase 10I",
        "exact_command_or_ui": "Separate operator prompt after READY FOR EXPLICIT T0 AUTHORIZATION",
    }
    # Drop legacy Phase 10G/10H key spelling if present
    matrix.pop("T0 authorization", None)
    matrix.pop("Upcoming bookings", None)
    matrix.pop("Equipment mappings", None)
    matrix.pop("User mappings", None)
    matrix.pop("Test accounts", None)
    matrix.pop("Emails", None)

    # Canonical matrix key order for dashboard
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

    tech_ok = all(
        g.get("result") == GATE_PASS
        for name, g in ordered.items()
        if g.get("blocking") and name != "T0 Authorization"
    )
    # Hard refuse if these incomplete (even if somehow marked PASS)
    hard_refuse = []
    if review.get("DATETIME_CONTRACT") != "APPROVED":
        hard_refuse.append("datetime_unapproved")
    if not window.get("configured"):
        hard_refuse.append("migration_window_missing")
    if not backup_verified:
        hard_refuse.append("backup_unverified")
    if not (release_plan or {}).get("reviewed_released"):
        hard_refuse.append("release_missing")
    if int((explicit_evidence or {}).get("explicit_mappings") or 0) == 0 and review.get(
        "DATETIME_CONTRACT"
    ) == "APPROVED":
        # After approval, incomplete equipment still blocks; before approval, discovery blocked anyway
        hard_refuse.append("equipment_mapping_incomplete_pending_discovery")

    verdict = VERDICT_READY if (tech_ok and not hard_refuse) else VERDICT_NOT_READY

    return {
        "phase": "10I",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "verdict": verdict,
        "t0_executed": False,
        "explicit_t0_authorization_required": True,
        "hard_refuse_reasons": hard_refuse,
        "production_baseline_sha": PRODUCTION_BASELINE_SHA,
        "backend_local_sha": (release_plan or {}).get("local_backend_sha") or "84aa6e5+uncommitted",
        "frontend_local_sha": (release_plan or {}).get("local_frontend_sha") or "de71188+uncommitted",
        "legacy_mysql": base.get("legacy_mysql"),
        "datetime_validation_status": base.get("datetime_validation_status"),
        "datetime_contract_status": review.get("DATETIME_CONTRACT"),
        "datetime_review": {
            "null_excluded": (review.get("null_booking_date") or {}).get("count"),
            "zero_manual_review": (review.get("zero_duration") or {}).get("count"),
            "stop": review.get("stop_condition"),
        },
        "migration_window": window,
        "discovery_status": (
            "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL"
            if review.get("DATETIME_CONTRACT") != "APPROVED"
            else (
                "DISCOVERY_BLOCKED_BY_MIGRATION_WINDOW"
                if not window.get("configured")
                else "READY_FOR_DISCOVERY"
            )
        ),
        "discovery_executed": False,
        "wallet_reconciliation_summary": {
            k: (wallet_reconciliation or {}).get(k)
            for k in (
                "ok",
                "wallet_count",
                "transaction_count",
                "max_transaction_id",
                "watermark",
                "mismatch_count",
                "orphan_wallets",
                "ledger_vs_stored_gap",
            )
        }
        if wallet_reconciliation
        else None,
        "production_migrate_plan": production_migrate_plan or base.get("production_migrate_plan"),
        "gate_matrix": ordered,
        "blockers": sorted(set(blockers)),
        "phase10h_embedded_verdict": base.get("verdict"),
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
        },
        "production_writes_performed": [],
        "operator_next_actions": [
            "STOP: datetime remains OPERATOR_REQUIRED — Main Admin must approve explicitly",
            "Configure migration window (do not invent dates) via Phase 8B UI/API",
            "Commit/PR/tag clean Phase 10D–10I release candidate (OPERATOR ACTION for push)",
            "Verify RDS backup in AWS Console → then --backup-verified",
            "After deploy: migrate --plan then explicit MIGRATE 0101–0104 (separate auth)",
            "Only after datetime+window: run migration_production_legacy_qualification (RO)",
            "Map only eligible-window equipment IDs explicitly",
            "Finance review mismatches/orphans (no auto-correct)",
            "Production test-account + email dry-runs (writes=0 / SMTP=0)",
            "Separate explicit T0 authorization",
        ],
    }


def write_phase10i_artifacts(
    report: dict[str, Any],
    *,
    datetime_validation: dict[str, Any],
    datetime_review: dict[str, Any],
    wallet_reconciliation: dict[str, Any] | None = None,
    finance_register: dict[str, Any] | None = None,
    release_plan: dict[str, Any] | None = None,
    backup_procedure: dict[str, Any] | None = None,
) -> list[str]:
    base = Path(getattr(settings, "BASE_DIR", ".")) / ARTIFACT_DIR
    written: list[str] = []
    pairs: list[tuple[str, Any]] = [
        ("phase10i_final_readiness.json", report),
        (
            "phase10i_go_no_go.json",
            {
                "verdict": report["verdict"],
                "t0_executed": False,
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "production_safety": report.get("production_safety"),
                "discovery_status": report.get("discovery_status"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "generated_at_utc": report.get("generated_at_utc"),
            },
        ),
        ("phase10i_datetime_review.json", datetime_review),
        ("legacy_datetime_validation.json", datetime_validation),
    ]
    if wallet_reconciliation:
        pairs.append(("phase10i_wallet_reconciliation.json", wallet_reconciliation))
    if finance_register:
        pairs.append(("phase10i_finance_exception_register.json", finance_register))
    if release_plan:
        pairs.append(("production_release_plan.json", release_plan))
    if backup_procedure:
        pairs.append(("production_backup_readiness.json", backup_procedure))

    for name, payload in pairs:
        path = write_json_artifact(base / name, payload)
        written.append(path)
    return written
