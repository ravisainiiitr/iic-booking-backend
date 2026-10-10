"""Per-machine time estimate settings for 2D laser / profile cutting equipment (Fabrication Materials page)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .laser_time_model import (
    MATERIAL_OVERRIDES_KEY,
    PARAMETER_SPECS,
    PRESETS,
    clean_material_overrides,
    clean_profile_overrides,
    equipment_detected_preset,
    material_cut_params,
    resolve_profile,
    stored_profile,
)

ESTIMATE_PROFILE_KEYS = ("laser_estimate_preset", "laser_estimate_overrides", "laser_estimate_material_overrides")


def profile_payload(equipment) -> Dict[str, Any]:
    """Machine type (chosen / detected), effective parameters with chart defaults, and the cutting speed and
    pierce time of every sheet material the machine supports (chart value and the OIC's override)."""
    from .fabrication_material_support import supported_materials

    stored = stored_profile(equipment)
    effective = resolve_profile(equipment)
    preset = PRESETS[effective["preset"]]
    overrides = stored.get(MATERIAL_OVERRIDES_KEY) or {}
    materials = []
    for m in supported_materials(equipment).order_by("display_order", "name"):
        cut = material_cut_params(effective, m)
        override = overrides.get(str(m.pk)) or {}
        materials.append(
            {
                "material_id": m.pk,
                "code": m.code,
                "name": m.name,
                "material_family": m.material_family,
                "thickness_mm": str(m.thickness_mm),
                "chart_cut_speed_mm_s": round(cut.chart_speed_mm_s, 2),
                "chart_pierce_s": round(cut.chart_pierce_s, 2),
                "cut_speed_mm_s": override.get("cut_speed_mm_s"),
                "pierce_s": override.get("pierce_s"),
                "warning": material_cut_params({**effective, MATERIAL_OVERRIDES_KEY: {}}, m).warning,
            }
        )
    return {
        "preset": stored.get("preset") if stored.get("preset") in PRESETS else "",
        "detected_preset": equipment_detected_preset(equipment),
        "effective_preset": effective["preset"],
        "effective_preset_label": preset["label"],
        "presets": [{"key": k, "label": p["label"]} for k, p in PRESETS.items()],
        "parameters": [
            {
                "key": k,
                "label": spec[0],
                "unit": spec[1],
                "min": spec[2],
                "max": spec[3],
                "value": effective.get(k),
                "default": preset.get(k),
            }
            for k, spec in PARAMETER_SPECS.items()
        ],
        "overrides": {k: v for k, v in (stored.get("overrides") or {}).items() if k in PARAMETER_SPECS},
        "materials": materials,
    }


def apply_profile_update(equipment, data: Dict[str, Any]) -> Optional[str]:
    """Apply ``laser_estimate_preset`` / ``laser_estimate_overrides`` / ``laser_estimate_material_overrides`` from a
    PATCH body to ``equipment.laser_estimate_profile`` (not saved). Returns an error message or None.

    Changing the machine type clears the parameter overrides (they belong to the old type); material speeds are
    kept only when sent again."""
    profile = stored_profile(equipment)
    if "laser_estimate_preset" in data:
        preset = str(data.get("laser_estimate_preset") or "").strip()
        if preset and preset not in PRESETS:
            return "Unknown machine type."
        old = resolve_profile(equipment, profile)["preset"]
        if preset:
            profile["preset"] = preset
        else:
            profile.pop("preset", None)
        if resolve_profile(equipment, profile)["preset"] != old:
            profile.pop("overrides", None)
            profile.pop(MATERIAL_OVERRIDES_KEY, None)
    if "laser_estimate_overrides" in data:
        overrides, err = clean_profile_overrides(data.get("laser_estimate_overrides"))
        if err:
            return err
        if overrides:
            profile["overrides"] = overrides
        else:
            profile.pop("overrides", None)
    if "laser_estimate_material_overrides" in data:
        materials, err = clean_material_overrides(data.get("laser_estimate_material_overrides"))
        if err:
            return err
        if materials:
            profile[MATERIAL_OVERRIDES_KEY] = materials
        else:
            profile.pop(MATERIAL_OVERRIDES_KEY, None)
    effective = resolve_profile(equipment, profile)
    if float(effective["bed_width_mm"]) <= 0 or float(effective["bed_height_mm"]) <= 0:
        return "Enter the bed size in mm."
    equipment.laser_estimate_profile = profile or None
    return None
