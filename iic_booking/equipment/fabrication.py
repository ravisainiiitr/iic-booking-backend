"""Shared booking logic for fabrication profiles (3D printing and 2D laser cutting).

Per-part details reach the charge engine through reserved input_values keys. They are always
rebuilt server-side from the linked analyses (never trusted from the client) and are stripped
before input_values are stored on the booking.

Input A is "Quantity Required": how many times the whole job (every file / part with its own count) is
made. It multiplies the 3D print weight and time and the laser sheet share. Bookings saved before it carry
no ``QUANTITY_MARKER_KEY`` and count as quantity 1 (3D print bookings stored the total weight in A).
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

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
PRINT_WEIGHT_KEY = "_print_weight_g"
JOB_QUANTITY_KEY = "_job_quantity"
# Laser: machine-time estimate of the whole job from the DXF cut paths (see laser_time_model).
LASER_ESTIMATE_KEY = "_laser_time_estimate"
RESERVED_KEYS = (
    PARTS_KEY, OWN_MATERIAL_KEY, BOOKED_MINUTES_KEY, PRINT_WEIGHT_KEY, JOB_QUANTITY_KEY, LASER_ESTIMATE_KEY,
)

# Stored with the booking's inputs (not reserved): A holds Quantity Required.
QUANTITY_MARKER_KEY = "_quantity_in_a"
# Stored with 3D print inputs (not reserved): the support choice and estimated support grams, for staff.
PRINT_SUPPORTS_KEY = "_print_supports"
QUANTITY_KEY = "A"
QUANTITY_LABEL = "Quantity Required"

MAX_PART_QUANTITY = 10_000
MAX_JOB_QUANTITY = 1_000


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


def parse_job_quantity(value) -> int | None:
    """Whole number from 1 to MAX_JOB_QUANTITY (2, 2.0 and "2" are accepted)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        if number != number.to_integral_value():
            return None
        qty = int(number)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if qty < 1 or qty > MAX_JOB_QUANTITY:
        return None
    return qty


def job_quantity_from_values(profile, input_values) -> int:
    """Quantity Required of saved / submitted inputs; 1 when missing and for bookings saved before it (their A
    is the 3D print weight, or another input of an equipment that later moved to a fabrication profile)."""
    if profile not in FABRICATION_PROFILE_TYPES or not isinstance(input_values, dict):
        return 1
    if not parse_bool(input_values.get(QUANTITY_MARKER_KEY)):
        return 1
    return parse_job_quantity(input_values.get(QUANTITY_KEY)) or 1


def booking_job_quantity(booking) -> int:
    return job_quantity_from_values(
        getattr(getattr(booking, "equipment", None), "profile_type", None), getattr(booking, "input_values", None)
    )


def drop_pre_quantity_print_weight(profile, input_values):
    """A page loaded before Quantity Required sends A = 3D print weight with C = print time; ignore that A so
    it is neither checked against the Quantity Required limits nor read as a quantity."""
    if (
        profile == EquipmentProfileType.PRINT_3D
        and isinstance(input_values, dict)
        and "C" in input_values
        and not parse_bool(input_values.get(QUANTITY_MARKER_KEY))
    ):
        return {k: v for k, v in input_values.items() if k not in (QUANTITY_KEY, "B", "C")}
    return input_values


def prepare_new_job_quantity(profile, input_values) -> tuple[dict, str | None]:
    """Inputs of a new fabrication booking with A set to Quantity Required (default 1). Returns (values, error)."""
    merged = dict(drop_pre_quantity_print_weight(profile, input_values) or {})
    raw = merged.get(QUANTITY_KEY)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        qty = 1
    else:
        qty = parse_job_quantity(raw)
        if qty is None:
            return merged, f"{QUANTITY_LABEL} must be a whole number from 1 to {MAX_JOB_QUANTITY}."
    merged[QUANTITY_KEY] = qty
    merged[QUANTITY_MARKER_KEY] = True
    return merged, None


