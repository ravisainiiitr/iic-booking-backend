"""Phase 10E — READ-ONLY upcoming migration window discovery artifact."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings
from iic_booking.users.legacy_ledger.datetime_contract import (
    contract_blocks_discovery,
    load_datetime_contract,
    operator_map_from_contract,
    validate_contract_for_discovery,
)
from iic_booking.users.legacy_ledger.equipment_mapping import get_active_mapping_for_old_id
from iic_booking.users.legacy_ledger.legacy_booking_mysql import (
    fetch_legacy_bookings_for_window,
    resolve_booking_column_map,
)
from iic_booking.users.legacy_ledger.legacy_conflict_analysis import analyze_booking_conflicts
from iic_booking.users.legacy_ledger.legacy_user_resolution import classify_user_mapping_for_row
from iic_booking.users.legacy_ledger.reader import OldMySQLNotConfigured, OldMySQLReader
from iic_booking.users.models.portal_migration import PortalMigrationState


def discover_upcoming_legacy_week(
    *,
    column_map_file: str = "",
    legacy_rows: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Discovery-only report for migration window candidates.
    Never creates LegacyBookingBlock records.
    """
    contract = load_datetime_contract(column_map_file or None)
    contract_gate = validate_contract_for_discovery(contract)
    state = PortalMigrationState.get_solo()

    if legacy_rows is not None:
        discovery = discover_legacy_bookings(legacy_rows)
        source = "fixture_rows"
        mysql_ok = True
    elif contract_blocks_discovery(contract):
        return {
            "ok": False,
            "error": "datetime_contract_operator_required",
            "contract_gate": contract_gate,
            "discovery": {"counts": {}},
            "candidates": [],
            "audit_mode": "READ_ONLY",
        }
    else:
        op_map = operator_map_from_contract(contract)
        # Write temp operator map path if column_map_file not given — use inline fetch
        fetch = fetch_legacy_bookings_for_window(
            window_start=state.migration_start_at,
            window_end=state.migration_window_end_at,
            column_map_file=column_map_file,
        )
        if not fetch.get("ok"):
            # Try inline operator map via extended fetch
            fetch = _fetch_with_operator_map(op_map, state.migration_start_at, state.migration_window_end_at)
        if not fetch.get("ok"):
            return {
                "ok": False,
                "error": fetch.get("error"),
                "contract_gate": contract_gate,
                "audit_mode": "READ_ONLY",
            }
        discovery = fetch.get("discovery_full") or {
            "counts": fetch.get("discovery", {}).get("counts") or {},
        }
        if "eligible" not in discovery:
            discovery = discover_legacy_bookings(_rows_from_fetch(fetch))
        source = "legacy_mysql_readonly"
        mysql_ok = True

    flat = _flatten_discovery(discovery)
    eligible = [r for r in flat if r.get("eligibility") == "eligible"]
    conflict_report = analyze_booking_conflicts(eligible)

    candidates = []
    for row in flat:
        mapping = get_active_mapping_for_old_id(row.get("old_equipment_id")) if row.get("old_equipment_id") else None
        user_map = classify_user_mapping_for_row(
            legacy_employee_id=row.get("legacy_employee_id") or row.get("employee_id"),
            legacy_user_id=row.get("legacy_user_id"),
        )
        enriched = {
            "legacy_booking_id": row.get("legacy_booking_id"),
            "legacy_user_id": row.get("legacy_user_id"),
            "legacy_employee_id": user_map.get("legacy_employee_id") or row.get("legacy_employee_id") or "",
            "legacy_equipment_id": row.get("old_equipment_id"),
            "legacy_booking_start": row.get("start_at"),
            "legacy_booking_end": row.get("end_at"),
            "legacy_status": row.get("status"),
            "charge": row.get("amount"),
            "duration_minutes": row.get("duration_minutes"),
            "new_equipment_id": getattr(mapping.new_equipment, "equipment_id", None) if mapping else row.get("new_equipment_id"),
            "equipment_mapping_status": mapping.status if mapping else "UNMAPPED",
            "user_mapping_status": user_map.get("user_mapping_status"),
            "eligibility": row.get("eligibility"),
            "conflict_status": "NONE",
        }
        for c in conflict_report.get("conflicts") or []:
            if c.get("legacy_booking_id") == enriched["legacy_booking_id"]:
                enriched["conflict_status"] = c.get("conflict_type")
                break
        candidates.append(enriched)

    user_resolved = sum(1 for c in candidates if c.get("user_mapping_status") == "RESOLVED_CHANNEL_I")
    user_unresolved = sum(1 for c in candidates if c.get("user_mapping_status") == "UNRESOLVED" and c.get("eligibility") == "eligible")

    return {
        "ok": True,
        "audit_mode": "READ_ONLY",
        "source": source,
        "mysql_ok": mysql_ok,
        "contract_gate": contract_gate,
        "window_start": state.migration_start_at.isoformat() if state.migration_start_at else None,
        "window_end": state.migration_window_end_at.isoformat() if state.migration_window_end_at else None,
        "discovery_counts": discovery.get("counts") or {},
        "conflict_report": {
            "conflict_count": conflict_report.get("conflict_count"),
            "by_type": conflict_report.get("by_type"),
        },
        "user_resolved_count": user_resolved,
        "user_unresolved_count": user_unresolved,
        "candidates": candidates,
        "blocks_created": 0,
    }


