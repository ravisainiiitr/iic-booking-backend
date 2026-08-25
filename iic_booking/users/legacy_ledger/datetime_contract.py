"""Phase 10E/10F — operator datetime contract loader, approval gate, and audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

CONTRACT_REL_PATH = Path("docs/release/migration/legacy_booking_datetime_map.json")
AUDIT_REL_PATH = Path("docs/release/migration/datetime_contract_audit.jsonl")

APPROVAL_OPERATORS_REQUIRED = "OPERATOR_REQUIRED"
APPROVAL_APPROVED = "APPROVED"

# Map contract candidate strategy names to resolver strategy names when approved.
STRATEGY_ALIASES = {
    "CANDIDATE_BOOKING_DATE_DATETIME_PLUS_DURATION": "BOOKING_DATETIME_PLUS_DURATION_MINUTES",
    "BOOKING_DATETIME_PLUS_DURATION_MINUTES": "BOOKING_DATETIME_PLUS_DURATION_MINUTES",
    "DATE_PLUS_DURATION": "DATE_PLUS_DURATION",
}

SEMANTICS_ALIASES = {
    "CANDIDATE_DURATION_MINUTES": "MINUTES",
    "MINUTES": "MINUTES",
}


def default_contract_path() -> Path:
    base = Path(getattr(settings, "BASE_DIR", "."))
    return base / CONTRACT_REL_PATH


def default_audit_path() -> Path:
    base = Path(getattr(settings, "BASE_DIR", "."))
    return base / AUDIT_REL_PATH


def load_datetime_contract(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else default_contract_path()
    if not p.is_file():
        return {
            "ok": False,
            "path": str(p),
            "error": "contract_file_not_found",
            "approval_status": APPROVAL_OPERATORS_REQUIRED,
        }
    data = json.loads(p.read_text(encoding="utf-8"))
    data["_path"] = str(p)
    data["ok"] = True
    return data


def contract_approval_status(contract: dict[str, Any]) -> str:
    explicit = (contract.get("_status") or contract.get("approval_status") or "").strip().upper()
    if explicit == APPROVAL_APPROVED:
        return APPROVAL_APPROVED
    if contract.get("approved_by") and contract.get("approved_at_utc"):
        return APPROVAL_APPROVED
    return APPROVAL_OPERATORS_REQUIRED


def contract_blocks_discovery(contract: dict[str, Any]) -> bool:
    return contract_approval_status(contract) != APPROVAL_APPROVED


def contract_blocks_t0(contract: dict[str, Any]) -> bool:
    return contract_blocks_discovery(contract)


def operator_map_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Translate operator contract JSON to legacy_booking_mysql operator_map keys."""
    if contract_approval_status(contract) != APPROVAL_APPROVED:
        return {}

    booking_date_col = contract.get("booking_date_column") or contract.get("booking_date")
    duration_col = contract.get("duration_column")
    strategy_raw = contract.get("datetime_strategy") or contract.get("approved_strategy") or ""
    strategy = STRATEGY_ALIASES.get(strategy_raw, strategy_raw)
    semantics_raw = contract.get("time_required_semantics") or ""
    semantics = SEMANTICS_ALIASES.get(semantics_raw, semantics_raw)

    op: dict[str, Any] = {
        "booking_id": contract.get("booking_id", "id"),
        "user_id": contract.get("user_id", "user_id"),
        "equipment_id": contract.get("equipment_id", "equipment_id"),
        "status": contract.get("status_column") or contract.get("status", "status"),
        "amount": contract.get("amount_column") or contract.get("amount", "charge"),
        "duration_column": duration_col,
        "datetime_strategy": strategy,
        "time_required_semantics": semantics,
        "timezone": contract.get("timezone"),
        "status_mapping": contract.get("status_mapping"),
        "cancellation_mapping": contract.get("cancellation_mapping"),
        "completion_mapping": contract.get("completion_mapping"),
    }
    if booking_date_col:
        op["booking_date"] = booking_date_col
    return {k: v for k, v in op.items() if v not in (None, "")}