def display_input_values(equipment, input_values):
    """Inputs as shown and reported: fabrication bookings always show their Quantity Required under A."""
    profile = getattr(equipment, "profile_type", None)
    if profile not in FABRICATION_PROFILE_TYPES or not isinstance(input_values, dict):
        return input_values
    return {**input_values, QUANTITY_KEY: job_quantity_from_values(profile, input_values)}


QUANTITY_FIELD_SPEC = {
    "field_label": QUANTITY_LABEL,
    "field_type": "NUMERIC",
    "is_required": True,
    "default_value": "1",
    # NUMERIC help_text: minimum / maximum / step.
    "help_text": f"1\n{MAX_JOB_QUANTITY}\n1",
}


def _is_quantity_label(label) -> bool:
    return str(label or "").strip().rstrip(":").strip().lower() == QUANTITY_LABEL.lower()


def ensure_fabrication_quantity_inputs(equipment, *, apply: bool = True) -> dict:
    """Give a 3D print / laser equipment the Quantity Required input (key A) for every user type.

    A user type with its own input rows gets its own A row; every other user type reads the shared rows, so a
    shared A row is added too. An existing A row is never changed: Quantity Required is left as it is and any
    other meaning is reported in ``conflicts``. Returns {"created": [user types], "existing": [...],
    "conflicts": [(user type, field type)]}; ``apply=False`` only reports.
    """
    from .models import DynamicInputField

    result = {"created": [], "existing": [], "conflicts": []}
    if getattr(equipment, "profile_type", None) not in FABRICATION_PROFILE_TYPES:
        return result
    rows = list(DynamicInputField.objects.filter(equipment=equipment))
    user_types = [""] + sorted({r.user_type for r in rows if r.user_type})
    a_rows = {r.user_type or "": r for r in rows if r.field_key == QUANTITY_KEY}
    for user_type in user_types:
        row = a_rows.get(user_type)
        if row is None:
            if apply:
                DynamicInputField.objects.create(
                    equipment=equipment, user_type=user_type, field_key=QUANTITY_KEY, **QUANTITY_FIELD_SPEC
                )
            result["created"].append(user_type)
        elif _is_quantity_label(row.field_label) and row.field_type == "NUMERIC":
            result["existing"].append(user_type)
        else:
            result["conflicts"].append((user_type, row.field_type))
    return result


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


def print_estimate_breakdown(analysis) -> dict:
    """The model's breakdown saved with the analysis ({} for analyses estimated before supports existed)."""
    bbox = getattr(analysis, "bounding_box", None)
    value = bbox.get("_estimate") if isinstance(bbox, dict) else None
    return value if isinstance(value, dict) else {}


def build_print_parts(analyses, job_quantity: int = 1) -> list[dict]:
    """Per-file totals. Estimates are multiplied by the file's copies and the job quantity; staff-entered
    actuals are the totals of all those copies already.

    ``weight_g_*`` is charged at the model material's rate (it includes supports printed in that material).
    Supports in a separate material are ``support_weight_g_*`` at ``support_material_code``'s rate; they stay
    at the estimate when staff enter the actual weight (which is the model material)."""
    job_quantity = max(1, int(job_quantity or 1))
    parts = []
    for a in analyses:
        qty = max(1, int(getattr(a, "quantity", 1) or 1))
        weight_each = _ceil_grams(a.weight_grams)
        time_each = int(a.estimated_time_minutes or 0)
        has_actual_weight = a.actual_weight_grams is not None
        has_actual_time = a.actual_time_minutes is not None
        copies = qty * job_quantity
        weight_total = _ceil_grams(a.actual_weight_grams) if has_actual_weight else weight_each * copies
        time_total = int(a.actual_time_minutes) if has_actual_time else time_each * copies
        part = {
            "kind": "print",
            "analysis_id": str(a.id),
            "name": a.display_part_name,
            "filename": a.original_filename,
            "quantity": qty,
            "job_quantity": job_quantity,
            "weight_g_each": weight_each,
            "time_min_each": time_each,
            "weight_g_total": weight_total,
            "time_min_total": time_total,
            "actual_weight": has_actual_weight,
            "actual_time": has_actual_time,
        }
        est = print_estimate_breakdown(a)
        if est.get("support_mode"):
            support_code = str(est.get("support_material_code") or "")
            support_each = _ceil_grams(est.get("support_material_g")) if support_code else 0
            part.update(
                {
                    "support_mode": est.get("support_mode"),
                    "support_mode_label": est.get("support_mode_label") or est.get("support_mode"),
                    "support_g_each": round(float(est.get("support_g") or 0), 1),
                    "support_angle_deg": est.get("support_angle_deg"),
                    "support_material_code": support_code if support_each else "",
                    "support_weight_g_each": support_each,
                    "support_weight_g_total": support_each * copies,
                }
            )
        orientation = (getattr(a, "slicer_settings", None) or {}).get("orientation")
        if orientation:
            part["orientation"] = list(orientation)
        parts.append(part)
    return parts


