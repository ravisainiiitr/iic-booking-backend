"""Shared booking logic for fabrication profiles (3D printing and 2D laser cutting).

Per-part details reach the charge engine through reserved input_values keys. They are always
rebuilt server-side from the linked analyses (never trusted from the client) and are stripped
before input_values are stored on the booking.
"""

from __future__ import annotations

import math
from decimal import Decimal

from .models import (
    FABRICATION_PROFILE_TYPES,
    EquipmentProfileType,
    LaserCutAnalysis,
    LaserCutBatch,
    PrintAnalysis,
    PrintAnalysisStatus,
)

PARTS_KEY = "_fabrication_parts"
OWN_MATERIAL_KEY = "_own_material"
BOOKED_MINUTES_KEY = "_booked_minutes"
RESERVED_KEYS = (PARTS_KEY, OWN_MATERIAL_KEY, BOOKED_MINUTES_KEY)

MAX_PART_QUANTITY = 10_000


def is_fabrication_equipment(equipment) -> bool:
    return getattr(equipment, "profile_type", None) in FABRICATION_PROFILE_TYPES


def strip_fabrication_keys(input_values):
    if not isinstance(input_values, dict):
        return input_values
    if not any(k in input_values for k in RESERVED_KEYS):
        return input_values
    return {k: v for k, v in input_values.items() if k not in RESERVED_KEYS}


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def own_material_available(equipment) -> bool:
    return (
        is_fabrication_equipment(equipment)
        and getattr(equipment, "own_material_fixed_charge", None) is not None
    )


def resolve_own_material(equipment, requested) -> bool:
    """The flag only takes effect when the equipment offers a fixed own-material charge."""
    return bool(parse_bool(requested) and own_material_available(equipment))


def default_part_name(filename: str) -> str:
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return (stem or base or "Part")[:255]


def parse_quantity(value) -> int | None:
    try:
        qty = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if qty < 1 or qty > MAX_PART_QUANTITY:
        return None
    return qty


def booked_minutes(booking) -> int:
    return sum(
        int((s.end_datetime - s.start_datetime).total_seconds() // 60)
        for s in booking.daily_slots.all()
        if s.start_datetime and s.end_datetime
    )


# --------------------------------------------------------------------------- 3D printing


def _ceil_grams(value) -> int:
    if value is None:
        return 0
    return int(math.ceil(float(value)))


def build_print_parts(analyses) -> list[dict]:
    """Per-file totals. Estimates are multiplied by quantity; staff-entered actuals are totals already."""
    parts = []
    for a in analyses:
        qty = max(1, int(getattr(a, "quantity", 1) or 1))
        weight_each = _ceil_grams(a.weight_grams)
        time_each = int(a.estimated_time_minutes or 0)
        has_actual_weight = a.actual_weight_grams is not None
        has_actual_time = a.actual_time_minutes is not None
        weight_total = _ceil_grams(a.actual_weight_grams) if has_actual_weight else weight_each * qty
        time_total = int(a.actual_time_minutes) if has_actual_time else time_each * qty
        parts.append(
            {
                "kind": "print",
                "analysis_id": str(a.id),
                "name": a.display_part_name,
                "filename": a.original_filename,
                "quantity": qty,
                "weight_g_each": weight_each,
                "time_min_each": time_each,
                "weight_g_total": weight_total,
                "time_min_total": time_total,
                "actual_weight": has_actual_weight,
                "actual_time": has_actual_time,
            }
        )
    return parts


def print_material_code(analyses) -> str:
    for a in analyses:
        code = a.material_code_snapshot or (a.material.code if a.material else "")
        if code:
            return code
    return ""


def inject_print_parts(input_values, analyses) -> dict:
    merged = dict(input_values or {})
    parts = build_print_parts(analyses)
    merged["A"] = sum(p["weight_g_total"] for p in parts)
    merged["C"] = sum(p["time_min_total"] for p in parts)
    code = print_material_code(analyses)
    if code:
        merged["B"] = code
    merged[PARTS_KEY] = parts
    return merged


def active_print_analyses_for_booking(booking) -> list:
    analyses = list(
        PrintAnalysis.objects.filter(booking=booking, cancelled_at__isnull=True)
        .select_related("material")
        .order_by("sequence", "created_at")
    )
    if not analyses:
        analysis = getattr(booking, "print_analysis", None)
        if analysis and not analysis.cancelled_at and analysis.superseded_at is None:
            analyses = [analysis]
    return analyses


# --------------------------------------------------------------------------- 2D laser cutting


def build_laser_parts(analyses) -> list[dict]:
    parts = []
    for a in analyses:
        m = a.material
        parts.append(
            {
                "kind": "laser",
                "analysis_id": str(a.id),
                "name": a.display_part_name,
                "filename": a.original_filename,
                "quantity": max(1, int(a.quantity or 1)),
                "width_mm": str(a.width_mm) if a.width_mm is not None else None,
                "height_mm": str(a.height_mm) if a.height_mm is not None else None,
                "area_mm2": str(a.area_mm2) if a.area_mm2 is not None else None,
                "units": a.units,
                "units_assumed": bool(a.units_assumed),
                "material_id": a.material_id,
                "material_code": (m.code if m else a.material_code_snapshot) or "",
                "material_name": m.name if m else "",
                "thickness_mm": str(m.thickness_mm) if m else None,
                "sheet_width_mm": str(m.sheet_width_mm) if m else None,
                "sheet_height_mm": str(m.sheet_height_mm) if m else None,
                "sheet_rate": str(m.sheet_rate if m else (a.sheet_rate_snapshot or "")) or None,
            }
        )
    return parts


def validate_laser_analyses(analyses, *, require_active_material: bool) -> str | None:
    from .laser_cut_service import sheet_fit_error

    if not analyses:
        return "Upload at least one DXF file."
    for a in analyses:
        label = a.display_part_name
        if a.status != PrintAnalysisStatus.COMPLETED:
            return f"{label}: the DXF could not be measured. Remove it or upload a corrected file."
        if not a.material_id or a.material is None:
            return f"{label}: choose a sheet material."
        if require_active_material and not a.material.is_active:
            return f"{label}: the selected sheet material is no longer available. Choose another one."
        err = sheet_fit_error(a.width_mm, a.height_mm, a.material)
        if err:
            return f"{label}: {err}"
    return None


def active_laser_analyses_for_batch(batch) -> list:
    return list(
        batch.items.filter(cancelled_at__isnull=True, superseded_at__isnull=True)
        .select_related("material")
        .order_by("sequence", "created_at")
    )


def active_laser_analyses_for_booking(booking) -> list:
    return list(
        LaserCutAnalysis.objects.filter(booking=booking, cancelled_at__isnull=True)
        .select_related("material")
        .order_by("sequence", "created_at")
    )


def merge_laser_booking_into_input_values(equipment, input_values, user, *, laser_cut_batch_id):
    """Validate an unlinked laser batch and inject its parts. Returns (input_values, error, batch)."""
    from .print_3d_views import print_analysis_actor_owns

    if not laser_cut_batch_id:
        return input_values, "Upload your DXF file(s) before booking.", None
    try:
        batch = LaserCutBatch.objects.get(pk=laser_cut_batch_id)
    except (LaserCutBatch.DoesNotExist, ValueError, TypeError):
        return input_values, "Invalid laser_cut_batch_id.", None
    except Exception:  # noqa: BLE001 - malformed UUIDs raise ValidationError on some backends
        return input_values, "Invalid laser_cut_batch_id.", None
    if batch.equipment_id != equipment.equipment_id:
        return input_values, "These DXF files were uploaded for a different equipment.", None
    if not print_analysis_actor_owns(user, batch.user_id):
        return input_values, "These DXF files were uploaded by another user.", None
    if batch.booking_id:
        return input_values, "These DXF files are already linked to a booking.", None
    analyses = active_laser_analyses_for_batch(batch)
    for a in analyses:
        if a.booking_id:
            return input_values, f"{a.display_part_name} is already linked to a booking.", None
    err = validate_laser_analyses(analyses, require_active_material=True)
    if err:
        return input_values, err, None
    merged = dict(input_values or {})
    merged[PARTS_KEY] = build_laser_parts(analyses)
    return merged, None, batch


def link_laser_batch_to_booking(booking, batch) -> None:
    batch.booking = booking
    batch.save(update_fields=["booking", "updated_at"])
    LaserCutAnalysis.objects.filter(
        batch=batch, cancelled_at__isnull=True, superseded_at__isnull=True
    ).update(booking=booking)


# --------------------------------------------------------------------------- recalculation


def apply_fabrication_to_input_values(booking, input_values) -> dict:
    """Rebuild reserved fabrication keys from the booking's linked files (used by every recalculation)."""
    equipment = getattr(booking, "equipment", None)
    profile = getattr(equipment, "profile_type", None)
    if profile not in FABRICATION_PROFILE_TYPES:
        return input_values
    merged = strip_fabrication_keys(dict(input_values or {}))
    if profile == EquipmentProfileType.PRINT_3D:
        analyses = active_print_analyses_for_booking(booking)
        if analyses:
            merged = inject_print_parts(merged, analyses)
    else:
        merged[PARTS_KEY] = build_laser_parts(active_laser_analyses_for_booking(booking))
        minutes = booked_minutes(booking) if getattr(booking, "pk", None) else 0
        if minutes > 0:
            merged[BOOKED_MINUTES_KEY] = minutes
    merged[OWN_MATERIAL_KEY] = bool(getattr(booking, "own_material", False)) and own_material_available(equipment)
    return merged


def fabrication_parts_summary(booking) -> list[dict]:
    """Display rows for booking detail, job sheet and emails."""
    equipment = getattr(booking, "equipment", None)
    profile = getattr(equipment, "profile_type", None)
    if profile == EquipmentProfileType.PRINT_3D:
        return build_print_parts(active_print_analyses_for_booking(booking))
    if profile == EquipmentProfileType.LASER_CUT_2D:
        return build_laser_parts(active_laser_analyses_for_booking(booking))
    return []


def format_part_line(part: dict) -> str:
    qty = part.get("quantity") or 1
    if part.get("kind") == "laser":
        size = ""
        if part.get("width_mm") and part.get("height_mm"):
            w = Decimal(part["width_mm"]).quantize(Decimal("0.1"))
            h = Decimal(part["height_mm"]).quantize(Decimal("0.1"))
            area = (Decimal(part.get("area_mm2") or 0) / Decimal("1000000")).quantize(Decimal("0.0001"))
            size = f", {w} × {h} mm ({area} m² each)"
        material = part.get("material_name") or part.get("material_code") or "no material"
        return f"{part.get('name')} × {qty} — {material}{size} [{part.get('filename')}]"
    weight = part.get("weight_g_each")
    time_min = part.get("time_min_each")
    est = f", est. {weight} g / {time_min} min each" if weight or time_min else ""
    return f"{part.get('name')} × {qty}{est} [{part.get('filename')}]"
