"""READ-ONLY legacy vs new equipment inventory for operator mapping preparation."""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from iic_booking.equipment.models import Equipment, EquipmentStatus
from iic_booking.users.legacy_ledger.reader import OldMySQLNotConfigured, OldMySQLReader

LEGACY_EQUIPMENT_TABLE_CANDIDATES = ("equipment", "equipments", "instrument", "instruments")


def _legacy_equipment_table(reader: OldMySQLReader) -> str | None:
    tables = {next(iter(t.values())) for t in reader.fetchall("SHOW TABLES")}
    hits = [t for t in LEGACY_EQUIPMENT_TABLE_CANDIDATES if t in tables]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return hits[0]  # report ambiguity in output
    return None


def fetch_legacy_equipment_inventory() -> dict[str, Any]:
    """SELECT legacy equipment ids/names — no fuzzy matching to new portal."""
    try:
        reader = OldMySQLReader()
    except OldMySQLNotConfigured as exc:
        return {"ok": False, "error": str(exc)}

    with reader:
        table = _legacy_equipment_table(reader)
        if not table:
            return {"ok": False, "error": "legacy_equipment_table_not_found", "tables_checked": LEGACY_EQUIPMENT_TABLE_CANDIDATES}
        cols = {c["Field"] for c in reader.fetchall(f"SHOW COLUMNS FROM `{table}`")}
        id_col = "id" if "id" in cols else None
        name_col = next((c for c in ("name", "equipment_name", "title", "equipment_title") if c in cols), None)
        code_col = next((c for c in ("code", "equipment_code", "short_name", "eq_code") if c in cols), None)
        if not id_col:
            return {"ok": False, "error": "legacy_equipment_id_column_not_found", "columns": sorted(cols)}
        select = [f"`{id_col}` AS legacy_id"]
        if name_col:
            select.append(f"`{name_col}` AS legacy_name")
        else:
            select.append("NULL AS legacy_name")
        if code_col:
            select.append(f"`{code_col}` AS legacy_code")
        else:
            select.append("NULL AS legacy_code")
        rows = reader.fetchall(f"SELECT {', '.join(select)} FROM `{table}` ORDER BY `{id_col}`")
    return {
        "ok": True,
        "table": table,
        "id_column": id_col,
        "name_column": name_col,
        "code_column": code_col,
        "legacy_equipment": rows,
        "count": len(rows),
    }


def fetch_new_portal_equipment_inventory() -> dict[str, Any]:
    qs = Equipment.objects.filter(
        Q(status=EquipmentStatus.ACTIVE) | Q(status="") | Q(status__isnull=True)
    ).values("equipment_id", "code", "name", "internal_department_id", "status")
    items = list(qs)
    return {"ok": True, "count": len(items), "equipment": items}


def build_equipment_mapping_candidate_report() -> dict[str, Any]:
    """
    Candidate report only — no automatic OLD→NEW pairing.
    Every legacy row is UNMAPPED until operator explicitly loads LegacyEquipmentMapping after 0102.
    """
    legacy = fetch_legacy_equipment_inventory()
    if not legacy.get("ok"):
        return {"ok": False, "legacy": legacy, "candidates": []}

    new_inv = fetch_new_portal_equipment_inventory()
    candidates = []
    for row in legacy.get("legacy_equipment") or []:
        candidates.append(
            {
                "legacy_equipment_id": row.get("legacy_id"),
                "legacy_equipment_name": row.get("legacy_name") or row.get("legacy_code") or "",
                "legacy_equipment_code": row.get("legacy_code") or "",
                "candidate_new_equipment_id": None,
                "candidate_new_equipment_code": None,
                "confidence": "NONE",
                "status": "UNMAPPED",
                "operator_confirmation_required": True,
            }
        )
    return {
        "ok": True,
        "legacy_inventory": {"count": legacy.get("count"), "table": legacy.get("table")},
        "new_portal_inventory": {"count": new_inv.get("count")},
        "mapped": 0,
        "unmapped": len(candidates),
        "ambiguous": 0,
        "candidates": candidates,
        "note": "No fuzzy matching performed. Load explicit mappings after users.0102.",
    }
