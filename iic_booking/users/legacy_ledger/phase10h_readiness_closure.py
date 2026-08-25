"""Phase 10H — production blocker closure with real MySQL evidence (READ-ONLY)."""

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
    build_phase10g_final_readiness,
    build_release_audit,
    build_schema_readiness,
    write_json_artifact,
)

ARTIFACT_DIR = Path("docs/release/migration")

PRODUCTION_BASELINE_SHA = "6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b"


def _probe_mysql() -> dict[str, Any]:
    try:
        from iic_booking.users.legacy_ledger.reader import OldMySQLReader

        with OldMySQLReader() as reader:
            probe = reader.connection_probe()
            audit = reader.live_financial_audit()
        return {
            "ok": bool(probe.get("ok")),
            "server_version": probe.get("server_version"),
            "database": probe.get("database"),
            "username_reported": probe.get("username_reported"),
            "account_appears_writable": probe.get("account_appears_writable"),
            "mysql_read_only_flag": probe.get("mysql_read_only_flag"),
            "configured_host": probe.get("configured_host"),
            "configured_port": probe.get("configured_port"),
            "row_counts": probe.get("row_counts"),
            "wallet_transaction_id_range": probe.get("wallet_transaction_id_range"),
            "live_financial_audit": {
                k: audit.get(k)
                for k in (
                    "users_total",
                    "users_with_employee_id",
                    "users_without_employee_id",
                    "duplicate_employee_id_groups",
                    "duplicate_employee_id_rows",
                    "wallet_count",
                    "transaction_count",
                    "calculated_closing_balance",
                    "sum_user_wallet_balance_column",
                    "outlier_abs_gt_10m",
                )
            },
            "writes": 0,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "writes": 0}


