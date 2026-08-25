"""Phase 8B — legacy equipment mapping validation (no fuzzy name matching)."""

from __future__ import annotations

from typing import Any

from iic_booking.equipment.models import Equipment, EquipmentStatus
from iic_booking.users.models.portal_migration import (
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
)


def validate_legacy_equipment_mappings() -> dict[str, Any]:
    """Read-only validation report for LegacyEquipmentMapping rows."""
    mapped: list[dict] = []
    unmapped: list[dict] = []
    conflict: list[dict] = []
    disabled: list[dict] = []
    invalid: list[dict] = []

    seen_new_active: dict[int, int] = {}
    for m in LegacyEquipmentMapping.objects.select_related("new_equipment", "department").all():
        row = {
            "id": m.id,
            "old_equipment_id": m.old_equipment_id,
            "old_equipment_code": m.old_equipment_code,
            "status": m.status,
            "new_equipment_id": getattr(m.new_equipment, "equipment_id", None),
            "new_equipment_code": getattr(m.new_equipment, "code", "") if m.new_equipment_id else "",
            "department_id": m.department_id,
        }
        if m.status == LegacyEquipmentMappingStatus.CONFLICT:
            conflict.append(row)
            continue
        if m.status == LegacyEquipmentMappingStatus.DISABLED:
            disabled.append(row)
            continue
        if m.status == LegacyEquipmentMappingStatus.UNMAPPED or not m.new_equipment_id:
            unmapped.append(row)
            continue
        if m.status == LegacyEquipmentMappingStatus.RETIRED:
            disabled.append(row)
            continue
        # ACTIVE
        eq = m.new_equipment
        if eq is None:
            invalid.append({**row, "reason": "missing_new_equipment"})
            continue
        eq_status = (eq.status or "").strip()
        if eq_status and eq_status != EquipmentStatus.ACTIVE:
            disabled.append({**row, "reason": "new_equipment_not_operational", "equipment_status": eq_status})
            continue
        if m.department_id and eq.internal_department_id and m.department_id != eq.internal_department_id:
            conflict.append({**row, "reason": "cross_department_mapping"})
            continue
        reason_u = (m.mapping_reason or "").upper()
        if "MODE_MISMATCH" in reason_u or "MUTUALLY_EXCLUSIVE" in reason_u:
            conflict.append({**row, "reason": "mode_mismatch"})
            continue
        nid = eq.equipment_id
        if nid in seen_new_active:
            conflict.append({**row, "reason": "duplicate_active_new_mapping", "other_mapping_id": seen_new_active[nid]})
            continue
        seen_new_active[nid] = m.id
        mapped.append(row)

    # Duplicate old ids are DB-unique; still report orphan new equipment optionally (informational)
    return {
        "mapped": mapped,
        "unmapped": unmapped,
        "conflict": conflict,
        "disabled": disabled,
        "invalid": invalid,
        "counts": {
            "mapped": len(mapped),
            "unmapped": len(unmapped),
            "conflict": len(conflict),
            "disabled": len(disabled),
            "invalid": len(invalid),
        },
        "ready": len(conflict) == 0 and len(invalid) == 0,
    }


def get_active_mapping_for_old_id(old_equipment_id: int) -> LegacyEquipmentMapping | None:
    return (
        LegacyEquipmentMapping.objects.filter(
            old_equipment_id=old_equipment_id,
            status=LegacyEquipmentMappingStatus.ACTIVE,
            new_equipment__isnull=False,
        )
        .select_related("new_equipment")
        .first()
    )


def validate_legacy_equipment_mapping_save(
    *,
    old_equipment_id: int,
    new_equipment_id: int | None,
    status: str,
    exclude_mapping_id: int | None = None,
    discovered_old_ids: set[int] | None = None,
) -> dict[str, Any]:
    """
    Pre-save validation for explicit administrator equipment mapping.
    Does not perform fuzzy automatic mapping.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if discovered_old_ids is not None and old_equipment_id not in discovered_old_ids:
        warnings.append(
            f"legacy_equipment_id={old_equipment_id} not found in discovered legacy bookings "
            "(may still be valid if inventory-only mapping)"
        )

    dup_qs = LegacyEquipmentMapping.objects.filter(old_equipment_id=old_equipment_id)
    if exclude_mapping_id:
        dup_qs = dup_qs.exclude(pk=exclude_mapping_id)
    if dup_qs.exists():
        errors.append("duplicate_legacy_equipment_mapping")

    if status not in LegacyEquipmentMappingStatus.values:
        errors.append("invalid_mapping_status")

    new_eq = None
    if new_equipment_id is not None:
        try:
            new_eq = Equipment.objects.get(pk=int(new_equipment_id))
        except (ValueError, Equipment.DoesNotExist):
            errors.append("invalid_new_equipment_id")
        if new_eq:
            eq_status = (new_eq.status or "").strip()
            if eq_status and eq_status != EquipmentStatus.ACTIVE:
                errors.append("new_equipment_not_active")

    if status == LegacyEquipmentMappingStatus.ACTIVE:
        if new_eq is None:
            errors.append("active_mapping_requires_new_equipment")
        else:
            clash = LegacyEquipmentMapping.objects.filter(
                new_equipment=new_eq,
                status=LegacyEquipmentMappingStatus.ACTIVE,
            )
            if exclude_mapping_id:
                clash = clash.exclude(pk=exclude_mapping_id)
            if clash.exists():
                other = clash.first()
                warnings.append(
                    f"multiple legacy equipment IDs map to new equipment {new_eq.equipment_id} "
                    f"(existing mapping id={other.pk if other else '?'}) — confirm explicitly"
                )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }
