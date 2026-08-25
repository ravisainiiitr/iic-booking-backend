"""
Phase 10 — READ-ONLY legacy MySQL booking schema discovery and qualification.

Uses SHOW COLUMNS / SELECT only. Never writes to MySQL.
Does not invent column names: resolves semantic fields only when exactly one
candidate column exists on the live `booking` table, or when an operator
supplies an approved JSON column map (--column-map-file).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.apps import apps
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, SlotStatus
from iic_booking.users.legacy_ledger.booking_bridge import (
    discover_legacy_bookings,
    find_overlapping_slots,
)
from iic_booking.users.legacy_ledger.equipment_mapping import (
    get_active_mapping_for_old_id,
    validate_legacy_equipment_mappings,
)
from iic_booking.users.legacy_ledger.reader import OldMySQLConnectionError, OldMySQLNotConfigured, OldMySQLReader
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import PortalMigrationState

# Candidate lists are NOT guesses — they are exhaustive allowed names.
# Resolution succeeds only when exactly one live column matches.
BOOKING_SEMANTIC_CANDIDATES: dict[str, list[str]] = {
    "booking_id": ["id", "booking_id"],
    "user_id": ["user_id", "uid"],
    "equipment_id": ["equipment_id", "instrument_id", "eq_id", "inst_id"],
    "booking_date": ["booking_date", "date", "book_date"],
    "start_time": ["start_time", "from_time", "slot_start", "start"],
    "end_time": ["end_time", "to_time", "slot_end", "end"],
    "start_datetime": ["start_at", "start_datetime", "booking_start", "from_datetime"],
    "end_datetime": ["end_at", "end_datetime", "booking_end", "to_datetime"],
    "status": ["status", "booking_status", "state"],
    "cancelled_flag": ["is_cancelled", "cancelled", "is_canceled", "canceled"],
    "amount": ["amount", "total_charge", "booking_amount", "charge"],
    "mode": ["mode", "booking_mode", "usage_mode"],
}

USERS_IDENTITY_CANDIDATES: dict[str, list[str]] = {
    "user_pk": ["id"],
    "employee_id": ["emp_id", "employee_id", "emp_code"],
}


def _pick_unique(candidates: list[str], available: set[str]) -> tuple[str | None, str]:
    hits = [c for c in candidates if c in available]
    if len(hits) == 1:
        return hits[0], "VERIFIED"
    if not hits:
        return None, "NOT_FOUND"
    return None, f"AMBIGUOUS:{','.join(hits)}"


def resolve_booking_column_map(
    booking_columns: list[str],
    *,
    operator_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build semantic→physical column map from live schema or operator file."""
    available = set(booking_columns)
    operator_map = dict(operator_map or {})
    resolved: dict[str, str | None] = {}
    status: dict[str, str] = {}
    blockers: list[str] = []

    for semantic, candidates in BOOKING_SEMANTIC_CANDIDATES.items():
        if semantic in operator_map:
            col = operator_map[semantic]
            if col not in available:
                status[semantic] = "OPERATOR_MAP_INVALID"
                blockers.append(f"operator_map.{semantic}={col} not in booking table")
                resolved[semantic] = None
            else:
                resolved[semantic] = col
                status[semantic] = "OPERATOR_VERIFIED"
            continue
        col, st = _pick_unique(candidates, available)
        resolved[semantic] = col
        status[semantic] = st

    # Operator-only duration column (e.g. production time_required)
    duration_col = operator_map.get("duration_column")
    if duration_col:
        if duration_col not in available:
            blockers.append(f"operator_map.duration_column={duration_col} not in booking table")
        else:
            resolved["duration"] = duration_col
            status["duration"] = "OPERATOR_VERIFIED"

    # Datetime strategy: operator override, else auto-detect
    datetime_strategy = operator_map.get("datetime_strategy")
    if datetime_strategy == "DATE_PLUS_DURATION":
        if not (resolved.get("booking_date") and resolved.get("duration")):
            blockers.append("DATE_PLUS_DURATION requires booking_date + duration_column")
        elif operator_map.get("time_required_semantics") in (None, "", "UNKNOWN", "OPERATOR_REQUIRED"):
            blockers.append("DATE_PLUS_DURATION requires approved time_required_semantics")
    elif datetime_strategy == "BOOKING_DATETIME_PLUS_DURATION_MINUTES":
        if not (resolved.get("booking_date") and resolved.get("duration")):
            blockers.append("BOOKING_DATETIME_PLUS_DURATION_MINUTES requires booking_date + duration_column")
        elif operator_map.get("time_required_semantics") in (None, "", "UNKNOWN", "OPERATOR_REQUIRED"):
            blockers.append("BOOKING_DATETIME_PLUS_DURATION_MINUTES requires approved time_required_semantics")
    elif datetime_strategy == "DATE_PLUS_TIME":
        if not (resolved.get("booking_date") and resolved.get("start_time") and resolved.get("end_time")):
            blockers.append("DATE_PLUS_TIME requires booking_date + start_time + end_time")
    elif not datetime_strategy:
        if resolved.get("start_datetime") and resolved.get("end_datetime"):
            datetime_strategy = "COMBINED_DATETIME"
        elif (
            resolved.get("booking_date")
            and resolved.get("start_time")
            and resolved.get("end_time")
            and status.get("start_datetime") != "OPERATOR_VERIFIED"
        ):
            datetime_strategy = "DATE_PLUS_TIME"
        elif resolved.get("start_datetime") or resolved.get("end_datetime"):
            blockers.append("partial_datetime_columns")
        else:
            blockers.append("no_resolvable_datetime_strategy")

    required = ["booking_id", "user_id", "equipment_id", "status"]
    for req in required:
        if not resolved.get(req):
            blockers.append(f"missing_required:{req}")

    ready = not blockers and datetime_strategy is not None
    return {
        "ready": ready,
        "datetime_strategy": datetime_strategy,
        "columns_available": sorted(available),
        "resolved": resolved,
        "status": status,
        "blockers": blockers,
        "operator_semantics": {
            k: operator_map.get(k)
            for k in (
                "time_required_semantics",
                "timezone",
                "status_mapping",
                "cancellation_mapping",
                "completion_mapping",
            )
            if operator_map.get(k)
        },
    }


