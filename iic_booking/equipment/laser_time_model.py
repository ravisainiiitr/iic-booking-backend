"""Machine-time estimates for 2D profile cutting (laser, waterjet, plasma, router, VMC) from the DXF toolpath.

Geometry (once per uploaded DXF, stored in ``LaserCutAnalysis.cut_features`` in drawing units):
every cut entity becomes line / curve pieces, overlapping duplicates are dropped, and pieces are chained into
contours through shared end points (one pierce per contour). Each piece keeps its length, curve radius and the
direction change at its end. Contours are visited nearest-first from the sheet corner for the rapid moves.
Pieces and rapid moves are kept as small histograms, so the estimate can be re-run for any machine, material
or unit choice without reading the file again.

Estimate per copy of a part:

* cutting: every piece at the material's cutting speed, slowed on curves to sqrt(acceleration x radius), and
  accelerating / braking to the corner speed the direction change allows (junction deviation, as in CNC
  motion planners);
* piercing: per contour, the material's pierce time plus the head settle time (height sensing, gas, plunge);
* rapid moves between contours at the rapid speed, with acceleration.

Per job: setup, loading of the sheets the parts need (part area / nesting efficiency) and a time allowance.
Every number is a profile parameter: ``PRESETS`` holds defaults per machine type (published cutting charts of
typical machines) and the equipment's ``laser_estimate_profile`` overrides them, per machine and per material.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

FEATURES_VERSION = 1
LEN_BINS_PER_DECADE = 6
RADIUS_BINS_PER_DECADE = 4
# Direction change at the end of a piece, in degrees; the last bucket runs to 180 (full reversal / stop).
TURN_EDGES = (0.0, 2.0, 10.0, 20.0, 35.0, 50.0, 70.0, 95.0, 125.0, 155.0)
CORNER_DEG = 35.0
STOP_DEG = 180.0
BEZIER_SAMPLES = 8
MAX_NN_CONTOURS = 3000
ENTRY_SAMPLES = 8
# Curves flatter than this many drawing-size units are treated as straight.
STRAIGHT_RADIUS_FACTOR = 1000.0


# --------------------------------------------------------------------------------------------- presets

FAMILIES = ("MS", "SS", "ACRYLIC", "MDF", "OTHER")
FAMILY_LABELS = {
    "MS": "mild steel",
    "SS": "stainless steel",
    "ACRYLIC": "acrylic",
    "MDF": "MDF",
    "OTHER": "this material",
}

# Each table row: (thickness mm, cutting speed mm/s, pierce / plunge seconds). Speeds are for a clean
# production cut on a typical machine of the class; the OIC can scale or override them per machine / material.
PRESETS: Dict[str, Dict[str, Any]] = {
    "co2_laser": {
        "label": "CO2 laser cutter (non-metals, 80-150 W)",
        "cuts": ("ACRYLIC", "MDF", "OTHER"),
        "rapid_speed_mm_s": 400.0,
        "acceleration_mm_s2": 2500.0,
        "junction_deviation_mm": 0.05,
        "pierce_settle_s": 0.3,
        "setup_min": 10.0,
        "sheet_load_min": 3.0,
        "bed_width_mm": 1300.0,
        "bed_height_mm": 900.0,
        "nesting_efficiency_pct": 75.0,
        "allowance_pct": 10.0,
        "cut_speed_factor_pct": 100.0,
        "materials": {
            "ACRYLIC": [(1, 30, 0.1), (2, 22, 0.15), (3, 16, 0.2), (5, 9, 0.35), (6, 7, 0.4), (8, 4.5, 0.6),
                        (10, 3, 0.8), (12, 2, 1.0), (15, 1.3, 1.5), (20, 0.8, 2.0)],
            "MDF": [(2, 25, 0.15), (3, 18, 0.2), (4, 13, 0.3), (6, 8, 0.5), (9, 4, 0.8), (12, 2.5, 1.2),
                    (18, 1.2, 2.0)],
            "OTHER": [(1, 40, 0.1), (3, 15, 0.25), (6, 7, 0.5), (9, 4, 0.8), (12, 2.5, 1.2)],
            "MS": [(0.5, 10, 0.5), (1, 5, 1.0), (2, 2, 2.0)],
            "SS": [(0.5, 8, 0.5), (1, 4, 1.0), (2, 1.5, 2.0)],
        },
    },
    "fiber_laser": {
        "label": "Fibre laser cutter (metals, 1.5-3 kW)",
        "cuts": ("MS", "SS", "OTHER"),
        "rapid_speed_mm_s": 1000.0,
        "acceleration_mm_s2": 6000.0,
        "junction_deviation_mm": 0.02,
        "pierce_settle_s": 0.6,
        "setup_min": 15.0,
        "sheet_load_min": 5.0,
        "bed_width_mm": 3000.0,
        "bed_height_mm": 1500.0,
        "nesting_efficiency_pct": 80.0,
        "allowance_pct": 10.0,
        "cut_speed_factor_pct": 100.0,
        "materials": {
            "MS": [(1, 140, 0.2), (2, 85, 0.35), (3, 55, 0.5), (4, 45, 0.7), (5, 36, 0.9), (6, 30, 1.2),
                   (8, 20, 1.8), (10, 15, 2.5), (12, 12, 3.2), (16, 8, 4.5), (20, 6, 6.0)],
            "SS": [(1, 330, 0.1), (2, 150, 0.2), (3, 75, 0.35), (4, 38, 0.5), (5, 22, 0.7), (6, 15, 0.9),
                   (8, 7, 1.5)],
            "OTHER": [(1, 250, 0.1), (2, 110, 0.25), (3, 55, 0.4), (4, 30, 0.6), (5, 18, 0.8), (6, 12, 1.0)],
            "ACRYLIC": [(2, 10, 0.5), (3, 6, 0.8), (5, 3, 1.2)],
            "MDF": [(3, 20, 0.3), (6, 8, 0.6)],
        },
    },
    "waterjet": {
        "label": "Abrasive waterjet (any material)",
        "cuts": FAMILIES,
        "rapid_speed_mm_s": 150.0,
        "acceleration_mm_s2": 1000.0,
        "junction_deviation_mm": 0.02,
        "pierce_settle_s": 1.5,
        "setup_min": 20.0,
        "sheet_load_min": 10.0,
        "bed_width_mm": 3000.0,
        "bed_height_mm": 1500.0,
        "nesting_efficiency_pct": 75.0,
        "allowance_pct": 15.0,
        "cut_speed_factor_pct": 100.0,
        "materials": {
            "MS": [(1, 20, 2.0), (2, 13, 3.0), (3, 9, 4.0), (5, 5.5, 6.0), (6, 4.5, 7.0), (10, 2.5, 11.0),
                   (12, 2.0, 13.0), (20, 1.1, 22.0), (25, 0.85, 28.0)],
            "SS": [(1, 17, 2.2), (2, 11, 3.3), (3, 7.5, 4.5), (5, 4.6, 6.6), (6, 3.8, 7.7), (10, 2.1, 12.0),
                   (12, 1.7, 14.0), (20, 0.95, 24.0)],
            "OTHER": [(1, 50, 1.0), (3, 22, 2.0), (6, 11, 3.5), (10, 6.5, 5.5), (20, 3, 10.0)],
            "ACRYLIC": [(2, 50, 2.0), (3, 40, 2.5), (5, 28, 3.0), (10, 13, 4.0), (20, 6, 6.0)],
            "MDF": [(3, 60, 1.0), (6, 35, 1.5), (12, 18, 2.5), (18, 12, 3.5)],
        },
    },
    "plasma": {
        "label": "CNC plasma cutter (conductive metals)",
        "cuts": ("MS", "SS", "OTHER"),
        "rapid_speed_mm_s": 250.0,
        "acceleration_mm_s2": 1500.0,
        "junction_deviation_mm": 0.05,
        "pierce_settle_s": 1.0,
        "setup_min": 10.0,
        "sheet_load_min": 5.0,
        "bed_width_mm": 3000.0,
        "bed_height_mm": 1500.0,
        "nesting_efficiency_pct": 75.0,
        "allowance_pct": 10.0,
        "cut_speed_factor_pct": 100.0,
        "materials": {
            "MS": [(1, 130, 0.3), (2, 110, 0.4), (3, 75, 0.5), (5, 50, 0.7), (6, 40, 0.8), (10, 25, 1.2),
                   (12, 20, 1.5), (16, 13, 2.0), (20, 9, 2.5)],
            "SS": [(1, 120, 0.3), (3, 65, 0.5), (6, 33, 0.9), (10, 20, 1.4), (12, 16, 1.7)],
            "OTHER": [(1, 140, 0.3), (3, 85, 0.5), (6, 45, 0.8), (10, 28, 1.2)],
            "ACRYLIC": [(3, 5, 2.0), (6, 3, 3.0)],
            "MDF": [(3, 5, 2.0), (6, 3, 3.0)],
        },
    },
    "cnc_router": {
        "label": "CNC router (wood, MDF, plastics; profile in depth passes)",
        "cuts": ("ACRYLIC", "MDF", "OTHER"),
        "rapid_speed_mm_s": 150.0,
        "acceleration_mm_s2": 800.0,
        "junction_deviation_mm": 0.05,
        "pierce_settle_s": 1.0,
        "setup_min": 20.0,
        "sheet_load_min": 10.0,
        "bed_width_mm": 2440.0,
        "bed_height_mm": 1220.0,
        "nesting_efficiency_pct": 70.0,
        "allowance_pct": 15.0,
        "cut_speed_factor_pct": 100.0,
        "materials": {
            "MDF": [(3, 40, 1.5), (6, 25, 2.5), (9, 16, 3.5), (12, 12, 4.5), (18, 8, 6.5)],
            "OTHER": [(3, 35, 1.5), (6, 22, 2.5), (9, 14, 3.5), (12, 11, 4.5), (18, 7, 6.5)],
            "ACRYLIC": [(2, 30, 1.5), (3, 25, 2.0), (5, 16, 3.0), (6, 13, 3.5), (10, 8, 5.5)],
            "MS": [(1, 4, 4.0), (2, 2.5, 6.0), (3, 1.8, 8.0)],
            "SS": [(1, 2.5, 5.0), (2, 1.5, 8.0)],
        },
    },
    "vmc_milling": {
        "label": "CNC milling / VMC (2D profiles in depth passes)",
        "cuts": FAMILIES,
        "rapid_speed_mm_s": 250.0,
        "acceleration_mm_s2": 1500.0,
        "junction_deviation_mm": 0.02,
        "pierce_settle_s": 2.0,
        "setup_min": 30.0,
        "sheet_load_min": 15.0,
        "bed_width_mm": 1000.0,
        "bed_height_mm": 500.0,
        "nesting_efficiency_pct": 70.0,
        "allowance_pct": 15.0,
        "cut_speed_factor_pct": 100.0,
        "materials": {
            "MS": [(1, 8, 4.0), (2, 5, 6.0), (3, 3.5, 8.0), (5, 2.2, 12.0), (6, 1.8, 14.0), (10, 1.1, 22.0),
                   (12, 0.9, 26.0)],
            "SS": [(1, 5, 5.0), (2, 3, 8.0), (3, 2.1, 11.0), (5, 1.3, 16.0), (6, 1.1, 19.0), (10, 0.65, 30.0)],
            "OTHER": [(1, 25, 2.0), (3, 12, 4.0), (6, 6, 7.0), (10, 3.5, 11.0), (12, 3, 13.0)],
            "ACRYLIC": [(3, 20, 2.0), (6, 10, 3.5), (10, 6, 5.5)],
            "MDF": [(3, 30, 1.5), (6, 18, 2.5), (12, 9, 4.5)],
        },
    },
}
DEFAULT_PRESET = "co2_laser"

# Editable machine parameters: key -> (label, unit, min, max).
PARAMETER_SPECS: Dict[str, Tuple[str, str, float, float]] = {
    "cut_speed_factor_pct": ("Cutting speed compared with the built-in chart", "%", 10, 500),
    "rapid_speed_mm_s": ("Rapid (travel) speed", "mm/s", 5, 5000),
    "acceleration_mm_s2": ("Acceleration", "mm/s²", 50, 50000),
    "junction_deviation_mm": ("Corner tolerance (junction deviation)", "mm", 0.001, 2),
    "pierce_settle_s": ("Head settle per pierce (height sensing, gas, plunge)", "s", 0, 60),
    "setup_min": ("Setup per job (file, focus, test cut)", "min", 0, 600),
    "sheet_load_min": ("Load and unload per sheet", "min", 0, 240),
    "bed_width_mm": ("Bed width", "mm", 50, 20000),
    "bed_height_mm": ("Bed height", "mm", 50, 20000),
    "nesting_efficiency_pct": ("Nesting efficiency (sheet area used)", "%", 20, 100),
    "allowance_pct": ("Time allowance", "%", 0, 200),
}
MATERIAL_OVERRIDES_KEY = "material_overrides"
MATERIAL_SPEED_RANGE = (0.05, 5000.0)
MATERIAL_PIERCE_RANGE = (0.0, 600.0)


def detect_preset(make: str = "", model: str = "", name: str = "", code: str = "") -> str:
    """Default machine type from the equipment's make / model / name / code text."""
    text = " ".join(str(v or "") for v in (make, model, name, code)).lower()
    rules = (
        (r"water\s*-?\s*jet|abrasive|\bhpw|high[\s-]*pressure\s*water|\bomax\b|\bawj\b", "waterjet"),
        (r"plasma|hypertherm", "plasma"),
        (r"\bvmc|vertical\s*machining|machining\s*cent|milling|\bmill\b", "vmc_milling"),
        (r"router|\bcncr|spindle|wood\s*cnc", "cnc_router"),
        (r"fib(?:er|re)|metal\s*laser|\bmlc|\bipg\b|raycus|trumpf|bystronic|amada|bodor|\bhan'?s\b", "fiber_laser"),
        (r"co2|co₂|laser|epilog|trotec|universal\s*laser|glowforge|\bboss\b", "co2_laser"),
    )
    for pattern, preset in rules:
        if re.search(pattern, text):
            return preset
    return DEFAULT_PRESET


