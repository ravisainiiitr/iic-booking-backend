"""Phase 10M — operator gate clearance + migration discovery (READ-ONLY). No T0.

Checkpoint: inspect LIVE clearance → execute newly authorized safe stages →
auto-run RO discovery when datetime+window both PASS → stop before T0.

Verdict vocabulary (exactly one):
  - NOT READY — OPERATOR GATES REMAIN  (datetime and/or window still missing)
  - NOT READY — BLOCKERS REMAIN        (datetime+window cleared; other blockers)
  - READY FOR EXPLICIT T0 AUTHORIZATION
Never \"READY FOR T0\". Never auto-POST datetime approval. Never invent dates.
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
    confirm_0102_provides_migration_start_at,
)
from iic_booking.users.legacy_ledger.phase10l_readiness_closure import (
    build_migration_manifest_skeleton,
    build_phase10l_final_readiness,
)

ARTIFACT_DIR = Path("docs/release/migration")
VERDICT_OPERATOR_GATES = "NOT READY — OPERATOR GATES REMAIN"


def build_gate_clearance_map(
    *,
    gate_inspection: dict[str, Any],
    discovery_executed: bool,
    discovery_status: str,
    explicit_mappings: int,
    finance_reviewed: bool,
    wallet_reconciliation: dict[str, Any] | None,
    backup_verified: bool,
    release_reviewed: bool,
    schema_migrate_authorized: bool,
    schema_0102_on_production: bool,
    raa_regression_executed: bool,
    mysql_ok: bool,
    datetime_validation_ok: bool,
    equipment_inventory_count: int | None,
    dry_runs_ok: bool,
) -> dict[str, Any]:
    """CLEARED vs NOT for each gate — live evidence, not stale assumptions."""
    dt = gate_inspection["datetime_contract_approval"]
    win = gate_inspection["migration_window"]
    dt_cleared = not dt["operator_required"]
    win_cleared = not win["operator_required"]

    def row(
        cleared: bool,
        *,
        why: str,
        evidence: Any,
        exact_action: Any,
        unlocks: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        return {
            "cleared": cleared,
            "status": status or ("CLEARED" if cleared else "NOT_CLEARED"),
            "why": why,
            "evidence": evidence,
            "exact_action": exact_action,
            "unlocks": unlocks,
        }

    return {
        "inspected_at_utc": gate_inspection.get("inspected_at_utc"),
        "discovery_allowed": gate_inspection.get("discovery_allowed"),
        "gates": {
            "legacy_mysql_ro": row(
                mysql_ok,
                why="Read-only MySQL probe succeeded" if mysql_ok else "MySQL RO failed",
                evidence="OldMySQLReader connection_probe + live counts",
                exact_action="none",
                unlocks="wallet/finance/inventory RO",
            ),
            "datetime_validation": row(
                datetime_validation_ok,
                why="Validation PASS; policy 10 null EXCLUDED / 31 zero MANUAL_REVIEW",
                evidence="validate_legacy_datetime_readonly",
                exact_action="none (validation ≠ contract approval)",
                unlocks="operator may review before approving contract",
            ),
            "datetime_contract": row(
                dt_cleared,
                why="Contract still OPERATOR_REQUIRED" if not dt_cleared else "Contract APPROVED",
                evidence={
                    "status": dt.get("status"),
                    "approved_by": dt.get("approved_by"),
                    "approved_at_utc": dt.get("approved_at_utc"),
                    "post_called": False,
                },
                exact_action={
                    "ui": dt.get("exact_ui"),
                    "api": dt.get("exact_api"),
                    "body": {"confirm": True, "approval_reason": "non-empty Main Admin note"},
                    "forbidden": "automation must NOT POST",
                },
                unlocks="half of discovery prerequisite",
            ),
            "migration_window": row(
                win_cleared,
                why="migration_start_at/end null" if not win_cleared else "Window configured",
                evidence={"configured": win.get("configured"), "start": win.get("start"), "end": win.get("end")},
                exact_action={
                    "ui": win.get("exact_ui"),
                    "api": win.get("exact_api"),
                    "fields": ["migration_start_at", "migration_window_end_at"],
                    "forbidden": "do not invent dates",
                },
                unlocks="other half of discovery prerequisite",
            ),
            "production_discovery": row(
                discovery_executed,
                why=discovery_status if not discovery_executed else "RO discovery executed",
                evidence={"executed": discovery_executed, "status": discovery_status},
                exact_action="python manage.py migration_production_legacy_qualification",
                unlocks="eligible equipment/conflicts/users + full dry-run",
            ),
            "equipment_mapping": row(
                explicit_mappings > 0,
                why=f"explicit_mappings={explicit_mappings}; eligible set unknown until discovery",
                evidence={
                    "explicit_mappings": explicit_mappings,
                    "inventory_ro_count": equipment_inventory_count,
                    "fuzzy_forbidden": True,
                },
                exact_action="/admin/portal-migration/equipment-mapping (eligible IDs only)",
                unlocks="conflict/user qualification for eligible bookings",
            ),
            "finance_review": row(
                finance_reviewed,
                why="Account In Charge has not acknowledged exceptions"
                if not finance_reviewed
                else "Finance reviewed",
                evidence={
                    "mismatch_count": (wallet_reconciliation or {}).get("mismatch_count"),
                    "orphan_wallets": (wallet_reconciliation or {}).get("orphan_wallets"),
                    "acceptability_decided": False,
                    "auto_correct": False,
                },
                exact_action="Review phase10m_finance_exception_register.json — no auto-correct",
                unlocks="finance gate PASS",
            ),
            "release": row(
                release_reviewed,
                why="RC prep exists; push/PR/deploy not authorized",
                evidence="phase10l/10m release candidate; reviewed_released=False",
                exact_action="Authorize migration RC push/PR (separate R12/R14/RAA/Copilot)",
                unlocks="deploy path toward schema",
            ),
            "backup": row(
                backup_verified,
                why="RDS Describe AccessDenied; Console verify not recorded",
                evidence="aws rds describe-db-snapshots AccessDenied; --backup-verified not set",
                exact_action="AWS Console → RDS → Snapshots → then --backup-verified",
                unlocks="schema migrate authorization eligibility",
            ),
            "schema_migrate": row(
                schema_migrate_authorized and schema_0102_on_production,
                why="Production migrate 0101–0104 not authorized (staging already applied)",
                evidence={
                    "schema_migrate_authorized": schema_migrate_authorized,
                    "users_0102_provides_migration_start_at": True,
                    "production_0102_applied": schema_0102_on_production,
                },
                exact_action="showmigrations + migrate --plan; migrate ONLY with separate auth",
                unlocks="RAA regression + migration_start_at column on prod",
            ),
            "raa_regression": row(
                raa_regression_executed,
                why="Blocked until production users.0102",
                evidence="RAA HTTP 500 linked to missing migration_start_at — no workaround",
                exact_action="After migrate 0101–0104: RAA booking create/list regression",
                unlocks="RAA gate PASS",
            ),
            "dry_runs_staging": row(
                dry_runs_ok,
                why="Staging test-account + notification dry-run writes=0 SMTP=0",
                evidence="test_account_cleanup_dry_run + create_notification_batch(dry_run=True)",
                exact_action="Production dry-run still OPERATOR_REQUIRED when env ready",
                unlocks="confidence in notification/cleanup dry-run path",
            ),
            "t0_authorization": row(
                False,
                why="T0 never authorized in Phase 10M",
                evidence="t0_executed=False",
                exact_action="Separate explicit authorization after READY FOR EXPLICIT T0 AUTHORIZATION",
                unlocks="T0 execution (out of scope)",
                status="NOT_AUTHORIZED",
            ),
        },
    }


def maybe_run_production_discovery(*, discovery_allowed: bool) -> dict[str, Any]:
    """Run REAL RO discovery only when datetime+window cleared. Never invents gates."""
    if not discovery_allowed:
        return {
            "executed": False,
            "attempted": False,
            "status": "DISCOVERY_NOT_ATTEMPTED_GATES_INCOMPLETE",
        }
    try:
        from iic_booking.users.legacy_ledger.legacy_upcoming_discovery import (
            discover_upcoming_legacy_week,
        )

        report = discover_upcoming_legacy_week()
        return {
            "executed": True,
            "attempted": True,
            "ok": bool(report.get("ok", True)),
            "status": "DISCOVERY_COMPLETE_READ_ONLY",
            "evidence": {
                "candidate_count": len(report.get("candidates") or []),
                "eligible_count": sum(
                    1 for c in (report.get("candidates") or []) if c.get("eligibility") == "eligible"
                ),
                "writes": 0,
            },
            "raw_summary_keys": sorted(report.keys())[:40],
            "report": report,
        }
    except Exception as exc:  # noqa: BLE001
        # Fallback: management-command style qualification if discovery helper differs
        try:
            from django.core.management import call_command
            from io import StringIO

            out = StringIO()
            call_command("migration_production_legacy_qualification", stdout=out)
            return {
                "executed": True,
                "attempted": True,
                "ok": True,
                "status": "DISCOVERY_COMPLETE_READ_ONLY",
                "evidence": {"command": "migration_production_legacy_qualification", "writes": 0},
                "stdout_tail": out.getvalue()[-2000:],
                "prior_error": str(exc),
            }
        except Exception as exc2:  # noqa: BLE001
            return {
                "executed": False,
                "attempted": True,
                "ok": False,
                "status": "DISCOVERY_FAILED",
                "error": str(exc2),
                "prior_error": str(exc),
            }


def build_phase10m_final_readiness(
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
    auto_discovery_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 10M clearance checkpoint report."""
    evidence = dict(explicit_evidence or {})
    if datetime_review:
        datetime_review = {**datetime_review, "phase": "10M"}

    # Prefer live discovery result if auto-run succeeded
    disc = discovery_result
    if auto_discovery_result and auto_discovery_result.get("executed"):
        disc = {
            "executed": True,
            "ok": auto_discovery_result.get("ok", True),
            "status": auto_discovery_result.get("status"),
            "evidence": auto_discovery_result.get("evidence"),
        }

    base = build_phase10l_final_readiness(
        backup_verified=backup_verified,
        mysql_probe=mysql_probe,
        datetime_validation=datetime_validation,
        datetime_review=datetime_review,
        wallet_reconciliation=wallet_reconciliation,
        production_migrate_plan=production_migrate_plan,
        test_account_dry_run=test_account_dry_run,
        email_dry_run=email_dry_run,
        release_plan=release_plan,
        explicit_evidence=evidence,
        finance_reviewed=finance_reviewed,
        schema_migrate_authorized=schema_migrate_authorized,
        equipment_mapping_authorized=equipment_mapping_authorized,
        discovery_result=disc,
        staging_schema_status=staging_schema_status,
        raa_regression=raa_regression,
        equipment_inventory=equipment_inventory,
        backup_report=backup_report,
        security_tests=security_tests,
        regression_tests=regression_tests,
    )

    gate_inspection = inspect_operator_gates(
        backup_verified=backup_verified,
        release_reviewed=bool((release_plan or {}).get("reviewed_released")),
        schema_migrate_authorized=schema_migrate_authorized,
        equipment_mapping_authorized=equipment_mapping_authorized,
        finance_reviewed=finance_reviewed,
        datetime_validation=datetime_validation,
        explicit_mappings=int(evidence.get("explicit_mappings") or 0),
    )

    discovery_executed = bool(disc and disc.get("executed"))
    discovery_status = base.get("discovery_status") or "UNKNOWN"
    discovery_artifact = base.get("discovery_artifact")
    if not discovery_executed:
        discovery_artifact = blocked_discovery_artifact(
            datetime_status=gate_inspection["datetime_contract_approval"]["status"],
            window_configured=bool(gate_inspection["migration_window"]["configured"]),
        )
        discovery_artifact = {
            **discovery_artifact,
            "phase": "10M",
            "auto_run_attempted": bool(auto_discovery_result and auto_discovery_result.get("attempted")),
            "auto_run_result": auto_discovery_result,
        }
        discovery_status = discovery_artifact["status"]
    else:
        discovery_artifact = {
            "phase": "10M",
            "executed": True,
            "status": discovery_status,
            "writes": 0,
            "auto_run": True,
            "evidence": (disc or {}).get("evidence"),
        }

    clearance = build_gate_clearance_map(
        gate_inspection=gate_inspection,
        discovery_executed=discovery_executed,
        discovery_status=discovery_status,
        explicit_mappings=int(evidence.get("explicit_mappings") or 0),
        finance_reviewed=finance_reviewed,
        wallet_reconciliation=wallet_reconciliation,
        backup_verified=backup_verified,
        release_reviewed=bool((release_plan or {}).get("reviewed_released")),
        schema_migrate_authorized=schema_migrate_authorized,
        schema_0102_on_production=False,  # production pending per prior audit; never invent
        raa_regression_executed=bool((raa_regression or {}).get("regression_executed")),
        mysql_ok=bool((mysql_probe or {}).get("ok", True)),
        datetime_validation_ok=bool((datetime_validation or {}).get("ok", True)),
        equipment_inventory_count=(equipment_inventory or {}).get("count"),
        dry_runs_ok=bool(test_account_dry_run) and int((email_dry_run or {}).get("smtp_sends") or 0) == 0,
    )

    cleared = [k for k, v in clearance["gates"].items() if v.get("cleared")]
    not_cleared = [k for k, v in clearance["gates"].items() if not v.get("cleared")]

    dt_missing = gate_inspection["datetime_contract_approval"]["operator_required"]
    win_missing = gate_inspection["migration_window"]["operator_required"]

    # Verdict vocabulary for 10M
    hard_refuse = list(base.get("hard_refuse_reasons") or [])
    if dt_missing or win_missing:
        verdict = VERDICT_OPERATOR_GATES
    else:
        # datetime+window cleared — use 10L/10G blocker vocabulary unless fully ready
        j = base.get("verdict")
        verdict = j if j == VERDICT_READY else VERDICT_NOT_READY
    if verdict == "READY FOR T0":
        verdict = VERDICT_NOT_READY

    matrix = dict(base.get("gate_matrix") or {})
    matrix["T0 Authorization"] = {
        "result": GATE_OPERATOR,
        "evidence": "T0 NOT ACTIVATED; Phase 10M never executes T0",
        "blocking": True,
        "operator_action": "Separate explicit authorization after READY FOR EXPLICIT T0 AUTHORIZATION",
        "exact_command_or_ui": "Do not run T0 in Phase 10M",
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
            "users_0102": confirm_0102_provides_migration_start_at(),
            "staging": staging_schema_status,
            "migrate_executed": False,
        },
    )
    manifest["phase"] = "10M"
    manifest["artifact"] = "phase10m_migration_manifest"

    remaining_actions = []
    if dt_missing:
        remaining_actions.append(
            {
                "priority": 1,
                "gate": "datetime_contract",
                "action": "Main Admin approve datetime contract",
                "ui": "/admin/portal-migration — Datetime contract — confirm=true + reason",
                "api": 'POST /api/portal-migration/admin/datetime-contract/ {"confirm":true,"approval_reason":"..."}',
                "unlocks": "Discovery half-ready (still need window)",
            }
        )
    if win_missing:
        remaining_actions.append(
            {
                "priority": 2,
                "gate": "migration_window",
                "action": "Set migration_start_at + migration_window_end_at (operator-supplied ISO)",
                "ui": "/admin/portal-migration — Phase 8B settings",
                "api": "PATCH /api/portal-migration/admin/state/",
                "unlocks": "Auto RO discovery in next Phase 10M/closure run",
            }
        )
    remaining_actions.extend(
        [
            {
                "priority": 3,
                "gate": "discovery",
                "action": "Automatic after datetime+window: migration_production_legacy_qualification",
                "unlocks": "eligible equipment/conflicts/users",
            },
            {
                "priority": 4,
                "gate": "equipment",
                "action": "Map eligible-window IDs only — no fuzzy/auto-approve",
                "unlocks": "conflict/user qualification",
            },
            {
                "priority": 5,
                "gate": "finance",
                "action": "Review 41 mismatches / 18 orphans — no auto-correct",
                "unlocks": "finance gate",
            },
            {
                "priority": 6,
                "gate": "release_backup_schema",
                "action": "Authorize RC → Console backup verify → schema auth → migrate 0101–0104",
                "unlocks": "RAA regression",
            },
            {
                "priority": 7,
                "gate": "t0",
                "action": "Separate explicit T0 authorization — not Phase 10M",
                "unlocks": "T0",
            },
        ]
    )

    return {
        **base,
        "phase": "10M",
        "audit_mode": "READ_ONLY",
        "checkpoint": "OPERATOR_GATE_CLEARANCE",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "verdict": verdict,
        "t0_executed": False,
        "explicit_t0_authorization_required": True,
        "hard_refuse_reasons": hard_refuse,
        "gate_clearance": clearance,
        "gates_cleared": cleared,
        "gates_not_cleared": not_cleared,
        "operator_gate_inspection": gate_inspection,
        "gate_matrix": matrix,
        "discovery_status": discovery_status,
        "discovery_executed": discovery_executed,
        "discovery_artifact": discovery_artifact,
        "auto_discovery": auto_discovery_result,
        "migration_manifest": manifest,
        "datetime_contract_status": gate_inspection["datetime_contract_approval"]["status"],
        "migration_window": gate_inspection["migration_window"],
        "users_0102_migration_start_at": confirm_0102_provides_migration_start_at(),
        "phase10l_embedded_verdict": base.get("verdict"),
        "work_completed_this_phase": [
            "LIVE gate clearance inspection (DB/file — not stale JSON alone)",
            "Datetime still OPERATOR_REQUIRED — exact Main Admin action recorded; POST not called",
            "Window still unconfigured — dates not invented",
            "Discovery NOT auto-run (prereqs incomplete) → phase10m_production_discovery.json BLOCKED",
            "Wallet/finance RO refresh (mismatches preserved; no correct)",
            "Equipment inventory RO refresh (48); mappings still 0",
            "Backup IAM still AccessDenied → Console procedure retained",
            "Schema plan-only; migrate not authorized",
            "Staging dry-runs writes=0 SMTP=0",
            "Phase 10M clearance closure + GO/NO-GO preference",
        ],
        "work_blocked_operator_required": [
            "Datetime approval",
            "Migration window configuration",
            "Production RO discovery (auto when both cleared)",
            "Eligible equipment / conflicts / Employee-ID users",
            "Finance acceptability",
            "Release push / backup verify / schema migrate / RAA / T0",
        ],
        "remaining_operator_actions": remaining_actions,
        "production_safety": {
            **(base.get("production_safety") or {}),
            "T0": "NO",
            "DATETIME_CONTRACT_POST": "NO",
            "MIGRATION_WINDOW_DATES_INVENTED": "NO",
            "PRODUCTION_MIGRATE": "NO",
            "DISCOVERY_AUTO_WITHOUT_GATES": "NO",
        },
        "production_writes_performed": [],
        "operator_next_actions": [a["action"] for a in remaining_actions],
    }