def print_supports_summary(parts) -> str:
    """One line per booking for staff, e.g. 'Touching build plate only (12 g); Part 2: none'."""
    rows = []
    for p in parts:
        mode = p.get("support_mode")
        if not mode:
            continue
        text = p.get("support_mode_label") or mode
        if p.get("support_weight_g_each"):
            text += f", {p['support_weight_g_each']} g {p.get('support_material_code')} each"
        elif float(p.get("support_g_each") or 0) > 0:
            text += f", ~{p['support_g_each']} g each"
        rows.append(f"{p.get('name')}: {text}" if len(parts) > 1 else text)
    return "; ".join(rows)


def print_material_code(analyses) -> str:
    for a in analyses:
        code = a.material_code_snapshot or (a.material.code if a.material else "")
        if code:
            return code
    return ""


def inject_print_parts(input_values, analyses, job_quantity: int = 1) -> dict:
    """Total weight (reserved key), B = material code and C = print minutes for all files and copies.
    A (Quantity Required) is left as it is."""
    merged = dict(input_values or {})
    parts = build_print_parts(analyses, job_quantity)
    merged[PRINT_WEIGHT_KEY] = sum(p["weight_g_total"] for p in parts)
    merged["C"] = sum(p["time_min_total"] for p in parts)
    code = print_material_code(analyses)
    if code:
        merged["B"] = code
    merged[JOB_QUANTITY_KEY] = max(1, int(job_quantity or 1))
    merged[PARTS_KEY] = parts
    supports = print_supports_summary(parts)
    if supports:
        merged[PRINT_SUPPORTS_KEY] = supports
    else:
        merged.pop(PRINT_SUPPORTS_KEY, None)
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


def stored_print_input_values(booking, analyses) -> dict:
    """The booking's inputs to store after its files or actuals change: B / C refreshed, A kept as the
    Quantity Required (bookings saved before it keep the weight in A)."""
    current = strip_fabrication_keys(dict(booking.input_values or {}))
    merged = inject_print_parts(current, analyses, booking_job_quantity(booking))
    if not parse_bool(current.get(QUANTITY_MARKER_KEY)):
        merged[QUANTITY_KEY] = merged[PRINT_WEIGHT_KEY]
    return strip_fabrication_keys(merged)


def laser_time_estimate(analyses, job_quantity: int = 1, own_material: bool = False):
    """Machine-time estimate of the job (``laser_time_model.JobEstimate``), or None while a part has no measured
    cut path (uploaded before the estimate existed, or not readable) or no sheet material."""
    from .laser_time_model import JobPart, estimate_job, has_features, resolve_profile, unit_factor

    if not analyses:
        return None
    job_quantity = max(1, int(job_quantity or 1))
    parts = []
    for a in analyses:
        if a.status != PrintAnalysisStatus.COMPLETED or not has_features(a.cut_features) or a.material is None:
            return None
        parts.append(
            JobPart(
                key=str(a.id),
                features=a.cut_features,
                unit_mm=unit_factor(a.units),
                material=a.material,
                copies=max(1, int(a.quantity or 1)) * job_quantity,
                area_mm2=float(a.area_mm2 or 0),
            )
        )
    return estimate_job(resolve_profile(analyses[0].equipment), parts, own_material=own_material)