def _flatten_discovery(discovery: dict) -> list[dict]:
    rows = []
    for bucket in (
        "eligible", "unmapped", "conflicting", "cancelled", "completed",
        "outside_window", "invalid", "duplicate",
    ):
        for row in discovery.get(bucket) or []:
            entry = dict(row)
            entry["eligibility"] = entry.get("eligibility") or bucket
            rows.append(entry)
    return rows


def _rows_from_fetch(fetch: dict) -> list[dict]:
    """Reconstruct normalized rows from fetch summary when full discovery not embedded."""
    return fetch.get("normalized_rows") or []


def _fetch_with_operator_map(operator_map: dict, window_start, window_end) -> dict[str, Any]:
    """Inline fetch using approved operator map without temp file."""
    import json
    import tempfile
    import os

    if not operator_map:
        return {"ok": False, "error": "no_operator_map"}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(operator_map, fh)
        result = fetch_legacy_bookings_for_window(
            window_start=window_start,
            window_end=window_end,
            column_map_file=path,
        )
        if result.get("ok"):
            # Re-run full discovery from mysql for normalized rows
            try:
                reader = OldMySQLReader()
            except OldMySQLNotConfigured as exc:
                return {"ok": False, "error": str(exc)}
            with reader:
                schema = reader.discover_schema()
                booking_cols = [c["Field"] for c in schema.get("columns", {}).get("booking", [])]
                col_map_report = resolve_booking_column_map(booking_cols, operator_map=operator_map)
                if not col_map_report["ready"]:
                    return {"ok": False, "error": "column_map_not_ready", "column_map": col_map_report}
                cm = col_map_report["resolved"]
                strategy = col_map_report["datetime_strategy"]
                tz = timezone.get_current_timezone()
                select_cols = sorted({c for c in cm.values() if c})
                sql = f"SELECT {', '.join(f'`{c}`' for c in select_cols)} FROM `booking`"
                raw_rows = reader.fetchall(sql)
                user_ids = {int(r[cm["user_id"]]) for r in raw_rows if r.get(cm["user_id"]) is not None}
                legacy_users = reader.users_by_ids(sorted(user_ids)) if user_ids else {}
            from iic_booking.users.legacy_ledger.legacy_booking_mysql import _combine_dt, _normalize_status

            normalized = []
            for row in raw_rows:
                start_at, end_at = _combine_dt(row, cm, strategy, tz)
                if start_at is None or end_at is None:
                    continue
                if timezone.is_naive(start_at):
                    start_at = timezone.make_aware(start_at, tz)
                if timezone.is_naive(end_at):
                    end_at = timezone.make_aware(end_at, tz)
                if window_start and start_at < window_start:
                    continue
                if window_end and start_at >= window_end:
                    continue
                legacy_user_id = row.get(cm["user_id"])
                legacy_user = legacy_users.get(int(legacy_user_id)) if legacy_user_id is not None else None
                emp_id = str((legacy_user or {}).get("emp_id") or "").strip()
                duration_raw = row.get(cm.get("duration")) if cm.get("duration") else None
                try:
                    duration_minutes = int(duration_raw) if duration_raw is not None else None
                except (TypeError, ValueError):
                    duration_minutes = None
                normalized.append(
                    {
                        "legacy_booking_id": int(row[cm["booking_id"]]),
                        "old_equipment_id": int(row[cm["equipment_id"]]) if row.get(cm["equipment_id"]) is not None else None,
                        "start_at": start_at,
                        "end_at": end_at,
                        "status": _normalize_status(row.get(cm["status"]), row, cm),
                        "amount": str(row.get(cm.get("amount")) or 0),
                        "legacy_user_id": legacy_user_id,
                        "employee_id": emp_id,
                        "duration_minutes": duration_minutes,
                    }
                )
            discovery = discover_legacy_bookings(normalized, window_start=window_start, window_end=window_end)
            result["discovery_full"] = discovery
            result["normalized_rows"] = normalized
        return result
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