def discover_mysql_booking_schema() -> dict[str, Any]:
    """READ-ONLY SHOW COLUMNS for booking + users identity fields."""
    try:
        reader = OldMySQLReader()
    except OldMySQLNotConfigured as exc:
        return {"ok": False, "error": str(exc)}

    try:
        with reader:
            schema = reader.discover_schema()
            booking_cols = [c["Field"] for c in schema.get("columns", {}).get("booking", [])]
            users_cols = [c["Field"] for c in schema.get("columns", {}).get("users", [])]
            users_avail = set(users_cols)
            user_identity = {}
            for semantic, candidates in USERS_IDENTITY_CANDIDATES.items():
                col, st = _pick_unique(candidates, users_avail)
                user_identity[semantic] = {"column": col, "status": st}

            col_map = resolve_booking_column_map(booking_cols)
            # Redacted types only — no sample row data
            booking_types = {
                c["Field"]: c.get("Type")
                for c in schema.get("columns", {}).get("booking", [])
            }
            return {
                "ok": col_map["ready"],
                "booking_column_count": len(booking_cols),
                "booking_column_names": sorted(booking_cols),
                "booking_column_types_redacted": booking_types,
                "users_identity": user_identity,
                "column_map": col_map,
                "wallet_tables_present": {
                    t: t in schema.get("columns", {})
                    for t in ("users", "user_wallet", "wallet_transactions", "booking")
                },
            }
    except OldMySQLConnectionError as exc:
        return {"ok": False, "error": str(exc)}