def _validation_summary_payload(validation_report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not validation_report:
        return None
    totals = validation_report.get("totals") or {}
    suspicious = validation_report.get("suspicious_durations") or []
    return {
        "mysql_ok": validation_report.get("ok"),
        "mysql_error": validation_report.get("error"),
        "total_bookings": totals.get("total_bookings"),
        "null_booking_date": totals.get("null_booking_date"),
        "null_time_required": totals.get("null_time_required"),
        "negative_time_required": totals.get("negative_time_required"),
        "zero_duration": totals.get("zero_duration"),
        "extremely_large_duration": totals.get("extremely_large_duration"),
        "bookings_crossing_midnight": totals.get("bookings_crossing_midnight"),
        "bookings_in_migration_window": totals.get("bookings_in_migration_window"),
        "bookings_outside_migration_window": totals.get("bookings_outside_migration_window"),
        "suspicious_duration_buckets": len(suspicious),
        "suspicious_durations_sample": suspicious[:10],
        "duration_distribution_top": (validation_report.get("duration_distribution_top") or [])[:10],
        "audit_mode": "READ_ONLY",
    }


def datetime_contract_ui_payload(
    contract: dict[str, Any] | None = None,
    *,
    validation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = contract or load_datetime_contract()
    status = contract_approval_status(contract)
    strategy_raw = contract.get("approved_strategy") or contract.get("datetime_strategy") or "CANDIDATE_BOOKING_DATE_DATETIME_PLUS_DURATION"
    strategy = STRATEGY_ALIASES.get(strategy_raw, strategy_raw)
    duration_unit = "minutes"
    semantics = contract.get("time_required_semantics") or "CANDIDATE_DURATION_MINUTES"
    if semantics in ("CANDIDATE_DURATION_MINUTES", "MINUTES"):
        duration_unit = "minutes"
    return {
        "approval_status": status,
        "blocks_discovery": contract_blocks_discovery(contract),
        "blocks_t0": contract_blocks_t0(contract),
        "booking_datetime_source": contract.get("booking_date_column") or "booking_date",
        "duration_source": contract.get("duration_column") or "time_required",
        "duration_unit": duration_unit,
        "datetime_strategy": strategy,
        "derived_end": "start + duration",
        "derived_end_formula": "booking_date + time_required minutes",
        "timezone": contract.get("timezone") or "Asia/Kolkata",
        "approved_by": contract.get("approved_by"),
        "approved_at_utc": contract.get("approved_at_utc"),
        "approved_strategy": contract.get("approved_strategy") or strategy,
        "approval_reason": contract.get("approval_reason"),
        "contract_path": contract.get("_path") or str(default_contract_path()),
        "validation_summary": _validation_summary_payload(validation_report),
        "operator_action": (
            "Main Administrator must POST approve with confirm=true after reviewing validation summary."
            if status != APPROVAL_APPROVED
            else "Contract approved for discovery. Does not activate T0 or create blocks."
        ),
        "approval_requires": {
            "confirm": True,
            "approved_by": "recorded from authenticated Main Administrator",
            "approval_reason": "non-empty operator note required",
        },
    }


def validate_contract_for_discovery(contract: dict[str, Any]) -> dict[str, Any]:
    status = contract_approval_status(contract)
    blockers: list[str] = []
    if status != APPROVAL_APPROVED:
        blockers.append("datetime_contract_not_approved")
    semantics = (contract.get("time_required_semantics") or "").upper()
    if semantics in ("", "OPERATOR_REQUIRED", "UNKNOWN", "CANDIDATE_DURATION_MINUTES"):
        if status == APPROVAL_APPROVED and semantics == "CANDIDATE_DURATION_MINUTES":
            pass  # allowed when explicitly approved with candidate semantics documented
        elif status == APPROVAL_APPROVED and semantics in ("MINUTES",):
            pass
        else:
            blockers.append("time_required_semantics_not_approved")
    strategy_raw = contract.get("datetime_strategy") or contract.get("approved_strategy") or ""
    if strategy_raw.startswith("CANDIDATE_") and status != APPROVAL_APPROVED:
        blockers.append("datetime_strategy_candidate_not_approved")
    return {
        "approval_status": status,
        "ready_for_discovery": not blockers,
        "blockers": blockers,
    }


def _append_audit_record(record: dict[str, Any], audit_path: Path | None = None) -> str:
    path = audit_path or default_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return str(path)


def approve_datetime_contract(
    *,
    approved_by: str,
    approval_reason: str,
    confirm: bool,
    approved_strategy: str | None = None,
    path: str | Path | None = None,
    actor_user_id: int | None = None,
    actor_email: str = "",
) -> dict[str, Any]:
    """
    Operator-gated datetime contract approval.
    Updates contract JSON + append-only audit log.
    Does NOT create blocks, freeze portal, migrate bookings, or send email.
    """
    if not confirm:
        return {"ok": False, "error": "confirm_required", "detail": "Pass confirm=true to approve."}
    operator = (approved_by or "").strip()
    reason = (approval_reason or "").strip()
    if not operator:
        return {"ok": False, "error": "approved_by_required"}
    if not reason:
        return {"ok": False, "error": "approval_reason_required"}

    contract_path = Path(path) if path else default_contract_path()
    if not contract_path.is_file():
        return {"ok": False, "error": "contract_file_not_found", "path": str(contract_path)}

    contract = load_datetime_contract(contract_path)
    if contract_approval_status(contract) == APPROVAL_APPROVED:
        return {
            "ok": False,
            "error": "already_approved",
            "approved_by": contract.get("approved_by"),
            "approved_at_utc": contract.get("approved_at_utc"),
        }

    strategy_raw = (approved_strategy or contract.get("datetime_strategy") or "").strip()
    approved_strategy_resolved = STRATEGY_ALIASES.get(strategy_raw, strategy_raw) or "BOOKING_DATETIME_PLUS_DURATION_MINUTES"
    approved_at = datetime.now(tz=dt_timezone.utc).isoformat()

    updated = dict(contract)
    updated["_status"] = APPROVAL_APPROVED
    updated["approved_by"] = operator
    updated["approved_at_utc"] = approved_at
    updated["approved_strategy"] = approved_strategy_resolved
    updated["approval_reason"] = reason
    updated["datetime_strategy"] = approved_strategy_resolved
    updated["time_required_semantics"] = "MINUTES"
    for key in ("ok", "_path"):
        updated.pop(key, None)

    backup_path = contract_path.with_suffix(
        contract_path.suffix + f".bak-{datetime.now(tz=dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    backup_path.write_text(contract_path.read_text(encoding="utf-8"), encoding="utf-8")
    contract_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")

    audit_path = _append_audit_record(
        {
            "event": "DATETIME_CONTRACT_APPROVED",
            "approved_by": operator,
            "approved_at_utc": approved_at,
            "approved_strategy": approved_strategy_resolved,
            "approval_reason": reason,
            "actor_user_id": actor_user_id,
            "actor_email": actor_email,
            "contract_path": str(contract_path),
            "backup_path": str(backup_path),
            "note": "Approval enables discovery only — not T0, blocks, freeze, or email.",
        }
    )

    reloaded = load_datetime_contract(contract_path)
    return {
        "ok": True,
        "approval_status": APPROVAL_APPROVED,
        "approved_by": operator,
        "approved_at_utc": approved_at,
        "approved_strategy": approved_strategy_resolved,
        "approval_reason": reason,
        "backup_path": str(backup_path),
        "audit_path": audit_path,
        "contract": datetime_contract_ui_payload(reloaded),
        "side_effects": {
            "blocks_created": 0,
            "emails_sent": 0,
            "portal_frozen": False,
            "t0_activated": False,
        },
    }

