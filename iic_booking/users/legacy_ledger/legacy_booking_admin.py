"""Phase 10D — admin helpers for legacy equipment/booking migration UI."""

from __future__ import annotations

from typing import Any

from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings
from iic_booking.users.legacy_ledger.equipment_mapping import (
    get_active_mapping_for_old_id,
    validate_legacy_equipment_mappings,
)
from iic_booking.users.legacy_ledger.legacy_equipment_inventory import (
    build_equipment_mapping_candidate_report,
    fetch_new_portal_equipment_inventory,
)
from iic_booking.users.legacy_ledger.legacy_user_resolution import summarize_user_mapping_counts
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    LegacyUserMappingStatus,
    MigrationBookingSettlement,
    MigrationSettlementStatus,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def is_main_admin(user) -> bool:
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)


def is_oic_or_admin(user) -> bool:
    if is_main_admin(user):
        return True
    return getattr(user, "user_type", None) in (UserType.MANAGER, UserType.OPERATOR)


def mask_employee_id(emp_id: str, *, allow_full: bool) -> str:
    emp = (emp_id or "").strip()
    if allow_full or len(emp) <= 4:
        return emp
    return f"{emp[:2]}***{emp[-2:]}"


DISCOVERY_BUCKETS = (
    "eligible",
    "unmapped",
    "conflicting",
    "cancelled",
    "completed",
    "outside_window",
    "invalid",
    "duplicate",
)


def discovery_rows_flat(discovery: dict[str, Any]) -> list[dict]:
    rows: list[dict] = []
    for bucket in DISCOVERY_BUCKETS:
        for row in discovery.get(bucket) or []:
            entry = dict(row)
            entry.setdefault("eligibility", row.get("eligibility") or bucket)
            rows.append(entry)
    return rows


def slot_status_for_row(row: dict) -> str:
    elig = row.get("eligibility") or ""
    if elig == "eligible":
        if row.get("conflict_status") and row.get("conflict_status") not in ("NONE", ""):
            return "CONFLICT"
        block = LegacyBookingBlock.objects.filter(
            legacy_booking_id=row.get("legacy_booking_id"),
            status=LegacyBookingBlockStatus.ACTIVE,
        ).first()
        if block:
            return "BLOCKED"
        return "READY"
    if elig in {"cancelled", "completed"}:
        return elig.upper()
    if elig == "outside_window":
        return "OUTSIDE WINDOW"
    if elig == "invalid":
        return "INVALID TIME"
    if elig == "unmapped":
        return "UNMAPPED EQUIPMENT"
    if elig == "conflicting":
        return "CONFLICT"
    if elig == "duplicate":
        return "DUPLICATE"
    return "UNKNOWN"


def slot_action_for_row(row: dict) -> str:
    """UI slot action label — USER UNRESOLVED does not block READY/BLOCKABLE."""
    elig = row.get("eligibility") or ""
    user_st = row.get("user_mapping_status") or ""
    if elig == "eligible":
        if row.get("conflict_status") and row.get("conflict_status") not in ("NONE", ""):
            return "CONFLICT — operator resolution required"
        if user_st == LegacyUserMappingStatus.UNRESOLVED:
            return "BLOCKABLE (user unresolved OK)"
        return "READY FOR T0 BLOCK"
    if elig == "unmapped":
        return "BLOCKED — map equipment first"
    if elig == "invalid":
        return "BLOCKED — invalid datetime"
    if elig in {"cancelled", "completed", "outside_window"}:
        return "NO BLOCK"
    if elig == "conflicting":
        return "CONFLICT"
    return "REVIEW"


def row_in_oic_scope(row: dict, oic_equipment_ids: set[int]) -> bool:
    new_eq = row.get("new_equipment_id")
    if new_eq is not None and int(new_eq) in oic_equipment_ids:
        return True
    mapping = get_active_mapping_for_old_id(row.get("old_equipment_id"))
    return bool(mapping and mapping.new_equipment_id in oic_equipment_ids)