def laser_parts_with_estimate(analyses, job_quantity: int = 1, own_material: bool = False):
    """(parts, JobEstimate or None); each part carries its own ``time_estimate`` row when the job has one."""
    estimate = laser_time_estimate(analyses, job_quantity, own_material)
    parts = build_laser_parts(analyses, job_quantity, estimate=estimate)
    return parts, estimate


def build_laser_parts(analyses, job_quantity: int = 1, *, estimate=None) -> list[dict]:
    """``quantity`` is the part's count in one job; the job is made ``job_quantity`` times."""
    from .laser_cut_service import effective_own_sheet

    job_quantity = max(1, int(job_quantity or 1))
    parts = []
    for a in analyses:
        m = a.material
        own_sheet = effective_own_sheet(a) if a.status == PrintAnalysisStatus.COMPLETED else None
        parts.append(
            {
                "kind": "laser",
                "analysis_id": str(a.id),
                "name": a.display_part_name,
                "filename": a.original_filename,
                "quantity": max(1, int(a.quantity or 1)),
                "job_quantity": job_quantity,
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
                # Sheet size to bring when the booking is "own material": entered by the user or from the drawing.
                "own_sheet_width_mm": own_sheet.as_dict()["width_mm"] if own_sheet else None,
                "own_sheet_height_mm": own_sheet.as_dict()["height_mm"] if own_sheet else None,
                "time_estimate": estimate.parts.get(str(a.id)) if estimate is not None else None,
            }
        )
    return parts


def validate_laser_analyses(
    analyses, *, require_active_material: bool, equipment=None, own_material: bool = False
) -> str | None:
    """``require_active_material`` (new bookings): each sheet must be enabled and, when ``equipment`` is
    given, supported by it. Existing bookings keep the sheets they were booked with.

    ``own_material``: the user supplies the sheet, so parts are not checked against the IIC sheet size."""
    from .fabrication_material_support import bookable_materials
    from .laser_cut_service import sheet_fit_error

    if not analyses:
        return "Upload at least one DXF file."
    bookable_ids = (
        set(bookable_materials(equipment).values_list("pk", flat=True))
        if require_active_material and equipment is not None
        else None
    )
    for a in analyses:
        label = a.display_part_name
        if a.status != PrintAnalysisStatus.COMPLETED:
            return f"{label}: the DXF could not be measured. Remove it or upload a corrected file."
        if not a.material_id or a.material is None:
            return f"{label}: choose a sheet material."
        if require_active_material and not a.material.is_active:
            return f"{label}: the selected sheet material is no longer available. Choose another one."
        if bookable_ids is not None and a.material_id not in bookable_ids:
            return f"{label}: the selected sheet material is no longer available on this machine. Choose another one."
        if not own_material:
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


def merge_laser_booking_into_input_values(equipment, input_values, user, *, laser_cut_batch_id, own_material=False):
    """Validate an unlinked laser batch and inject its parts. Returns (input_values, error, batch).

    ``own_material`` must already be resolved against the equipment (``resolve_own_material``)."""
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
    err = validate_laser_analyses(
        analyses, require_active_material=True, equipment=equipment, own_material=own_material
    )
    if err:
        return input_values, err, None
    merged, qty_err = prepare_new_job_quantity(EquipmentProfileType.LASER_CUT_2D, input_values)
    if qty_err:
        return input_values, qty_err, None
    parts, estimate = laser_parts_with_estimate(analyses, merged[QUANTITY_KEY], own_material)
    merged[PARTS_KEY] = parts
    merged[JOB_QUANTITY_KEY] = merged[QUANTITY_KEY]
    if estimate is not None:
        merged[LASER_ESTIMATE_KEY] = estimate.as_dict()
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
    job_quantity = job_quantity_from_values(profile, merged)
    if profile == EquipmentProfileType.PRINT_3D:
        analyses = active_print_analyses_for_booking(booking)
        if analyses:
            merged = inject_print_parts(merged, analyses, job_quantity)
    else:
        parts, estimate = laser_parts_with_estimate(
            active_laser_analyses_for_booking(booking), job_quantity, booking_own_material(booking)
        )
        merged[PARTS_KEY] = parts
        merged[JOB_QUANTITY_KEY] = job_quantity
        if estimate is not None:
            merged[LASER_ESTIMATE_KEY] = estimate.as_dict()
        minutes = booked_minutes(booking) if getattr(booking, "pk", None) else 0
        if minutes > 0:
            merged[BOOKED_MINUTES_KEY] = minutes
    merged[OWN_MATERIAL_KEY] = booking_own_material(booking)
    return merged