def stored_profile(equipment) -> Dict[str, Any]:
    raw = getattr(equipment, "laser_estimate_profile", None) if equipment is not None else None
    return dict(raw) if isinstance(raw, dict) else {}


def equipment_detected_preset(equipment) -> str:
    return detect_preset(
        getattr(equipment, "make", ""),
        getattr(equipment, "model_information", ""),
        getattr(equipment, "name", ""),
        getattr(equipment, "code", ""),
    )


def resolve_profile(equipment=None, stored: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Preset defaults for the machine with the equipment's saved overrides applied."""
    stored = dict(stored) if stored is not None else stored_profile(equipment)
    preset_key = stored.get("preset")
    if preset_key not in PRESETS:
        preset_key = equipment_detected_preset(equipment) if equipment is not None else DEFAULT_PRESET
    profile = {k: v for k, v in PRESETS[preset_key].items()}
    profile["preset"] = preset_key
    for key, value in (stored.get("overrides") or {}).items():
        if key in PARAMETER_SPECS and value is not None:
            profile[key] = value
    profile[MATERIAL_OVERRIDES_KEY] = dict(stored.get(MATERIAL_OVERRIDES_KEY) or {})
    return profile


def _bounded_number(value, label, unit, lo, hi) -> Tuple[Optional[float], Optional[str]]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{label} must be a number."
    if not math.isfinite(number) or number < lo or number > hi:
        return None, f"{label} must be between {lo:g} and {hi:g}{(' ' + unit) if unit else ''}."
    return number, None


def clean_profile_overrides(raw: Any) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """Validate machine parameter overrides from the API. Blank / None values are dropped."""
    if raw in (None, ""):
        return {}, None
    if not isinstance(raw, dict):
        return None, "Send the estimate parameters as an object."
    cleaned: Dict[str, float] = {}
    for key, value in raw.items():
        if key not in PARAMETER_SPECS:
            return None, f"Unknown estimate parameter '{key}'."
        if value in (None, ""):
            continue
        number, err = _bounded_number(value, *PARAMETER_SPECS[key])
        if err:
            return None, err
        cleaned[key] = number
    return cleaned, None


def clean_material_overrides(raw: Any) -> Tuple[Optional[Dict[str, Dict[str, float]]], Optional[str]]:
    """{material_id: {"cut_speed_mm_s", "pierce_s"}} with blank values dropped (blank = use the chart)."""
    if raw in (None, ""):
        return {}, None
    if not isinstance(raw, dict):
        return None, "Send the material speeds as an object keyed by material id."
    cleaned: Dict[str, Dict[str, float]] = {}
    for key, row in raw.items():
        try:
            material_id = str(int(key))
        except (TypeError, ValueError):
            return None, "Material speeds must be keyed by material id."
        if row in (None, ""):
            continue
        if not isinstance(row, dict):
            return None, "Each material speed must be an object."
        out: Dict[str, float] = {}
        speed = row.get("cut_speed_mm_s")
        if speed not in (None, ""):
            number, err = _bounded_number(speed, "Cutting speed", "mm/s", *MATERIAL_SPEED_RANGE)
            if err:
                return None, err
            out["cut_speed_mm_s"] = number
        pierce = row.get("pierce_s")
        if pierce not in (None, ""):
            number, err = _bounded_number(pierce, "Pierce time", "s", *MATERIAL_PIERCE_RANGE)
            if err:
                return None, err
            out["pierce_s"] = number
        if out:
            cleaned[material_id] = out
    return cleaned, None


def _interp_log(table: List[Tuple[float, float, float]], thickness: float) -> Tuple[float, float]:
    """Cutting speed (log-log) and pierce time (linear) at ``thickness`` from a chart, extrapolating the end
    segments (speed falls roughly with a power of the thickness)."""
    rows = sorted(table)
    t = max(float(thickness), 0.05)
    if len(rows) == 1:
        th, sp, pi = rows[0]
        return sp * th / t, pi * t / th
    if t <= rows[0][0]:
        (t0, s0, p0), (t1, s1, p1) = rows[0], rows[1]
    elif t >= rows[-1][0]:
        (t0, s0, p0), (t1, s1, p1) = rows[-2], rows[-1]
    else:
        i = bisect.bisect_right([r[0] for r in rows], t)
        (t0, s0, p0), (t1, s1, p1) = rows[i - 1], rows[i]
    k = math.log(s1 / s0) / math.log(t1 / t0)
    speed = s0 * (t / t0) ** k
    pierce = p0 + (p1 - p0) * (t - t0) / (t1 - t0)
    return speed, max(0.0, pierce)


@dataclass
class MaterialCut:
    speed_mm_s: float
    pierce_s: float
    chart_speed_mm_s: float
    chart_pierce_s: float
    overridden: bool
    warning: str = ""


def material_cut_params(profile: Dict[str, Any], material) -> MaterialCut:
    """Cutting speed and pierce time for a sheet material on this machine (override, else the chart)."""
    family = getattr(material, "material_family", None) or "OTHER"
    if family not in FAMILIES:
        family = "OTHER"
    thickness = float(getattr(material, "thickness_mm", None) or 1)
    table = (profile.get("materials") or {}).get(family) or PRESETS[DEFAULT_PRESET]["materials"]["OTHER"]
    speed, pierce = _interp_log(table, thickness)
    factor = float(profile.get("cut_speed_factor_pct") or 100.0) / 100.0
    chart_speed = max(MATERIAL_SPEED_RANGE[0], speed * factor)
    chart_pierce = pierce
    override = (profile.get(MATERIAL_OVERRIDES_KEY) or {}).get(str(getattr(material, "pk", "")), {}) or {}
    final_speed = float(override.get("cut_speed_mm_s") or chart_speed)
    final_pierce = float(override["pierce_s"]) if override.get("pierce_s") is not None else chart_pierce
    warning = ""
    if family not in profile.get("cuts", FAMILIES) and not override:
        label = PRESETS.get(profile.get("preset"), {}).get("label", "this machine")
        warning = (
            f"This machine type ({label.split(' (')[0]}) does not normally cut {FAMILY_LABELS[family]}; "
            "the cutting time is a rough guess."
        )
    return MaterialCut(final_speed, final_pierce, chart_speed, chart_pierce, bool(override), warning)


# --------------------------------------------------------------------------------------------- geometry


@dataclass
class _Piece:
    start: Tuple[float, float]
    end: Tuple[float, float]
    length: float
    radius: float  # 0 = straight
    t0: Tuple[float, float]  # unit tangent at the start
    t1: Tuple[float, float]  # unit tangent at the end
    mid: Tuple[float, float]


def _unit(dx: float, dy: float) -> Optional[Tuple[float, float]]:
    n = math.hypot(dx, dy)
    if n <= 0 or not math.isfinite(n):
        return None
    return dx / n, dy / n


def _bezier(p0, c1, c2, p3, t):
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p3[0], a * p0[1] + b * c1[1] + c * c2[1] + d * p3[1])