def build_legacy_booking_list(
    discovery: dict[str, Any],
    *,
    actor,
    include_pii: bool = False,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    allow_pii = include_pii and is_main_admin(actor)
    oic_scope: set[int] = set()
    if not is_main_admin(actor):
        oic_scope = set(get_equipment_ids_managed_by_oic(actor.id))

    filters = filters or {}
    eligibility_filter = (filters.get("eligibility") or filters.get("migration_status") or "").strip()
    user_map_filter = (filters.get("user_mapping_status") or "").strip()
    equipment_filter = filters.get("old_equipment_id") or filters.get("equipment_id")
    search = (filters.get("search") or filters.get("q") or "").strip().lower()

    out: list[dict] = []
    for row in discovery_rows_flat(discovery):
        if oic_scope and not row_in_oic_scope(row, oic_scope):
            continue
        if eligibility_filter and row.get("eligibility") != eligibility_filter:
            continue
        if user_map_filter and (row.get("user_mapping_status") or "") != user_map_filter:
            continue
        if equipment_filter is not None and int(row.get("old_equipment_id") or -1) != int(equipment_filter):
            continue
        if search:
            hay = " ".join(
                str(row.get(k) or "")
                for k in (
                    "legacy_booking_id",
                    "old_equipment_id",
                    "legacy_employee_id",
                    "status",
                )
            ).lower()
            if search not in hay:
                continue

        emp = row.get("legacy_employee_id") or ""
        user_st = row.get("user_mapping_status") or ""
        row_elig = row.get("eligibility") or ""
        display_eligibility = slot_status_for_row(row)
        if row_elig == "eligible" and user_st == LegacyUserMappingStatus.UNRESOLVED:
            display_eligibility = "USER UNRESOLVED"
        out.append(
            {
                **row,
                "legacy_employee_id_display": mask_employee_id(emp, allow_full=allow_pii),
                "slot_status": slot_status_for_row(row),
                "slot_action": slot_action_for_row(row),
                "display_eligibility": display_eligibility,
                "migration_status": row.get("eligibility") or "unknown",
                "conflict": row.get("eligibility") == "conflicting"
                or bool(row.get("conflict_status") and row.get("conflict_status") != "NONE"),
            }
        )
    return out


def count_bookings_by_legacy_equipment(discovery: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in discovery_rows_flat(discovery):
        old_id = row.get("old_equipment_id")
        if old_id is not None:
            counts[int(old_id)] = counts.get(int(old_id), 0) + 1
    return counts


def build_equipment_mapping_table(
    *,
    booking_counts: dict[int, int] | None = None,
) -> list[dict]:
    booking_counts = booking_counts or {}
    legacy_by_id: dict[int, dict] = {}
    try:
        cand = build_equipment_mapping_candidate_report()
        if cand.get("ok"):
            legacy_by_id = {
                int(r["legacy_equipment_id"]): r
                for r in (cand.get("candidates") or [])
                if r.get("legacy_equipment_id") is not None
            }
    except Exception:
        legacy_by_id = {}
    mappings = {
        m.old_equipment_id: m
        for m in LegacyEquipmentMapping.objects.select_related("new_equipment", "updated_by").all()
    }
    all_ids = sorted(set(legacy_by_id.keys()) | set(mappings.keys()))
    rows: list[dict] = []
    for old_id in all_ids:
        m = mappings.get(old_id)
        leg = legacy_by_id.get(old_id, {})
        conflict_count = 0
        if m and m.status == LegacyEquipmentMappingStatus.CONFLICT:
            conflict_count = 1
        rows.append(
            {
                "old_equipment_id": old_id,
                "old_equipment_name": (m.old_equipment_name if m else leg.get("legacy_equipment_name")) or "",
                "old_equipment_code": (m.old_equipment_code if m else leg.get("legacy_equipment_code")) or "",
                "legacy_booking_count": booking_counts.get(old_id, 0),
                "mapping_id": m.id if m else None,
                "new_equipment_id": getattr(m.new_equipment, "equipment_id", None) if m and m.new_equipment else None,
                "new_equipment_code": getattr(m.new_equipment, "code", "") if m and m.new_equipment else "",
                "new_equipment_name": getattr(m.new_equipment, "name", "") if m and m.new_equipment else "",
                "mapping_status": m.status if m else LegacyEquipmentMappingStatus.UNMAPPED,
                "conflict_count": conflict_count,
                "last_updated": m.updated_at.isoformat() if m and m.updated_at else None,
            }
        )
    return rows


def build_migration_summary_dashboard(
    discovery: dict[str, Any] | None = None,
    *,
    legacy_equipment_count: int | None = None,
) -> dict[str, Any]:
    state = PortalMigrationState.get_solo()
    mapping_report = validate_legacy_equipment_mappings()
    mc = mapping_report.get("counts") or {}
    new_inv = fetch_new_portal_equipment_inventory()
    discovery = discovery or {"counts": {}}
    dc = discovery.get("counts") or {}
    flat = discovery_rows_flat(discovery)
    user_counts = summarize_user_mapping_counts(flat)

    active_blocks = LegacyBookingBlock.objects.filter(status=LegacyBookingBlockStatus.ACTIVE).count()
    user_resolved_blocks = LegacyBookingBlock.objects.filter(
        user_mapping_status=LegacyUserMappingStatus.RESOLVED_CHANNEL_I
    ).count()
    user_unresolved_blocks = LegacyBookingBlock.objects.filter(
        user_mapping_status=LegacyUserMappingStatus.UNRESOLVED
    ).count()

    refunds_pending = MigrationBookingSettlement.objects.filter(status=MigrationSettlementStatus.PENDING).count()
    refunds_completed = MigrationBookingSettlement.objects.filter(
        status=MigrationSettlementStatus.COMPLETED
    ).count()

    legacy_eq = legacy_equipment_count
    if legacy_eq is None:
        try:
            cand = build_equipment_mapping_candidate_report()
            legacy_eq = cand.get("legacy_inventory", {}).get("count") if cand.get("ok") else None
        except Exception:
            legacy_eq = None

    mapped_total = mc.get("mapped", 0)
    legacy_total = legacy_eq or 0

    return {
        "legacy_equipment_discovered": legacy_total,
        "new_equipment_available": new_inv.get("count", 0),
        "equipment_mapped": f"{mapped_total} / {legacy_total}" if legacy_total else mapped_total,
        "equipment_mapped_count": mapped_total,
        "legacy_bookings_in_window": sum(dc.values()) if dc else len(flat),
        "eligible": dc.get("eligible", 0),
        "slot_blocks_ready": dc.get("eligible", 0),
        "active_slot_blocks": active_blocks,
        "unmapped_equipment": dc.get("unmapped", 0),
        "time_mapping_issues": dc.get("invalid", 0),
        "user_mapping_resolved": user_counts.get("resolved", 0) + user_resolved_blocks,
        "user_mapping_pending": user_counts.get("unresolved", 0) + user_unresolved_blocks,
        "conflicts": dc.get("conflicting", 0),
        "invalid_bookings": dc.get("invalid", 0),
        "cancelled": dc.get("cancelled", 0),
        "completed": dc.get("completed", 0),
        "outside_window": dc.get("outside_window", 0),
        "duplicate": dc.get("duplicate", 0),
        "refunds_pending": refunds_pending,
        "refunds_completed": refunds_completed,
        "migration_window_configured": bool(state.migration_start_at and state.migration_window_end_at),
        "datetime_map_note": (
            "Operator must approve docs/release/migration/legacy_booking_datetime_map.json "
            "before live MySQL discovery."
        ),
        "user_mapping_blocks_readiness": False,
        "mapping_counts": mc,
        "discovery_counts": dc,
    }


def run_legacy_booking_discovery(legacy_rows: list[dict] | None = None) -> dict[str, Any]:
    rows = legacy_rows or []
    discovery = discover_legacy_bookings(rows)
    return discovery