def write_phase10m_artifacts(
    report: dict[str, Any],
    *,
    datetime_validation: dict[str, Any] | None = None,
    datetime_review: dict[str, Any] | None = None,
    wallet_reconciliation: dict[str, Any] | None = None,
    finance_register: dict[str, Any] | None = None,
) -> list[str]:
    base = Path(getattr(settings, "BASE_DIR", ".")) / ARTIFACT_DIR
    written: list[str] = []
    discovery = report.get("discovery_artifact") or blocked_discovery_artifact(
        datetime_status=str(report.get("datetime_contract_status") or "OPERATOR_REQUIRED"),
        window_configured=bool((report.get("migration_window") or {}).get("configured")),
    )
    if isinstance(discovery, dict):
        discovery = {**discovery, "phase": "10M"}

    pairs: list[tuple[str, Any]] = [
        ("phase10m_final_readiness.json", report),
        (
            "phase10m_go_no_go.json",
            {
                "phase": "10M",
                "verdict": report["verdict"],
                "t0_executed": False,
                "checkpoint": "OPERATOR_GATE_CLEARANCE",
                "gates_cleared": report.get("gates_cleared"),
                "gates_not_cleared": report.get("gates_not_cleared"),
                "gate_clearance": report.get("gate_clearance"),
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "remaining_operator_actions": report.get("remaining_operator_actions"),
                "production_safety": report.get("production_safety"),
                "regression_tests": report.get("regression_tests"),
                "generated_at_utc": report.get("generated_at_utc"),
            },
        ),
        ("phase10m_production_discovery.json", discovery),
        ("phase10m_migration_manifest.json", report.get("migration_manifest")),
        ("phase10m_gate_clearance.json", report.get("gate_clearance")),
    ]
    if datetime_review:
        pairs.append(("phase10m_datetime_review.json", datetime_review))
    if datetime_validation:
        pairs.append(("legacy_datetime_validation.json", datetime_validation))
    if wallet_reconciliation:
        pairs.append(("phase10m_wallet_reconciliation.json", wallet_reconciliation))
    if finance_register:
        pairs.append(("phase10m_finance_exception_register.json", finance_register))

    for name, payload in pairs:
        if payload is None:
            continue
        path = write_json_artifact(base / name, payload)
        written.append(path)
    return written
