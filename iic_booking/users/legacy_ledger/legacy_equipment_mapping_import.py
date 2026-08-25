"""Phase 10E — explicit legacy equipment mapping file import (preview/apply)."""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from iic_booking.equipment.models import Equipment
from iic_booking.users.legacy_ledger.equipment_mapping import (
    validate_legacy_equipment_mapping_save,
)
from iic_booking.users.models.portal_migration import (
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
)


def parse_equipment_mapping_file(data: list | dict) -> list[dict]:
    if isinstance(data, dict):
        rows = data.get("mappings") or data.get("equipment_mappings") or []
    else:
        rows = data
    if not isinstance(rows, list):
        raise ValueError("mapping_file_must_be_list_or_mappings_key")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "legacy_equipment_id": int(row["legacy_equipment_id"]),
                "new_equipment_id": int(row["new_equipment_id"]),
                "approved_by": str(row.get("approved_by") or ""),
                "approval_timestamp": row.get("approval_timestamp") or row.get("approved_at_utc"),
                "operator_note": str(row.get("operator_note") or row.get("note") or ""),
            }
        )
    return out


def validate_equipment_mapping_file(
    rows: list[dict],
    *,
    required_legacy_ids: set[int] | None = None,
) -> dict[str, Any]:
    errors: list[dict] = []
    warnings: list[dict] = []
    valid_rows: list[dict] = []
    seen_legacy: set[int] = set()
    seen_new: dict[int, int] = {}

    for row in rows:
        old_id = row["legacy_equipment_id"]
        new_id = row["new_equipment_id"]
        if old_id in seen_legacy:
            errors.append({"legacy_equipment_id": old_id, "error": "duplicate_legacy_id_in_file"})
            continue
        seen_legacy.add(old_id)
        v = validate_legacy_equipment_mapping_save(
            old_equipment_id=old_id,
            new_equipment_id=new_id,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        if not v["valid"]:
            errors.append({"legacy_equipment_id": old_id, "errors": v["errors"]})
            continue
        for w in v.get("warnings") or []:
            warnings.append({"legacy_equipment_id": old_id, "warning": w})
        if new_id in seen_new:
            warnings.append(
                {
                    "legacy_equipment_id": old_id,
                    "warning": f"multiple legacy IDs map to new equipment {new_id} (also legacy {seen_new[new_id]})",
                }
            )
        seen_new[new_id] = old_id
        if not row.get("approved_by"):
            warnings.append({"legacy_equipment_id": old_id, "warning": "missing approved_by"})
        valid_rows.append(row)

    missing_required = []
    if required_legacy_ids:
        missing_required = sorted(required_legacy_ids - seen_legacy)

    return {
        "valid": not errors and not missing_required,
        "row_count": len(rows),
        "valid_row_count": len(valid_rows),
        "errors": errors,
        "warnings": warnings,
        "missing_required_legacy_ids": missing_required,
        "valid_rows": valid_rows,
    }


def preview_equipment_mapping_import(
    file_path: str | Path,
    *,
    required_legacy_ids: set[int] | None = None,
) -> dict[str, Any]:
    p = Path(file_path)
    if not p.is_file():
        return {"ok": False, "error": "file_not_found", "path": str(p)}
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = parse_equipment_mapping_file(data)
    report = validate_equipment_mapping_file(rows, required_legacy_ids=required_legacy_ids)
    return {"ok": report["valid"], "path": str(p), "preview": report, "dry_run": True}


def apply_equipment_mapping_import(
    file_path: str | Path,
    *,
    actor,
    required_legacy_ids: set[int] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    preview = preview_equipment_mapping_import(file_path, required_legacy_ids=required_legacy_ids)
    if not preview.get("ok") and preview.get("preview", {}).get("errors"):
        return {**preview, "applied": 0}
    if dry_run:
        return {**preview, "applied": 0, "note": "dry_run_no_database_writes"}

    applied = 0
    for row in preview.get("preview", {}).get("valid_rows") or []:
        old_id = row["legacy_equipment_id"]
        new_id = row["new_equipment_id"]
        try:
            new_eq = Equipment.objects.get(pk=new_id)
        except Equipment.DoesNotExist:
            continue
        mapping, _ = LegacyEquipmentMapping.objects.update_or_create(
            old_equipment_id=old_id,
            defaults={
                "new_equipment": new_eq,
                "status": LegacyEquipmentMappingStatus.ACTIVE,
                "mapping_reason": row.get("operator_note") or "imported from legacy_equipment_mapping.json",
                "updated_by": actor,
            },
        )
        if mapping.created_by_id is None and actor is not None:
            mapping.created_by = actor
            mapping.save(update_fields=["created_by"])
        applied += 1
    return {**preview, "applied": applied, "dry_run": False}


def default_mapping_file_path() -> Path:
    from django.conf import settings

    return Path(settings.BASE_DIR) / "docs" / "release" / "migration" / "legacy_equipment_mapping.json"


def write_mapping_file_template(path: Path | None = None) -> Path:
    p = path or default_mapping_file_path()
    if p.is_file():
        return p
    template = {
        "_contract": "Explicit legacy → new equipment mapping. No fuzzy matching.",
        "_status": "OPERATOR_REQUIRED",
        "mappings": [],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(template, indent=2), encoding="utf-8")
    return p