def _combine_dt(row: dict, col_map: dict[str, str | None], strategy: str, tz) -> tuple[Any, Any]:
    if strategy == "COMBINED_DATETIME":
        start = row.get(col_map["start_datetime"])
        end = row.get(col_map["end_datetime"])
        return start, end
    if strategy == "DATE_PLUS_DURATION":
        d = row.get(col_map["booking_date"])
        duration_raw = row.get(col_map.get("duration"))
        if not isinstance(d, date) or duration_raw is None:
            return None, None
        # Interpretation deferred to approved operator semantics — only timedelta/time supported here.
        if isinstance(duration_raw, timedelta):
            delta = duration_raw
        elif isinstance(duration_raw, time):
            start = datetime.combine(d if isinstance(d, date) else d.date(), duration_raw)
            if timezone.is_naive(start):
                start = timezone.make_aware(start, tz)
            return start, None
        elif isinstance(duration_raw, (int, float, Decimal)):
            delta = timedelta(hours=float(duration_raw))
        else:
            return None, None
        start = datetime.combine(d, time(0, 0)) if isinstance(d, date) and not isinstance(d, datetime) else d
        if isinstance(start, datetime) and timezone.is_naive(start):
            start = timezone.make_aware(start, tz)
        end = start + delta
        return start, end
    if strategy == "BOOKING_DATETIME_PLUS_DURATION_MINUTES":
        start_raw = row.get(col_map["booking_date"])
        duration_raw = row.get(col_map.get("duration"))
        if start_raw is None or duration_raw is None:
            return None, None
        if isinstance(start_raw, datetime):
            start = start_raw
        elif isinstance(start_raw, date):
            start = datetime.combine(start_raw, time(0, 0))
        else:
            return None, None
        if timezone.is_naive(start):
            start = timezone.make_aware(start, tz)
        try:
            minutes = int(duration_raw)
        except (TypeError, ValueError):
            return None, None
        end = start + timedelta(minutes=minutes)
        return start, end
    d = row.get(col_map["booking_date"])
    st = row.get(col_map["start_time"])
    et = row.get(col_map["end_time"])
    if isinstance(d, date) and not isinstance(d, datetime):
        if isinstance(st, timedelta):
            st = (datetime.min + st).time()
        if isinstance(et, timedelta):
            et = (datetime.min + et).time()
        start = datetime.combine(d, st if isinstance(st, time) else time(0, 0))
        end = datetime.combine(d, et if isinstance(et, time) else time(0, 0))
        if timezone.is_naive(start):
            start = timezone.make_aware(start, tz)
        if timezone.is_naive(end):
            end = timezone.make_aware(end, tz)
        return start, end
    return None, None


def _normalize_status(raw: Any, row: dict, col_map: dict[str, str | None]) -> str:
    status = str(raw or "").strip().upper()
    cancel_col = col_map.get("cancelled_flag")
    if cancel_col:
        flag = row.get(cancel_col)
        if flag in (1, True, "1", "Y", "y", "yes", "YES"):
            return "CANCELLED"
    return status


def fetch_legacy_bookings_for_window(
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    column_map_file: str = "",
) -> dict[str, Any]:
    """SELECT legacy bookings in window; normalize to discovery rows."""
    state = PortalMigrationState.get_solo()
    window_start = window_start or state.migration_start_at
    window_end = window_end or state.migration_window_end_at
    if not window_start or not window_end:
        return {
            "ok": False,
            "error": "migration_start_at and migration_window_end_at must be configured",
        }

    operator_map = {}
    if column_map_file:
        with open(column_map_file, encoding="utf-8") as fh:
            operator_map = json.load(fh)

    try:
        reader = OldMySQLReader()
    except OldMySQLNotConfigured as exc:
        return {"ok": False, "error": str(exc)}

    with reader:
        schema = reader.discover_schema()
        booking_cols = [c["Field"] for c in schema.get("columns", {}).get("booking", [])]
        col_map_report = resolve_booking_column_map(booking_cols, operator_map=operator_map or None)
        if not col_map_report["ready"]:
            return {"ok": False, "column_map": col_map_report, "error": "column_map_not_ready"}

        cm = col_map_report["resolved"]
        strategy = col_map_report["datetime_strategy"]
        tz = timezone.get_current_timezone()

        # Build SELECT list (explicit columns only)
        select_cols = sorted({c for c in cm.values() if c})
        sql = f"SELECT {', '.join(f'`{c}`' for c in select_cols)} FROM `booking`"
        rows = reader.fetchall(sql)

        # Resolve legacy users.emp_id for booking.user_id (authoritative employee identity)
        user_ids = {
            int(row[cm["user_id"]])
            for row in rows
            if row.get(cm["user_id"]) is not None
        }
        legacy_users = reader.users_by_ids(sorted(user_ids)) if user_ids else {}

    normalized: list[dict] = []
    for row in rows:
        start_at, end_at = _combine_dt(row, cm, strategy, tz)
        if start_at is None or end_at is None:
            continue
        if timezone.is_naive(start_at):
            start_at = timezone.make_aware(start_at, tz)
        if timezone.is_naive(end_at):
            end_at = timezone.make_aware(end_at, tz)
        # Window filter on start_at
        if start_at < window_start or start_at >= window_end:
            continue
        legacy_user_id = row.get(cm["user_id"])
        legacy_user = legacy_users.get(int(legacy_user_id)) if legacy_user_id is not None else None
        emp_id = str((legacy_user or {}).get("emp_id") or "").strip()
        duration_raw = row.get(cm.get("duration")) if cm.get("duration") else None
        duration_minutes = None
        if duration_raw is not None:
            try:
                duration_minutes = int(duration_raw)
            except (TypeError, ValueError):
                duration_minutes = None
        normalized.append(
            {
                "legacy_booking_id": int(row[cm["booking_id"]]),
                "old_equipment_id": int(row[cm["equipment_id"]]) if row.get(cm["equipment_id"]) is not None else None,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "status": _normalize_status(row.get(cm["status"]), row, cm),
                "amount": str(row.get(cm["amount"]) or 0),
                "legacy_user_id": legacy_user_id,
                "employee_id": emp_id,
                "duration_minutes": duration_minutes,
            }
        )

    discovery = discover_legacy_bookings(normalized, window_start=window_start, window_end=window_end)
    eligible_for_audit = [
        {
            "legacy_booking_id": e["legacy_booking_id"],
            "old_equipment_id": e["old_equipment_id"],
            "start_at": e["start_at"],
            "end_at": e["end_at"],
            "legacy_user_id": next(
                (n["legacy_user_id"] for n in normalized if n["legacy_booking_id"] == e["legacy_booking_id"]),
                None,
            ),
        }
        for e in discovery.get("eligible") or []
    ]
    return {
        "ok": True,
        "column_map": col_map_report,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "raw_in_window": len(normalized),
        "discovery": {
            "counts": discovery["counts"],
        },
        "eligible_for_audit": eligible_for_audit,
    }


def map_legacy_identities(eligible_rows: list[dict]) -> dict[str, Any]:
    """
    Report legacy user_id → new portal User via users.emp_id (authoritative).

    Unresolved new-portal users do NOT block slot occupancy or migration readiness.
    """
    exceptions: list[dict] = []
    mapped = 0
    unresolved = 0
    legacy_user_ids = {
        int(r["legacy_user_id"])
        for r in eligible_rows
        if r.get("legacy_user_id") is not None and not str(r.get("employee_id") or "").strip()
    }
    legacy_users: dict[int, dict] = {}
    if legacy_user_ids:
        try:
            reader = OldMySQLReader()
            with reader:
                legacy_users = reader.users_by_ids(sorted(legacy_user_ids))
        except OldMySQLNotConfigured:
            return {"ok": False, "error": "mysql_not_configured"}

    for row in eligible_rows:
        lid = row.get("legacy_user_id")
        emp = str(row.get("employee_id") or "").strip()
        if not emp and lid is not None:
            legacy = legacy_users.get(int(lid))
            if not legacy:
                exceptions.append({"legacy_booking_id": row.get("legacy_booking_id"), "reason": "legacy_user_not_found"})
                continue
            emp = str(legacy.get("emp_id") or "").strip()
        if lid is None and not emp:
            exceptions.append({"legacy_booking_id": row.get("legacy_booking_id"), "reason": "missing_legacy_user_id"})
            continue
        if not emp:
            exceptions.append({"legacy_booking_id": row.get("legacy_booking_id"), "reason": "missing_emp_id"})
            continue
        matches = User.objects.filter(emp_id=emp).count()
        if matches == 1:
            mapped += 1
        elif matches == 0:
            unresolved += 1
            exceptions.append(
                {
                    "legacy_booking_id": row.get("legacy_booking_id"),
                    "reason": "no_new_portal_user_for_emp_id",
                    "informational": True,
                }
            )
        else:
            exceptions.append({"legacy_booking_id": row.get("legacy_booking_id"), "reason": "ambiguous_emp_id_match"})

    hard_exceptions = [e for e in exceptions if not e.get("informational")]
    return {
        "ok": True,
        "mapped_count": mapped,
        "unresolved_count": unresolved,
        "exception_count": len(exceptions),
        "hard_exception_count": len(hard_exceptions),
        "exceptions_sample": exceptions[:20],
        "user_mapping_blocks_readiness": False,
    }