def booking_own_material(booking) -> bool:
    return bool(getattr(booking, "own_material", False)) and own_material_available(getattr(booking, "equipment", None))


def laser_booking_time_estimate(booking) -> dict | None:
    """Job machine-time estimate of a laser booking for display, or None."""
    if getattr(getattr(booking, "equipment", None), "profile_type", None) != EquipmentProfileType.LASER_CUT_2D:
        return None
    estimate = laser_time_estimate(
        active_laser_analyses_for_booking(booking), booking_job_quantity(booking), booking_own_material(booking)
    )
    return estimate.as_dict() if estimate is not None else None


def fabrication_parts_summary(booking) -> list[dict]:
    """Display rows for booking detail, job sheet and emails."""
    equipment = getattr(booking, "equipment", None)
    profile = getattr(equipment, "profile_type", None)
    if profile == EquipmentProfileType.PRINT_3D:
        return build_print_parts(active_print_analyses_for_booking(booking), booking_job_quantity(booking))
    if profile == EquipmentProfileType.LASER_CUT_2D:
        parts, _estimate = laser_parts_with_estimate(
            active_laser_analyses_for_booking(booking), booking_job_quantity(booking), booking_own_material(booking)
        )
        return parts
    return []


def format_seconds(seconds) -> str:
    """'45 s', '12 min 5 s', '2 h 4 min'."""
    total = max(0, int(round(float(seconds or 0))))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def format_part_line(part: dict) -> str:
    qty = part.get("quantity") or 1
    job_quantity = int(part.get("job_quantity") or 1)
    if job_quantity > 1:
        qty = f"{qty} × {job_quantity} sets"
    if part.get("kind") == "laser":
        size = ""
        if part.get("width_mm") and part.get("height_mm"):
            w = Decimal(part["width_mm"]).quantize(Decimal("0.1"))
            h = Decimal(part["height_mm"]).quantize(Decimal("0.1"))
            area = (Decimal(part.get("area_mm2") or 0) / Decimal("1000000")).quantize(Decimal("0.0001"))
            size = f", {w} × {h} mm ({area} m² each)"
        material = part.get("material_name") or part.get("material_code") or "no material"
        est = part.get("time_estimate") or {}
        time_text = ""
        if est.get("seconds_each"):
            time_text = (
                f", est. {format_seconds(est['seconds_each'])} each "
                f"({Decimal(str(est.get('cut_length_mm') or 0)).quantize(Decimal('1'))} mm cut, {est.get('pierces')} pierces)"
            )
        return f"{part.get('name')} × {qty} — {material}{size}{time_text} [{part.get('filename')}]"
    weight = part.get("weight_g_each")
    time_min = part.get("time_min_each")
    est = f", est. {weight} g / {time_min} min each" if weight or time_min else ""
    supports = ""
    if part.get("support_mode"):
        supports = f", supports: {part.get('support_mode_label') or part.get('support_mode')}"
        if part.get("support_weight_g_each"):
            supports += f" (+{part['support_weight_g_each']} g {part.get('support_material_code')} each)"
    oriented = ", user-selected orientation" if part.get("orientation") else ""
    return f"{part.get('name')} × {qty}{est}{supports}{oriented} [{part.get('filename')}]"