def _datetime_validation() -> dict[str, Any]:
    try:
        from iic_booking.users.legacy_ledger.legacy_datetime_validation import (
            validate_legacy_datetime_readonly,
        )

        return validate_legacy_datetime_readonly()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def build_phase10h_final_readiness(
    *,
    backup_verified: bool = False,
    mysql_probe: dict[str, Any] | None = None,
    datetime_validation: dict[str, Any] | None = None,
    production_migrate_plan: dict[str, Any] | None = None,
    explicit_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authoritative Phase 10H GO/NO-GO with real MySQL evidence when available."""
    base = build_phase10g_final_readiness(backup_verified=backup_verified)
    mysql_probe = mysql_probe if mysql_probe is not None else _probe_mysql()
    datetime_validation = (
        datetime_validation if datetime_validation is not None else _datetime_validation()
    )
    evidence = explicit_evidence or {}

    matrix = dict(base.get("gate_matrix") or {})

    # LEGACY MYSQL — upgrade with real connectivity evidence
    if mysql_probe.get("ok"):
        matrix["Legacy MySQL"] = {
            "result": GATE_PASS,
            "evidence": (
                f"ok=True; host={mysql_probe.get('configured_host')}:{mysql_probe.get('configured_port')}; "
                f"db={mysql_probe.get('database')}; user={mysql_probe.get('username_reported')}; "
                f"writable={mysql_probe.get('account_appears_writable')}; "
                f"counts={mysql_probe.get('row_counts')}"
            ),
            "blocking": True,
        }
    else:
        matrix["Legacy MySQL"] = {
            "result": GATE_BLOCKED,
            "evidence": mysql_probe.get("error") or "mysql_unreachable",
            "blocking": True,
        }

    # DATETIME validation evidence (approval still separate)
    if datetime_validation.get("ok"):
        totals = datetime_validation.get("totals") or {}
        matrix["Datetime"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"validation=PASS; contract={datetime_validation.get('contract_approval_status')}; "
                f"totals={totals}; approval_called=False; discovery still gated"
            ),
            "blocking": True,
        }
        datetime_status = "OPERATOR_REQUIRED"
        datetime_validation_status = "PASS"
    else:
        matrix["Datetime"] = {
            "result": GATE_BLOCKED,
            "evidence": datetime_validation.get("error") or "validation_failed",
            "blocking": True,
        }
        datetime_status = "OPERATOR_REQUIRED"
        datetime_validation_status = "BLOCKED"

    # Upcoming bookings remain blocked until datetime approved
    from iic_booking.users.legacy_ledger.datetime_contract import (
        contract_approval_status,
        load_datetime_contract,
    )

    contract = load_datetime_contract()
    approved = contract_approval_status(contract) == "APPROVED"
    if not approved:
        matrix["Upcoming bookings"] = {
            "result": GATE_BLOCKED,
            "evidence": "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL",
            "blocking": True,
        }
    # else leave 10G discovery result

    # Wallets — partial evidence from live financial audit when MySQL ok
    if mysql_probe.get("ok"):
        audit = mysql_probe.get("live_financial_audit") or {}
        ledger = audit.get("calculated_closing_balance")
        stored = audit.get("sum_user_wallet_balance_column")
        gap = None
        if ledger is not None and stored is not None:
            try:
                gap = float(ledger) - float(stored)
            except (TypeError, ValueError):
                gap = None
        matrix["Wallets"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"RO audit txn={audit.get('transaction_count')}; wallets={audit.get('wallet_count')}; "
                f"ledger={ledger}; stored={stored}; gap={gap}; finance review required"
            ),
            "blocking": True,
        }
        matrix["Finance"] = {
            "result": GATE_OPERATOR,
            "evidence": (
                f"duplicate_emp_id_groups={audit.get('duplicate_employee_id_groups')}; "
                f"outlier_abs_gt_10m={audit.get('outlier_abs_gt_10m')}; gap={gap}; "
                "no auto-correction"
            ),
            "blocking": True,
        }
        matrix["Users"] = {
            "result": GATE_WARN,
            "evidence": (
                f"legacy users={audit.get('users_total')}; with_emp_id={audit.get('users_with_employee_id')}; "
                f"without={audit.get('users_without_employee_id')}; "
                f"dup_groups={audit.get('duplicate_employee_id_groups')}; "
                "USER UNRESOLVED does not block T0 when equipment+time valid"
            ),
            "blocking": False,
        }

    # Backup remains blocked unless operator verified
    if not backup_verified:
        matrix["Backup"] = {
            "result": GATE_BLOCKED,
            "evidence": "RDS DescribeDBSnapshots AccessDenied / backup_verified=False",
            "blocking": True,
        }

    # Schema: production migrate --plan evidence (0101-0103 on prod; 0104 needs code deploy)
    if production_migrate_plan:
        matrix["Schema"] = {
            "result": GATE_OPERATOR,
            "evidence": json.dumps(production_migrate_plan, default=str)[:500],
            "blocking": True,
        }

    blockers = [
        name
        for name, g in matrix.items()
        if g.get("blocking") and g.get("result") in (GATE_BLOCKED, GATE_OPERATOR)
        and name != "T0 authorization"
    ]
    # Always keep T0 auth as operator required
    if matrix.get("T0 authorization", {}).get("result") != GATE_PASS:
        blockers.append("T0 authorization")

    tech_ok = all(
        g.get("result") == GATE_PASS
        for name, g in matrix.items()
        if g.get("blocking") and name != "T0 authorization"
    )
    verdict = VERDICT_READY if tech_ok else VERDICT_NOT_READY

    return {
        "phase": "10H",
        "audit_mode": "READ_ONLY",
        "generated_at_utc": datetime.now(tz=dt_timezone.utc).isoformat(),
        "verdict": verdict,
        "t0_executed": False,
        "explicit_t0_authorization_required": True,
        "production_baseline_sha": PRODUCTION_BASELINE_SHA,
        "legacy_mysql": mysql_probe,
        "datetime_validation_status": datetime_validation_status,
        "datetime_contract_status": datetime_status,
        "datetime_validation": {
            "ok": datetime_validation.get("ok"),
            "contract_approval_status": datetime_validation.get("contract_approval_status"),
            "totals": datetime_validation.get("totals"),
            "migration_window": datetime_validation.get("migration_window"),
            "suspicious_threshold_minutes": datetime_validation.get("suspicious_threshold_minutes"),
            "suspicious_bucket_count": len(datetime_validation.get("suspicious_durations") or []),
        },
        "discovery_status": (
            "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL" if not approved else "READY_FOR_DISCOVERY"
        ),
        "production_migrate_plan": production_migrate_plan
        or {
            "applied": ["0096", "0097", "0098", "0099", "0100"],
            "pending_on_production_image": ["0101", "0102", "0103"],
            "pending_after_10d_deploy": ["0104"],
            "migrate_executed": False,
        },
        "equipment_known": {
            "legacy_ids_in_booking_table": evidence.get("legacy_equipment_ids", 45),
            "explicit_mappings": 0,
            "fuzzy_matching": False,
            "note": "Eligible-window required set unknown until datetime APPROVED + discovery",
        },
        "gate_matrix": matrix,
        "blockers": sorted(set(blockers)),
        "phase10g_embedded_verdict": base.get("verdict"),
        "release_audit": build_release_audit(),
        "schema_readiness": build_schema_readiness(),
        "app_rollback_note": "APP ROLLBACK != DATABASE ROLLBACK",
        "architecture_invariant": (
            "USER UNRESOLVED + VALID DATETIME + VALID EQUIPMENT MAPPING = READY/BLOCKABLE"
        ),
        "production_safety": {
            "PRODUCTION_MIGRATE": "NO",
            "T0": "NO",
            "BOOKING_BLOCK": "NO",
            "OLD_PORTAL_FREEZE": "NO",
            "EMAILS_SENT": "NO",
            "REFUNDS": "NO",
            "CLEANUP": "NO",
            "LEGACY_MYSQL_WRITES": "NO",
            "PRODUCTION_WALLET_WRITES": "NO",
            "PRODUCTION_BOOKING_WRITES": "NO",
            "PRODUCTION_USER_WRITES": "NO",
        },
        "production_writes_performed": [],
        "mysql_connectivity_path": {
            "production_django": "OLD_MYSQL_HOST=host.docker.internal:3306 on EC2 (private)",
            "staging_local": "SSH -L 127.0.0.1:13306:127.0.0.1:3306 ubuntu@EC2 → host.docker.internal:13306",
            "public_3306": "NOT opened",
        },
        "operator_next_actions": [
            "Keep SSH tunnel alive for staging probes OR use production django for RO SELECT",
            "Commit/PR/tag/deploy Phase 10D–10H (0104 included)",
            "Verify RDS backup (console/authorized IAM) → --backup-verified",
            "migrate --plan then explicit MIGRATE for 0101–0104 after deploy",
            "Main Admin approve datetime contract (confirm=true) after reviewing legacy_datetime_validation.json",
            "Run migration_discover_legacy_upcoming / qualification (READ-ONLY)",
            "Complete explicit equipment mappings for eligible-window IDs",
            "Finance review of wallet gap / outliers (no auto-correct)",
            "Test-account + email dry-runs on production",
            "Separate explicit T0 authorization",
        ],
    }


def write_phase10h_artifacts(report: dict[str, Any], *, datetime_validation: dict[str, Any]) -> list[str]:
    base = Path(getattr(settings, "BASE_DIR", ".")) / ARTIFACT_DIR
    written: list[str] = []
    pairs = [
        ("phase10h_final_readiness.json", report),
        (
            "phase10h_go_no_go.json",
            {
                "verdict": report["verdict"],
                "t0_executed": False,
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "production_safety": report.get("production_safety"),
                "datetime_validation_status": report.get("datetime_validation_status"),
                "discovery_status": report.get("discovery_status"),
                "generated_at_utc": report.get("generated_at_utc"),
            },
        ),
        (
            "datetime_operator_review.json",
            {
                "DATETIME_CONTRACT": "OPERATOR_REQUIRED",
                "DATETIME_VALIDATION": report.get("datetime_validation_status"),
                "approval_endpoint_called": False,
                "candidate": {
                    "start": "booking.booking_date",
                    "duration": "booking.time_required",
                    "unit": "minutes",
                    "end": "booking_date + time_required minutes",
                },
                "totals": (datetime_validation or {}).get("totals"),
                "risks": [
                    "10 null booking_date rows",
                    "31 zero-duration rows",
                    "suspicious durations >24h present (not auto-rejected)",
                    "migration window not configured on PortalMigrationState",
                ],
                "note": "Main Admin must POST approve with confirm=true + reason. Does not activate T0.",
            },
        ),
        (
            "production_wallet_reconciliation.json",
            {
                "source": "OldMySQLReader.live_financial_audit READ_ONLY",
                "writes": 0,
                **(report.get("legacy_mysql") or {}),
            },
        ),
        (
            "production_equipment_mapping_candidates.json",
            {
                "legacy_equipment_ids_in_booking": 45,
                "new_equipment_known": 64,
                "explicit_mappings": 0,
                "fuzzy_matching": False,
                "status": "OPERATOR REQUIRED — eligible-window set after datetime approval + discovery",
                "classifications_pending": True,
            },
        ),
        (
            "finance_exception_register.json",
            {
                "auto_correction": False,
                "finance_exceptions_blocking": "OPERATOR_REVIEW",
                "notes": [
                    "Prior AI29/AI30 evidence: wallet balance column vs ledger gap; poison reversal pair; outliers",
                    "Reconfirm with current live_financial_audit before T0",
                ],
                "current_audit": (report.get("legacy_mysql") or {}).get("live_financial_audit"),
            },
        ),
        (
            "production_backup_readiness.json",
            {
                "backup_verified": False,
                "status": "BLOCKED",
                "evidence": "EC2 IAM AccessDenied for rds:DescribeDBInstances/DescribeDBSnapshots",
                "operator_action": "Verify snapshot in AWS console or grant read-only RDS describe; then --backup-verified",
                "t0_refuses_without_backup": True,
            },
        ),
        (
            "production_upcoming_bookings.json",
            {
                "status": "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL",
                "writes": 0,
            },
        ),
        (
            "production_user_mapping.json",
            {
                "identity_key": "Channel-I emp_id only",
                "forbidden_keys": ["email", "name", "phone", "username"],
                "legacy_users_total": ((report.get("legacy_mysql") or {}).get("live_financial_audit") or {}).get(
                    "users_total"
                ),
                "users_with_employee_id": ((report.get("legacy_mysql") or {}).get("live_financial_audit") or {}).get(
                    "users_with_employee_id"
                ),
                "duplicate_employee_id_groups": ((report.get("legacy_mysql") or {}).get("live_financial_audit") or {}).get(
                    "duplicate_employee_id_groups"
                ),
                "user_unresolved_blocks_t0": False,
                "import_executed": False,
            },
        ),
        (
            "phase10h_schema_audit.json",
            report.get("production_migrate_plan"),
        ),
        (
            "production_release_plan.json",
            {
                "production_sha": PRODUCTION_BASELINE_SHA,
                "local_backend_sha": (report.get("release_audit") or {}).get("backend", {}).get("local_sha"),
                "local_frontend_sha": "de71188bf3bd69724204c7ac078459e19eb535e0",
                "uncommitted_phases": ["10D", "10E", "10F", "10G", "10H"],
                "deploy_executed": False,
                "push_executed": False,
                "sequence": [
                    "commit/PR/tag backend including users.0104",
                    "commit/PR/tag frontend migration UI",
                    "deploy backend (no auto-migrate)",
                    "deploy frontend",
                    "showmigrations + migrate --plan",
                    "explicit MIGRATE 0101-0104",
                    "datetime approve",
                    "discovery + mappings",
                    "separate T0 authorization",
                ],
            },
        ),
    ]
    for name, payload in pairs:
        written.append(write_json_artifact(base / name, payload))
    # Always refresh datetime validation artifact if ok
    if datetime_validation.get("ok"):
        written.append(write_json_artifact(base / "legacy_datetime_validation.json", datetime_validation))
    return written