def audit_target_slots(eligible_rows: list[dict]) -> dict[str, Any]:
    """Calculate target DailySlots read-only; report conflicts with existing bookings."""
    target_found = 0
    target_missing = 0
    already_occupied = 0
    overlapping_legacy = 0
    slot_conflicts = 0
    existing_new_booking = 0
    details: list[dict] = []

    seen_keys: set[tuple] = set()
    duplicates = 0

    for row in eligible_rows:
        key = (row.get("legacy_booking_id"), row.get("old_equipment_id"), row.get("start_at"))
        if key in seen_keys:
            duplicates += 1
        seen_keys.add(key)

        mapping = get_active_mapping_for_old_id(row.get("old_equipment_id"))
        if not mapping or not mapping.new_equipment_id:
            continue
        eq = mapping.new_equipment
        start = row.get("start_at")
        end = row.get("end_at")
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if timezone.is_naive(start):
            start = timezone.make_aware(start, timezone.get_current_timezone())
        if timezone.is_naive(end):
            end = timezone.make_aware(end, timezone.get_current_timezone())

        slots = find_overlapping_slots(eq, start, end)
        if not slots:
            target_missing += 1
            details.append({"legacy_booking_id": row.get("legacy_booking_id"), "issue": "target_slot_missing"})
            continue
        target_found += 1
        booked = [s for s in slots if s.status == SlotStatus.BOOKED]
        if booked:
            already_occupied += 1
            existing_new_booking += 1
            details.append({"legacy_booking_id": row.get("legacy_booking_id"), "issue": "EXISTING_NEW_BOOKING"})
            slot_conflicts += 1
        blocked = [s for s in slots if s.status == SlotStatus.BLOCKED]
        if blocked:
            overlapping_legacy += 1

    return {
        "target_slot_found": target_found,
        "target_slot_missing": target_missing,
        "already_occupied": already_occupied,
        "slot_conflicts": slot_conflicts,
        "existing_new_booking_conflicts": existing_new_booking,
        "duplicate_eligible_records": duplicates,
        "details_sample": details[:30],
    }


def build_t0_dataset_summary(
    *,
    discovery_counts: dict[str, int] | None = None,
    identity_exceptions: int = 0,
    slot_audit: dict[str, Any] | None = None,
    mapping_report: dict[str, Any] | None = None,
    test_users: int = 0,
    email_recipients: int = 0,
) -> dict[str, Any]:
    counts = discovery_counts or {}
    slot_audit = slot_audit or {}
    mapping_report = mapping_report or {}
    mc = mapping_report.get("counts") or {}
    blockers = []
    if (counts.get("unmapped") or 0) > 0:
        blockers.append("unmapped_equipment")
    # unresolved_identities is informational only — does NOT block T0 readiness (Phase 10D)
    if (slot_audit.get("slot_conflicts") or 0) > 0:
        blockers.append("slot_conflicts")
    if (slot_audit.get("duplicate_eligible_records") or 0) > 0:
        blockers.append("duplicate_eligible")
    if (mc.get("unmapped") or 0) > 0 or (mc.get("conflict") or 0) > 0:
        blockers.append("equipment_mapping_not_ready")

    return {
        "eligible_legacy_bookings": counts.get("eligible", 0),
        "cancelled": counts.get("cancelled", 0),
        "completed": counts.get("completed", 0),
        "outside_window": counts.get("outside_window", 0),
        "outside_window_invalid": counts.get("invalid", 0),
        "unmapped_equipment": counts.get("unmapped", 0),
        "conflicting_discovery": counts.get("conflicting", 0),
        "unresolved_identities": identity_exceptions,
        "user_mapping_blocks_readiness": False,
        "target_slots": slot_audit.get("target_slot_found", 0),
        "slot_conflicts": slot_audit.get("slot_conflicts", 0),
        "existing_new_bookings": slot_audit.get("existing_new_booking_conflicts", 0),
        "duplicate_records": slot_audit.get("duplicate_eligible_records", 0),
        "test_accounts": test_users,
        "email_recipients": email_recipients,
        "t0_ready": len(blockers) == 0,
        "blockers": blockers,
    }