def _bezier_radius(p0, c1, c2, p3, t) -> float:
    u = 1.0 - t
    d1 = (
        3 * u * u * (c1[0] - p0[0]) + 6 * u * t * (c2[0] - c1[0]) + 3 * t * t * (p3[0] - c2[0]),
        3 * u * u * (c1[1] - p0[1]) + 6 * u * t * (c2[1] - c1[1]) + 3 * t * t * (p3[1] - c2[1]),
    )
    d2 = (
        6 * u * (c2[0] - 2 * c1[0] + p0[0]) + 6 * t * (p3[0] - 2 * c2[0] + c1[0]),
        6 * u * (c2[1] - 2 * c1[1] + p0[1]) + 6 * t * (p3[1] - 2 * c2[1] + c1[1]),
    )
    cross = abs(d1[0] * d2[1] - d1[1] * d2[0])
    speed = math.hypot(*d1)
    if cross <= 1e-15 or speed <= 0:
        return math.inf
    return speed ** 3 / cross


def _line_piece(a, b) -> Optional[_Piece]:
    t = _unit(b[0] - a[0], b[1] - a[1])
    if t is None:
        return None
    return _Piece(a, b, math.hypot(b[0] - a[0], b[1] - a[1]), 0.0, t, t, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))


def _curve_piece(p0, c1, c2, p3, straight_radius: float) -> Optional[_Piece]:
    pts = [_bezier(p0, c1, c2, p3, i / BEZIER_SAMPLES) for i in range(BEZIER_SAMPLES + 1)]
    length = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(BEZIER_SAMPLES))
    if length <= 0:
        return None
    t0 = _unit(c1[0] - p0[0], c1[1] - p0[1]) or _unit(c2[0] - p0[0], c2[1] - p0[1]) or _unit(
        pts[1][0] - p0[0], pts[1][1] - p0[1]
    )
    t1 = _unit(p3[0] - c2[0], p3[1] - c2[1]) or _unit(p3[0] - c1[0], p3[1] - c1[1]) or _unit(
        p3[0] - pts[-2][0], p3[1] - pts[-2][1]
    )
    if t0 is None or t1 is None:
        return None
    radius = min(_bezier_radius(p0, c1, c2, p3, t) for t in (0.25, 0.5, 0.75))
    if not math.isfinite(radius) or radius > straight_radius:
        radius = 0.0
    return _Piece(p0, p3, length, radius, t0, t1, pts[BEZIER_SAMPLES // 2])


def _xy(v) -> Tuple[float, float]:
    return float(v[0]), float(v[1])


def pieces_from_entities(entities: Iterable, straight_radius: float) -> List[_Piece]:
    from ezdxf import path as ezpath
    from ezdxf.path import Command

    pieces: List[_Piece] = []
    for entity in entities:
        try:
            p = ezpath.make_path(entity)
        except Exception:  # noqa: BLE001 - an entity without a path (e.g. degenerate) adds no cut
            continue
        cur = _xy(p.start)
        for cmd in p.commands():
            end = _xy(cmd.end)
            piece = None
            if cmd.type == Command.LINE_TO:
                piece = _line_piece(cur, end)
            elif cmd.type == Command.CURVE4_TO:
                piece = _curve_piece(cur, _xy(cmd.ctrl1), _xy(cmd.ctrl2), end, straight_radius)
            elif cmd.type == Command.CURVE3_TO:
                c = _xy(cmd.ctrl)
                c1 = (cur[0] + 2 / 3 * (c[0] - cur[0]), cur[1] + 2 / 3 * (c[1] - cur[1]))
                c2 = (end[0] + 2 / 3 * (c[0] - end[0]), end[1] + 2 / 3 * (c[1] - end[1]))
                piece = _curve_piece(cur, c1, c2, end, straight_radius)
            if piece is not None:
                pieces.append(piece)
            cur = end
    return pieces


def _dedupe(pieces: List[_Piece], q: float) -> Tuple[List[_Piece], int]:
    def key_pt(pt):
        return round(pt[0] / q), round(pt[1] / q)

    seen = set()
    kept = []
    for piece in pieces:
        a, b = key_pt(piece.start), key_pt(piece.end)
        key = (min(a, b), max(a, b), key_pt(piece.mid), piece.radius > 0)
        if key in seen:
            continue
        seen.add(key)
        kept.append(piece)
    return kept, len(pieces) - len(kept)


class _NodeGrid:
    """Snaps end points within ``q`` of each other to one node."""

    def __init__(self, q: float):
        self.q = q
        self.cells: Dict[Tuple[int, int], int] = {}
        self.pos: List[Tuple[float, float]] = []

    def node(self, pt: Tuple[float, float]) -> int:
        ix, iy = round(pt[0] / self.q), round(pt[1] / self.q)
        for dx in (0, -1, 1):
            for dy in (0, -1, 1):
                found = self.cells.get((ix + dx, iy + dy))
                if found is not None:
                    return found
        idx = len(self.pos)
        self.cells[(ix, iy)] = idx
        self.pos.append(pt)
        return idx


def _turn_deg(u: Tuple[float, float], v: Tuple[float, float]) -> float:
    dot = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(dot))


@dataclass
class _Contour:
    pieces: List[Tuple[float, float, float]]  # (length, radius, turn at end in degrees)
    closed: bool
    entries: List[Tuple[float, float]]
    exits: List[Tuple[float, float]]


def chain_contours(pieces: List[_Piece], q: float) -> List[_Contour]:
    """Walk the pieces into contours through shared end points, going straight on where a node branches."""
    grid = _NodeGrid(q)
    ends = [(grid.node(p.start), grid.node(p.end)) for p in pieces]
    adj: Dict[int, List[int]] = {}
    for i, (a, b) in enumerate(ends):
        adj.setdefault(a, []).append(i)
        if b != a:
            adj.setdefault(b, []).append(i)
    used = [False] * len(pieces)
    odd = [n for n, edges in adj.items() if len(edges) % 2 == 1]
    contours: List[_Contour] = []

    def walk(start: int) -> _Contour:
        node, direction = start, None
        oriented: List[Tuple[_Piece, Tuple[float, float], Tuple[float, float]]] = []
        visited = [grid.pos[start]]
        while True:
            best, best_turn = None, None
            for i in adj.get(node, ()):
                if used[i]:
                    continue
                forward = ends[i][0] == node
                t_in = pieces[i].t0 if forward else (-pieces[i].t1[0], -pieces[i].t1[1])
                turn = 0.0 if direction is None else _turn_deg(direction, t_in)
                if best is None or turn < best_turn:
                    best, best_turn = (i, forward, t_in), turn
                    if direction is None:
                        break
            if best is None:
                break
            i, forward, t_in = best
            used[i] = True
            t_out = pieces[i].t1 if forward else (-pieces[i].t0[0], -pieces[i].t0[1])
            oriented.append((pieces[i], t_in, t_out))
            node = ends[i][1] if forward else ends[i][0]
            direction = t_out
            visited.append(grid.pos[node])
        closed = node == start and len(oriented) > 0
        rows = []
        for j, (piece, _t_in, t_out) in enumerate(oriented):
            if j + 1 < len(oriented):
                turn = _turn_deg(t_out, oriented[j + 1][1])
            elif closed:
                turn = _turn_deg(t_out, oriented[0][1])
            else:
                turn = STOP_DEG
            rows.append((piece.length, piece.radius, turn))
        if closed:
            step = max(1, len(visited) // ENTRY_SAMPLES)
            entries = visited[:-1:step][:ENTRY_SAMPLES] or [visited[0]]
            exits = list(entries)
        else:
            entries = [visited[0], visited[-1]]
            exits = [visited[-1], visited[0]]
        return _Contour(rows, closed, entries, exits)

    for start in odd + list(adj.keys()):
        while any(not used[i] for i in adj.get(start, ())):
            contours.append(walk(start))
    return contours


def travel_moves(contours: List[_Contour], origin: Tuple[float, float]) -> Tuple[List[float], bool]:
    """Rapid move lengths visiting the contours nearest-first from ``origin``. Very large drawings use a
    serpentine order of the contour entry points instead (approximate)."""
    import numpy as np

    n = len(contours)
    if n == 0:
        return [], False
    if n > MAX_NN_CONTOURS:
        pts = np.array([c.entries[0] for c in contours], dtype=np.float64)
        ys = pts[:, 1]
        band = max((ys.max() - ys.min()) / max(1.0, math.sqrt(n)), 1e-9)
        rows = np.floor((ys - ys.min()) / band)
        xs = np.where(rows % 2 == 0, pts[:, 0], -pts[:, 0])
        order = np.lexsort((xs, rows))
        path = np.vstack([np.array(origin, dtype=np.float64), pts[order]])
        return np.hypot(*(np.diff(path, axis=0).T)).tolist(), True

    cand, exits, owner, spans = [], [], [], []
    for k, c in enumerate(contours):
        spans.append((len(cand), len(cand) + len(c.entries)))
        cand.extend(c.entries)
        exits.extend(c.exits)
        owner.extend([k] * len(c.entries))
    cand_arr = np.array(cand, dtype=np.float64)
    exit_arr = np.array(exits, dtype=np.float64)
    alive = np.ones(len(cand), dtype=bool)
    pos = np.array(origin, dtype=np.float64)
    moves = []
    for _ in range(n):
        d = np.hypot(cand_arr[:, 0] - pos[0], cand_arr[:, 1] - pos[1])
        d[~alive] = np.inf
        k = int(np.argmin(d))
        moves.append(float(d[k]))
        pos = exit_arr[k]
        lo, hi = spans[owner[k]]
        alive[lo:hi] = False
    return moves, False


def _sig(x: float, digits: int = 6) -> float:
    return float(f"{x:.{digits}g}")


def _len_bin(length: float) -> int:
    return math.floor(math.log10(max(length, 1e-12)) * LEN_BINS_PER_DECADE)


def compute_cut_features(entities: Iterable, bbox: Dict[str, float]) -> Dict[str, Any]:
    """Toolpath features of the cut geometry, in drawing units (see the module docstring)."""
    width = float(bbox["max_x"]) - float(bbox["min_x"])
    height = float(bbox["max_y"]) - float(bbox["min_y"])
    diag = math.hypot(width, height) or 1.0
    q = max(diag * 1e-5, 1e-9)
    pieces = pieces_from_entities(entities, straight_radius=diag * STRAIGHT_RADIUS_FACTOR)
    pieces, duplicates = _dedupe(pieces, q)
    contours = chain_contours(pieces, q)
    moves, approximate = travel_moves(contours, (float(bbox["min_x"]), float(bbox["min_y"])))

    piece_bins: Dict[Tuple[int, int, int], List[float]] = {}
    corners = 0
    cut_length = 0.0
    for contour in contours:
        for length, radius, turn in contour.pieces:
            cut_length += length
            if turn >= CORNER_DEG and turn < STOP_DEG:
                corners += 1
            rb = -999 if radius <= 0 else math.floor(math.log10(radius) * RADIUS_BINS_PER_DECADE)
            tb = bisect.bisect_right(TURN_EDGES, turn) - 1
            acc = piece_bins.setdefault((_len_bin(length), rb, tb), [0.0, 0.0, 0.0, 0])
            acc[0] += length
            acc[1] += radius
            acc[2] += turn
            acc[3] += 1
    travel_bins: Dict[int, List[float]] = {}
    for d in moves:
        if d <= 0:
            continue
        acc = travel_bins.setdefault(_len_bin(d), [0.0, 0])
        acc[0] += d
        acc[1] += 1

    closed = sum(1 for c in contours if c.closed)
    return {
        "v": FEATURES_VERSION,
        "cut_length": _sig(cut_length),
        "contours": len(contours),
        "closed": closed,
        "open": len(contours) - closed,
        "corners": corners,
        "duplicates": duplicates,
        "travel_length": _sig(sum(moves)),
        "travel_approximate": approximate,
        "pieces": [
            [_sig(s / n), _sig(r / n), round(t / n, 2), int(n)]
            for (s, r, t, n) in (piece_bins[k] for k in sorted(piece_bins))
        ],
        "travel": [[_sig(s / n), int(n)] for (s, n) in (travel_bins[k] for k in sorted(travel_bins))],
    }


def has_features(features) -> bool:
    return isinstance(features, dict) and features.get("v") == FEATURES_VERSION and "pieces" in features


# --------------------------------------------------------------------------------------------- estimate


def _segment_time(length: float, v: float, accel: float, v_end: float) -> float:
    """Seconds for ``length`` mm at up to ``v`` mm/s, starting and ending at ``v_end`` (trapezoid profile)."""
    v = max(v, 1e-3)
    v_end = min(max(v_end, 0.0), v)
    if accel <= 0:
        return length / v
    ramp = (v * v - v_end * v_end) / accel
    if length >= ramp:
        return length / v + (v - v_end) ** 2 / (accel * v)
    peak = math.sqrt(v_end * v_end + accel * length)
    return 2.0 * (peak - v_end) / accel


def junction_speed(turn_deg: float, v: float, accel: float, deviation: float) -> float:
    """Highest speed through a direction change of ``turn_deg`` (0 = straight on, 180 = reverse)."""
    if turn_deg >= STOP_DEG - 1e-6:
        return 0.0
    if turn_deg <= 0.5:
        return v
    sin_half = math.sin(math.radians(180.0 - turn_deg) / 2.0)
    if sin_half >= 0.999999:
        return v
    r = deviation * sin_half / (1.0 - sin_half)
    return min(v, math.sqrt(max(accel * r, 0.0)))


@dataclass
class PartTime:
    cut_s: float
    pierce_s: float
    travel_s: float
    cut_length_mm: float
    pierces: int

    @property
    def total_s(self) -> float:
        return self.cut_s + self.pierce_s + self.travel_s


def estimate_part(features: Dict[str, Any], unit_mm: float, profile: Dict[str, Any], cut: MaterialCut) -> PartTime:
    """Seconds for one copy of a part: cutting, piercing and rapid moves."""
    accel = float(profile["acceleration_mm_s2"])
    deviation = float(profile["junction_deviation_mm"])
    rapid = float(profile["rapid_speed_mm_s"])
    v = cut.speed_mm_s
    cut_s = 0.0
    for length, radius, turn, count in features.get("pieces") or []:
        length_mm = float(length) * unit_mm
        v_piece = v if not radius else min(v, math.sqrt(accel * float(radius) * unit_mm))
        v_end = junction_speed(float(turn), v_piece, accel, deviation)
        cut_s += int(count) * _segment_time(length_mm, v_piece, accel, v_end)
    travel_s = sum(
        int(count) * _segment_time(float(length) * unit_mm, rapid, accel, 0.0)
        for length, count in features.get("travel") or []
    )
    pierces = int(features.get("contours") or 0)
    pierce_s = pierces * (cut.pierce_s + float(profile["pierce_settle_s"]))
    return PartTime(cut_s, pierce_s, travel_s, float(features.get("cut_length") or 0) * unit_mm, pierces)


@dataclass
class JobPart:
    key: str
    features: Dict[str, Any]
    unit_mm: float
    material: Any
    copies: int
    area_mm2: float


@dataclass
class JobEstimate:
    preset: str
    preset_label: str
    parts: Dict[str, Dict[str, Any]]
    cutting_min: float
    setup_min: float
    sheets: int
    sheet_min: float
    allowance_pct: float
    allowance_min: float
    total_min: int
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "preset": self.preset,
            "preset_label": self.preset_label,
            "cutting_min": round(self.cutting_min, 1),
            "setup_min": round(self.setup_min, 1),
            "sheets": self.sheets,
            "sheet_min": round(self.sheet_min, 1),
            "allowance_pct": round(self.allowance_pct, 1),
            "allowance_min": round(self.allowance_min, 1),
            "total_min": self.total_min,
            "warnings": list(self.warnings),
        }


def part_estimate_row(t: PartTime, cut: MaterialCut, copies: int) -> Dict[str, Any]:
    return {
        "cut_length_mm": round(t.cut_length_mm, 1),
        "pierces": t.pierces,
        "cut_speed_mm_s": round(cut.speed_mm_s, 2),
        "pierce_s": round(cut.pierce_s, 2),
        "seconds_each": round(t.total_s, 1),
        "cutting_seconds_each": round(t.cut_s, 1),
        "pierce_seconds_each": round(t.pierce_s, 1),
        "travel_seconds_each": round(t.travel_s, 1),
        "minutes_total": round(t.total_s * copies / 60.0, 1),
        "warning": cut.warning,
    }


def estimate_job(profile: Dict[str, Any], parts: List[JobPart], *, own_material: bool = False) -> JobEstimate:
    """Machine time for the whole booking: setup + sheet loading + every copy of every part, plus the allowance.

    Sheets: the parts' bounding areas per sheet material over the usable area of one sheet (the material's sheet,
    or the machine bed for own material; never more than the bed) at the nesting efficiency."""
    bed_area = float(profile["bed_width_mm"]) * float(profile["bed_height_mm"])
    efficiency = float(profile["nesting_efficiency_pct"]) / 100.0
    rows: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    cutting_s = 0.0
    area_by_sheet: Dict[Any, List[float]] = {}
    for part in parts:
        cut = material_cut_params(profile, part.material)
        t = estimate_part(part.features, part.unit_mm, profile, cut)
        rows[part.key] = part_estimate_row(t, cut, part.copies)
        cutting_s += t.total_s * part.copies
        if cut.warning and cut.warning not in warnings:
            warnings.append(cut.warning)
        material = part.material
        if own_material or material is None:
            sheet_key, sheet_area = "own", bed_area
        else:
            sheet_area = float(material.sheet_width_mm) * float(material.sheet_height_mm)
            sheet_key = getattr(material, "pk", None) or getattr(material, "code", "")
            sheet_area = min(sheet_area, bed_area) if sheet_area > 0 else bed_area
        bucket = area_by_sheet.setdefault(sheet_key, [0.0, sheet_area])
        bucket[0] += float(part.area_mm2 or 0) * part.copies
    sheets = sum(
        max(1, math.ceil(area / max(usable * efficiency, 1.0))) for area, usable in area_by_sheet.values()
    ) if parts else 0
    setup_min = float(profile["setup_min"])
    sheet_min = sheets * float(profile["sheet_load_min"])
    cutting_min = cutting_s / 60.0
    base = setup_min + sheet_min + cutting_min
    allowance_pct = float(profile["allowance_pct"])
    allowance_min = base * allowance_pct / 100.0
    total = max(1, math.ceil(base + allowance_min - 1e-9))
    return JobEstimate(
        preset=profile["preset"],
        preset_label=PRESETS[profile["preset"]]["label"],
        parts=rows,
        cutting_min=cutting_min,
        setup_min=setup_min,
        sheets=sheets,
        sheet_min=sheet_min,
        allowance_pct=allowance_pct,
        allowance_min=allowance_min,
        total_min=total,
        warnings=warnings,
    )


def unit_factor(units: str) -> float:
    from .laser_cut_service import UNIT_TO_MM

    return float(UNIT_TO_MM.get(units or "mm", Decimal("1")))
