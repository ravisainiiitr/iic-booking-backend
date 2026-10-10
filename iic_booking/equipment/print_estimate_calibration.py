"""Per-printer estimate profile for the OIC page, and calibration factors fitted from staff-entered actuals."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from django.utils import timezone

from .print_estimate_model import (
    CALIBRATION_KEY,
    PARAMETER_SPECS,
    PRESETS,
    SUPPORT_OPTIONS_KEY,
    TECHNOLOGY_LABELS,
    clean_profile_overrides,
    clean_support_options,
    detect_preset,
    estimate,
    fit_calibration,
    resolve_profile,
    resolved_support_options,
    stored_profile,
    supports_offered,
)

logger = logging.getLogger(__name__)

MAX_CALIBRATION_PARTS = 200
_FDM_ONLY = {
    "line_width_mm", "wall_count", "top_thickness_mm", "bottom_thickness_mm", "perimeter_speed_mm_s",
    "infill_speed_mm_s", "solid_infill_speed_mm_s", "support_speed_mm_s", "acceleration_mm_s2", "corner_speed_mm_s",
    "max_flow_mm3_s", "infill_segment_mm", "layer_overhead_s", "min_layer_time_s", "infill_pattern_factor",
    "support_interface_layers", "support_layer_overhead_s", "toolchange_s", "toolchange_purge_g",
    "brim_width_mm", "raft_margin_mm", "raft_density_pct",
}
SUPPORT_MATERIAL_IDS_KEY = "support_material_ids"
_LAYERED_ONLY = {"per_layer_s", "bottom_layers", "bottom_layer_s", "area_s_per_cm2", "lift_mm",
                 "support_material_density_g_cm3"}


def parameter_keys(technology: str) -> List[str]:
    skip = _LAYERED_ONLY if technology == "FDM" else _FDM_ONLY
    return [k for k in PARAMETER_SPECS if k not in skip]


def profile_payload(equipment) -> Dict[str, Any]:
    """What the OIC page shows: preset (chosen / detected), effective parameters, overrides and calibration."""
    stored = stored_profile(equipment)
    detected = detect_preset(equipment.make, equipment.model_information, equipment.name)
    effective = resolve_profile(equipment)
    tech = effective.get("technology", "FDM")
    keys = parameter_keys(tech)
    return {
        "preset": stored.get("preset") if stored.get("preset") in PRESETS else "",
        "detected_preset": detected,
        "effective_preset": effective["preset"],
        "technology": tech,
        "technology_label": TECHNOLOGY_LABELS.get(tech, tech),
        "presets": [
            {"key": k, "label": p["label"], "technology": p["technology"]} for k, p in PRESETS.items()
        ],
        "parameters": [
            {
                "key": k,
                "label": PARAMETER_SPECS[k][0],
                "unit": PARAMETER_SPECS[k][1],
                "min": PARAMETER_SPECS[k][2],
                "max": PARAMETER_SPECS[k][3],
                "kind": "bool" if PARAMETER_SPECS[k][2] is None else "number",
                "value": effective.get(k),
                "default": PRESETS[effective["preset"]].get(k),
            }
            for k in keys
        ],
        "overrides": {k: v for k, v in (stored.get("overrides") or {}).items() if k in keys},
        "calibration": stored.get(CALIBRATION_KEY) or None,
        "support_material_ids": [int(i) for i in stored.get(SUPPORT_MATERIAL_IDS_KEY) or []],
        "supports_available": supports_offered(tech),
        "support_options": resolved_support_options(stored, tech),
    }


def calibration_samples(equipment, profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, float]]:
    """Per-copy estimate (without calibration) vs actual for the printer's booked parts with actuals.

    Actuals equal to the estimate are skipped: the actuals form starts from the estimate, so an unchanged value
    says nothing about the real print.
    """
    from .fabrication import booking_job_quantity
    from .models import PrintAnalysis
    from .print_3d_service import analysis_features, support_options

    base = dict(stored_profile(equipment))
    base.pop(CALIBRATION_KEY, None)
    profile = profile or resolve_profile(equipment, base)
    qs = (
        PrintAnalysis.objects.filter(equipment=equipment, booking__isnull=False, cancelled_at__isnull=True)
        .exclude(actual_weight_grams__isnull=True, actual_time_minutes__isnull=True)
        .select_related("material", "booking")
        .order_by("-updated_at")[:MAX_CALIBRATION_PARTS]
    )
    samples = []
    for a in qs:
        copies = max(1, int(a.quantity or 1)) * booking_job_quantity(a.booking)
        density = float(a.material.density_g_per_cm3) if a.material else 1.24
        infill = float((a.slicer_settings or {}).get("infill_percent") or 100.0)
        try:
            b = estimate(
                analysis_features(a),
                profile,
                infill_percent=infill,
                density_g_cm3=density,
                supports=support_options(a.slicer_settings),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calibration estimate failed for analysis %s: %s", a.pk, exc)
            continue
        sample: Dict[str, float] = {}
        est_w_total = int(a.weight_grams or 0) * copies
        if a.actual_weight_grams is not None and int(a.actual_weight_grams) != est_w_total:
            # Actual weight is the model material (separate support material is not weighed).
            sample["est_g"] = b.model_g + b.adhesion_g + (0.0 if b.support_separate else b.support_g)
            sample["act_g"] = float(a.actual_weight_grams) / copies - b.waste_g
        est_t_total = int(a.estimated_time_minutes or 0) * copies
        if a.actual_time_minutes is not None and int(a.actual_time_minutes) != est_t_total:
            sample["est_min"] = b.print_min
            sample["act_min"] = float(a.actual_time_minutes) / copies - b.warmup_min
        if sample:
            samples.append(sample)
    return samples


def fit_equipment_calibration(equipment) -> Dict[str, Any]:
    fitted = fit_calibration(calibration_samples(equipment))
    fitted["fitted_at"] = timezone.now().isoformat()
    fitted["applied"] = False
    return fitted


def apply_profile_update(equipment, data: Dict[str, Any]) -> Optional[str]:
    """Apply ``print_estimate_preset`` / ``print_estimate_overrides`` / ``print_estimate_calibration`` /
    ``print_estimate_support_material_ids`` / ``print_estimate_support_options`` from a PATCH body to
    ``equipment.print_estimate_profile`` (not saved). Returns an error message or None."""
    profile = stored_profile(equipment)
    if "print_estimate_preset" in data:
        preset = str(data.get("print_estimate_preset") or "").strip()
        if preset and preset not in PRESETS:
            return "Unknown printer type preset."
        old_tech = resolve_profile(equipment, profile).get("technology")
        if preset:
            profile["preset"] = preset
        else:
            profile.pop("preset", None)
        if resolve_profile(equipment, profile).get("technology") != old_tech:
            profile.pop("overrides", None)
    if "print_estimate_overrides" in data:
        overrides, err = clean_profile_overrides(data.get("print_estimate_overrides"))
        if err:
            return err
        profile["overrides"] = overrides
    if "print_estimate_support_material_ids" in data:
        from .models import PrintMaterial

        raw = data.get("print_estimate_support_material_ids") or []
        if not isinstance(raw, list):
            return "Support materials must be a list of material ids."
        try:
            ids = sorted({int(i) for i in raw})
        except (TypeError, ValueError):
            return "Support materials must be a list of material ids."
        known = set(PrintMaterial.objects.filter(pk__in=ids, is_active=True).values_list("pk", flat=True))
        if set(ids) - known:
            return "Unknown or inactive support material."
        if ids:
            profile[SUPPORT_MATERIAL_IDS_KEY] = ids
        else:
            profile.pop(SUPPORT_MATERIAL_IDS_KEY, None)
    if "print_estimate_support_options" in data:
        tech = resolve_profile(equipment, profile).get("technology", "FDM")
        options, err = clean_support_options(
            data.get("print_estimate_support_options"), tech, profile.get(SUPPORT_OPTIONS_KEY)
        )
        if err:
            return err
        if options:
            profile[SUPPORT_OPTIONS_KEY] = options
        else:
            profile.pop(SUPPORT_OPTIONS_KEY, None)
    action = data.get("print_estimate_calibration")
    if action:
        action = str(action).strip().lower()
        calibration = dict(profile.get(CALIBRATION_KEY) or {})
        if action == "fit":
            equipment.print_estimate_profile = profile
            calibration = fit_equipment_calibration(equipment)
        elif action == "apply":
            if not calibration.get("weight_factor") and not calibration.get("time_factor"):
                return "Fit the calibration from actual prints first (not enough parts with actual weight or time)."
            calibration["applied"] = True
        elif action == "off":
            calibration["applied"] = False
        else:
            return "Calibration action must be fit, apply or off."
        profile[CALIBRATION_KEY] = calibration
    equipment.print_estimate_profile = profile or None
    return None