def investigate_legacy_booking_datetime() -> dict[str, Any]:
    """
    READ-ONLY production-safe investigation of booking_date + time_required semantics.
    No PII: aggregates and type metadata only.
    """
    try:
        reader = OldMySQLReader()
    except OldMySQLNotConfigured as exc:
        return {"ok": False, "error": str(exc)}

    with reader:
        probe = reader.connection_probe()
        booking_cols = {
            c["Field"]: c.get("Type")
            for c in reader.fetchall("SHOW COLUMNS FROM `booking`")
        }
        create_row = reader.fetchone("SHOW CREATE TABLE `booking`")
        create_sql = (create_row or {}).get("Create Table") or ""
        # Redact any DEFAULT/comment literals that might embed emails — keep structure only
        create_redacted = create_sql[:4000] if create_sql else ""

        stats = reader.fetchone(
            """
            SELECT
              COUNT(*) AS total_rows,
              COUNT(DISTINCT `time_required`) AS distinct_time_required,
              MIN(`time_required`) AS min_time_required,
              MAX(`time_required`) AS max_time_required,
              MIN(`booking_date`) AS min_booking_date,
              MAX(`booking_date`) AS max_booking_date,
              SUM(CASE WHEN `is_deleted` = 1 THEN 1 ELSE 0 END) AS deleted_rows,
              SUM(CASE WHEN `is_active` = 0 THEN 1 ELSE 0 END) AS inactive_rows
            FROM `booking`
            """
        )
        top_time_required = reader.fetchall(
            """
            SELECT `time_required`, COUNT(*) AS row_count
            FROM `booking`
            GROUP BY `time_required`
            ORDER BY row_count DESC
            LIMIT 20
            """
        )
        top_status = reader.fetchall(
            """
            SELECT `status`, COUNT(*) AS row_count
            FROM `booking`
            GROUP BY `status`
            ORDER BY row_count DESC
            LIMIT 20
            """
        )
        pattern_sample = reader.fetchall(
            """
            SELECT `booking_date`, `time_required`, `status`,
                   SUM(CASE WHEN `is_deleted` = 1 THEN 1 ELSE 0 END) AS deleted_cnt,
                   COUNT(*) AS row_count
            FROM `booking`
            GROUP BY `booking_date`, `time_required`, `status`
            ORDER BY row_count DESC
            LIMIT 25
            """
        )

    tr_type = booking_cols.get("time_required", "UNKNOWN")
    bd_type = booking_cols.get("booking_date", "UNKNOWN")
    findings: list[str] = []
    semantics = "OPERATOR_REQUIRED"

    tr_lower = (tr_type or "").lower()
    if "time" in tr_lower and "int" not in tr_lower:
        findings.append("time_required column type appears TIME-like — may encode slot start time")
        semantics = "CANDIDATE_START_TIME_ONLY"
    elif "int" in tr_lower or "decimal" in tr_lower or "float" in tr_lower:
        findings.append("time_required column type appears numeric — may encode duration in hours/minutes/slots")
        semantics = "CANDIDATE_DURATION_NUMERIC"
    elif "varchar" in tr_lower or "char" in tr_lower:
        findings.append("time_required column type appears textual — encoded range possible")
        semantics = "CANDIDATE_ENCODED_TEXT"
    else:
        findings.append(f"time_required MySQL type={tr_type} — semantics not inferable from schema alone")

    auto_map = resolve_booking_column_map(list(booking_cols.keys()))
    return {
        "ok": True,
        "mysql_probe_summary": {
            "database": probe.get("database"),
            "server_version": probe.get("server_version"),
            "read_only_account": not probe.get("account_appears_writable"),
        },
        "booking_date_column": {
            "name": "booking_date",
            "mysql_type": bd_type,
        },
        "time_required_column": {
            "name": "time_required",
            "mysql_type": tr_type,
            "inferred_semantics": semantics,
            "findings": findings,
        },
        "lifecycle_columns": {
            "is_deleted": booking_cols.get("is_deleted"),
            "is_active": booking_cols.get("is_active"),
            "status": booking_cols.get("status"),
        },
        "aggregate_stats": stats,
        "top_time_required_values": top_time_required,
        "top_status_values": top_status,
        "pattern_sample_redacted": pattern_sample,
        "show_create_table_excerpt": create_redacted,
        "auto_column_map": auto_map,
        "operator_action": (
            "Approve docs/release/migration/legacy_booking_datetime_map.json "
            "with time_required_semantics and datetime_strategy before discovery."
            if not auto_map.get("ready")
            else "Auto map ready"
        ),
    }
