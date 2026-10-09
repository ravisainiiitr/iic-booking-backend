"""Weight and time estimates for 3D printing, from the mesh and a per-printer profile.

Mesh analysis (done once per uploaded STL, stored in ``PrintAnalysis.bounding_box["_features"]``):
volume, surface area, the wall ("lateral") area, up / down facing projected areas grouped by slope, the area
resting on the bed, overhangs steeper than 45 degrees with the support volume under them (to the bed or to the
model surface below), and a coarse per-height profile of the cross-section area and contour length.

Estimates (re-run from the stored features whenever the material or density changes):

* FDM: walls (wall area x wall count x line width), top / bottom solid skin (sloped skin narrower than the walls
  is printed by the walls), sparse infill of the rest at the chosen density, supports under overhangs, a small
  purge / skirt allowance. Time is extruded volume over the speed actually reached on the part's segment
  lengths (acceleration), capped by the hotend's volumetric flow, layers kept above the minimum layer time
  for cooling, a per-layer overhead (layer change, travel, retraction) and a fixed warm-up.
* Resin (MSLA / SLA), MultiJet (MJP) and powder (SLS): solid parts; time is per layer (independent of the
  XY size for resin) plus an optional per-layer term for the cross-section area (MJP / SLS).

Every number is a profile parameter; ``PRESETS`` gives defaults per printer type (calibrated against
PrusaSlicer 2.9 output for FDM) and the equipment's ``print_estimate_profile`` overrides them. A fitted
calibration factor from staff-entered actual weight / time can scale the result per printer.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .print_size_limit import _ASCII_VERTEX_RE, _BINARY_DTYPE, is_binary_stl

MODEL_VERSION = 2
FEATURES_KEY = "_features"
ESTIMATE_KEY = "_estimate"

TECH_FDM = "FDM"
TECH_RESIN = "RESIN"
TECH_MJP = "MJP"
TECH_SLS = "SLS"
TECHNOLOGIES = (TECH_FDM, TECH_RESIN, TECH_MJP, TECH_SLS)
TECHNOLOGY_LABELS = {
    TECH_FDM: "FDM (filament)",
    TECH_RESIN: "Resin (MSLA / SLA)",
    TECH_MJP: "MultiJet (MJP)",
    TECH_SLS: "Powder (SLS)",
}

SUPPORT_ANGLES = (30, 35, 40, 45, 50, 55, 60, 65, 70)
DEFAULT_SUPPORT_ANGLE = 45
SUPPORT_TABLE_KEYS = ("area_all", "vol_all", "top_all", "area_plate", "vol_plate", "top_plate")
SUPPORT_AUTO = "auto"
SUPPORT_NONE = "none"
SUPPORT_BUILDPLATE = "buildplate"
SUPPORT_EVERYWHERE = "everywhere"
SUPPORT_MODES = (SUPPORT_AUTO, SUPPORT_NONE, SUPPORT_BUILDPLATE, SUPPORT_EVERYWHERE)
SUPPORT_MODE_LABELS = {
    SUPPORT_AUTO: "Auto",
    SUPPORT_NONE: "None",
    SUPPORT_BUILDPLATE: "Touching build plate only",
    SUPPORT_EVERYWHERE: "Everywhere",
}
AUTO_SUPPORT_MIN_OVERHANG_MM2 = 20.0


def nearest_angle_index(angle_deg) -> int:
    try:
        a = float(angle_deg)
    except (TypeError, ValueError):
        a = DEFAULT_SUPPORT_ANGLE
    return min(range(len(SUPPORT_ANGLES)), key=lambda i: abs(SUPPORT_ANGLES[i] - a))
PROFILE_BINS = 48
SLOPE_EDGES = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0)  # tan(slope from horizontal) bucket edges; last bucket open
SUPPORT_SAMPLE_BUDGET = 300_000
ON_BED_MM = 0.2

# --------------------------------------------------------------------------------------------- presets

_FDM_BASE: Dict[str, Any] = {
    "technology": TECH_FDM,
    "layer_height_mm": 0.1,
    "line_width_mm": 0.45,
    "wall_count": 2,
    "top_thickness_mm": 0.7,
    "bottom_thickness_mm": 0.95,
    "perimeter_speed_mm_s": 31.0,
    "infill_speed_mm_s": 32.0,
    "solid_infill_speed_mm_s": 62.0,
    "support_speed_mm_s": 60.0,
    "acceleration_mm_s2": 1500.0,
    "corner_speed_mm_s": 7.2,
    "max_flow_mm3_s": 12.0,
    "infill_segment_mm": 300.0,
    "layer_overhead_s": 0.8,
    "min_layer_time_s": 8.0,
    "warmup_min": 6.0,
    "supports": True,
    "support_density_pct": 12.0,
    "support_interface_layers": 2,
    "support_layer_overhead_s": 1.0,
    "toolchange_s": 20.0,
    "toolchange_purge_g": 0.05,
    "infill_pattern_factor": 1.0,
    "waste_g": 0.5,
    "waste_pct": 0.0,
}

PRESETS: Dict[str, Dict[str, Any]] = {
    "fdm_classic": {
        **_FDM_BASE,
        "label": "FDM - classic (Raise3D, Flashforge, Ultimaker class)",
    },
    "fdm_prusa": {
        **_FDM_BASE,
        "label": "FDM - Prusa MK4 / Core One (input shaping)",
        "perimeter_speed_mm_s": 69.0,
        "infill_speed_mm_s": 36.0,
        "solid_infill_speed_mm_s": 136.0,
        "support_speed_mm_s": 120.0,
        "acceleration_mm_s2": 3000.0,
        "corner_speed_mm_s": 7.5,
        "max_flow_mm3_s": 15.0,
        "layer_overhead_s": 1.1,
        "min_layer_time_s": 8.0,
        "warmup_min": 4.0,
    },
    "fdm_bambu": {
        **_FDM_BASE,
        "label": "FDM - Bambu Lab (P1 / X1 / A1)",
        "line_width_mm": 0.42,
        "top_thickness_mm": 0.8,
        "bottom_thickness_mm": 0.96,
        "perimeter_speed_mm_s": 103.0,
        "infill_speed_mm_s": 300.0,
        "solid_infill_speed_mm_s": 200.0,
        "support_speed_mm_s": 150.0,
        "acceleration_mm_s2": 8000.0,
        "corner_speed_mm_s": 15.6,
        "max_flow_mm3_s": 21.0,
        "layer_overhead_s": 1.1,
        "support_density_pct": 10.0,
        "infill_pattern_factor": 1.06,
        "min_layer_time_s": 4.0,
        "warmup_min": 6.0,
    },
    "fdm_bio": {
        **_FDM_BASE,
        "label": "Extrusion bioprinter",
        "layer_height_mm": 0.2,
        "line_width_mm": 0.4,
        "wall_count": 1,
        "perimeter_speed_mm_s": 10.0,
        "infill_speed_mm_s": 10.0,
        "solid_infill_speed_mm_s": 10.0,
        "support_speed_mm_s": 10.0,
        "acceleration_mm_s2": 500.0,
        "max_flow_mm3_s": 2.0,
        "layer_overhead_s": 3.0,
        "min_layer_time_s": 0.0,
        "warmup_min": 10.0,
        "supports": False,
        "waste_g": 0.2,
    },
    "resin_msla": {
        "technology": TECH_RESIN,
        "label": "Resin - MSLA (Phrozen, Elegoo, Anycubic class)",
        "layer_height_mm": 0.05,
        "per_layer_s": 8.5,
        "bottom_layers": 6,
        "bottom_layer_s": 35.0,
        "area_s_per_cm2": 0.0,
        "lift_mm": 5.0,
        "warmup_min": 5.0,
        "supports": True,
        "support_density_pct": 4.0,
        "raft_mm": 0.6,
        "support_material_density_g_cm3": None,
        "waste_g": 0.0,
        "waste_pct": 5.0,
    },
    "resin_formlabs": {
        "technology": TECH_RESIN,
        "label": "Resin - Formlabs Form 4 / 4L",
        "layer_height_mm": 0.1,
        "per_layer_s": 6.5,
        "bottom_layers": 4,
        "bottom_layer_s": 20.0,
        "area_s_per_cm2": 0.0,
        "lift_mm": 5.0,
        "warmup_min": 8.0,
        "supports": True,
        "support_density_pct": 4.0,
        "raft_mm": 0.8,
        "support_material_density_g_cm3": None,
        "waste_g": 0.0,
        "waste_pct": 5.0,
    },
    "mjp_projet": {
        "technology": TECH_MJP,
        "label": "MultiJet - 3D Systems ProJet",
        "layer_height_mm": 0.032,
        "per_layer_s": 11.0,
        "bottom_layers": 0,
        "bottom_layer_s": 0.0,
        "area_s_per_cm2": 0.02,
        "lift_mm": 0.0,
        "warmup_min": 15.0,
        "supports": True,
        "support_density_pct": 100.0,
        "raft_mm": 0.5,
        "support_material_density_g_cm3": 0.88,
        "waste_g": 0.0,
        "waste_pct": 3.0,
    },
    "sls_sinterit": {
        "technology": TECH_SLS,
        "label": "Powder - SLS (Sinterit Lisa class)",
        "layer_height_mm": 0.125,
        "per_layer_s": 14.0,
        "bottom_layers": 0,
        "bottom_layer_s": 0.0,
        "area_s_per_cm2": 0.6,
        "lift_mm": 0.0,
        "warmup_min": 60.0,
        "supports": False,
        "support_density_pct": 0.0,
        "raft_mm": 0.0,
        "support_material_density_g_cm3": None,
        "waste_g": 0.0,
        "waste_pct": 0.0,
    },
}

DEFAULT_PRESET = "fdm_classic"

# Editable parameters: key -> (label, unit, min, max). Booleans use min/max None.
PARAMETER_SPECS: Dict[str, Tuple[str, str, Optional[float], Optional[float]]] = {
    "layer_height_mm": ("Layer height", "mm", 0.01, 1.0),
    "line_width_mm": ("Line width", "mm", 0.1, 2.0),
    "wall_count": ("Walls (perimeters)", "", 1, 10),
    "top_thickness_mm": ("Top solid thickness", "mm", 0.0, 5.0),
    "bottom_thickness_mm": ("Bottom solid thickness (effective)", "mm", 0.0, 5.0),
    "perimeter_speed_mm_s": ("Wall speed (effective average)", "mm/s", 1, 1000),
    "infill_speed_mm_s": ("Sparse infill speed (effective average)", "mm/s", 1, 1000),
    "solid_infill_speed_mm_s": ("Solid infill speed (effective average)", "mm/s", 1, 1000),
    "support_speed_mm_s": ("Support speed (effective average)", "mm/s", 1, 1000),
    "acceleration_mm_s2": ("Acceleration", "mm/s²", 50, 50000),
    "corner_speed_mm_s": ("Corner speed", "mm/s", 0, 100),
    "max_flow_mm3_s": ("Max volumetric flow", "mm³/s", 0.5, 100),
    "infill_segment_mm": ("Typical infill segment length", "mm", 0.5, 300),
    "layer_overhead_s": ("Per-layer overhead", "s", 0, 60),
    "min_layer_time_s": ("Minimum layer time", "s", 0, 120),
    "per_layer_s": ("Time per layer", "s", 0.1, 600),
    "bottom_layers": ("Bottom layers", "", 0, 50),
    "bottom_layer_s": ("Bottom layer time", "s", 0, 600),
    "area_s_per_cm2": ("Extra time per cm² per layer", "s", 0, 60),
    "lift_mm": ("Part lift on supports", "mm", 0, 50),
    "warmup_min": ("Warm-up / calibration", "min", 0, 600),
    "supports": ("Estimate supports", "", None, None),
    "support_density_pct": ("Default support density", "%", 0, 100),
    "support_interface_layers": ("Support interface layers", "", 0, 10),
    "support_layer_overhead_s": ("Extra time per support layer", "s", 0, 60),
    "toolchange_s": ("Tool change per layer (separate support material)", "s", 0, 600),
    "toolchange_purge_g": ("Purge per tool change", "g", 0, 10),
    "raft_mm": ("Raft / base thickness", "mm", 0, 10),
    "support_material_density_g_cm3": ("Support material density", "g/cm³", 0.1, 5.0),
    "infill_pattern_factor": ("Infill pattern factor", "", 0.5, 2.0),
    "waste_g": ("Purge / waste per part", "g", 0, 1000),
    "waste_pct": ("Waste allowance", "%", 0, 100),
}

CALIBRATION_KEY = "calibration"
CALIBRATION_MIN_SAMPLES = 3
CALIBRATION_FACTOR_RANGE = (0.4, 2.5)


def detect_preset(make: str = "", model: str = "", name: str = "") -> str:
    """Default preset from the equipment's make / model / name text."""
    text = " ".join(str(v or "") for v in (make, model, name)).lower()
    rules = (
        (r"cellink|bio\s*x|bioprint|bio-print|regemat|allevi", "fdm_bio"),
        (r"sinterit|lisa|\bsls\b|selective laser|fuse ?1|\beos\b|formiga", "sls_sinterit"),
        (r"projet|\bmjp\b|multijet|multi-jet|polyjet|objet|stratasys j", "mjp_projet"),
        (r"formlabs|form ?[234]", "resin_formlabs"),
        (r"phrozen|sonic|elegoo|anycubic photon|photon|saturn|\bmars\b|msla|\bsla\b|\bdlp\b|resin|halot|uniformation",
         "resin_msla"),
        (r"bambu|\bp1[sp]\b|\bx1\b|\ba1\b|\bh2d\b", "fdm_bambu"),
        (r"prusa|mk4|core ?one|\bxl\b", "fdm_prusa"),
    )
    for pattern, preset in rules:
        if re.search(pattern, text):
            return preset
    return DEFAULT_PRESET


def stored_profile(equipment) -> Dict[str, Any]:
    raw = getattr(equipment, "print_estimate_profile", None) if equipment is not None else None
    return dict(raw) if isinstance(raw, dict) else {}


def resolve_profile(equipment=None, stored: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Preset defaults for the printer, with the equipment's saved overrides and calibration applied."""
    stored = dict(stored) if stored is not None else stored_profile(equipment)
    preset_key = stored.get("preset")
    if preset_key not in PRESETS:
        preset_key = detect_preset(
            getattr(equipment, "make", ""), getattr(equipment, "model_information", ""), getattr(equipment, "name", "")
        ) if equipment is not None else DEFAULT_PRESET
    profile = dict(PRESETS[preset_key])
    profile["preset"] = preset_key
    for key, value in (stored.get("overrides") or {}).items():
        if key in PARAMETER_SPECS and value is not None:
            profile[key] = value
    calibration = stored.get(CALIBRATION_KEY) or {}
    profile["time_factor"] = _clamped_factor(calibration.get("time_factor")) if calibration.get("applied") else 1.0
    profile["weight_factor"] = _clamped_factor(calibration.get("weight_factor")) if calibration.get("applied") else 1.0
    return profile


def _clamped_factor(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(f) or f <= 0:
        return 1.0
    lo, hi = CALIBRATION_FACTOR_RANGE
    return max(lo, min(hi, f))


def clean_profile_overrides(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate an overrides dict from the API. Returns (cleaned, error); blank / None values are dropped."""
    if raw in (None, ""):
        return {}, None
    if not isinstance(raw, dict):
        return None, "Send the estimate parameters as an object."
    cleaned: Dict[str, Any] = {}
    for key, value in raw.items():
        if key not in PARAMETER_SPECS:
            return None, f"Unknown estimate parameter '{key}'."
        if value in (None, ""):
            continue
        label, unit, lo, hi = PARAMETER_SPECS[key]
        if lo is None:
            cleaned[key] = str(value).strip().lower() not in ("false", "0", "no", "off", "none")
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{label} must be a number."
        if not math.isfinite(number) or number < lo or number > hi:
            return None, f"{label} must be between {lo:g} and {hi:g}{(' ' + unit) if unit else ''}."
        cleaned[key] = int(round(number)) if key in ("wall_count", "bottom_layers") else number
    return cleaned, None


# --------------------------------------------------------------------------------------------- mesh

def stl_triangles(data: bytes) -> np.ndarray:
    """Triangles of a binary or ASCII STL as an (n, 3, 3) float64 array (finite triangles only)."""
    if is_binary_stl(data):
        if len(data) < 84:
            raise ValueError("File is too small to be a valid binary STL.")
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 > len(data):
            raise ValueError("Binary STL header reports more triangles than file contains.")
        tris = np.frombuffer(data, dtype=_BINARY_DTYPE, count=count, offset=84)["v"].astype(np.float64)
    else:
        values = _ASCII_VERTEX_RE.findall(data)
        n = len(values) // 3
        if n == 0:
            raise ValueError("No triangles found in ASCII STL.")
        tris = np.asarray(values[: n * 3], dtype=np.float64).reshape(n, 3, 3)
    if len(tris):
        tris = tris[np.isfinite(tris).all(axis=(1, 2))]
    if not len(tris):
        raise ValueError("STL contains no triangles.")
    return tris


def _ramp_below(edges: np.ndarray, lo: np.ndarray, hi: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """For each height in ``edges``: sum of weight x (share of the face's height range lying below it)."""
    out = np.zeros(len(edges), dtype=np.float64)
    if not len(lo):
        return out
    span = hi - lo
    flat = span <= 1e-9
    if flat.any():
        c = np.sort(lo[flat])
        w = weight[flat][np.argsort(lo[flat])]
        cw = np.concatenate(([0.0], np.cumsum(w)))
        out += cw[np.searchsorted(c, edges, side="left")]
    s = ~flat
    if s.any():
        d = weight[s] / span[s]
        lo_s, hi_s = lo[s], hi[s]
        o_lo = np.argsort(lo_s)
        o_hi = np.argsort(hi_s)
        lo_sorted, hi_sorted = lo_s[o_lo], hi_s[o_hi]
        cd_lo = np.concatenate(([0.0], np.cumsum(d[o_lo])))
        cdz_lo = np.concatenate(([0.0], np.cumsum(d[o_lo] * lo_sorted)))
        cd_hi = np.concatenate(([0.0], np.cumsum(d[o_hi])))
        cdz_hi = np.concatenate(([0.0], np.cumsum(d[o_hi] * hi_sorted)))
        i = np.searchsorted(lo_sorted, edges, side="left")
        j = np.searchsorted(hi_sorted, edges, side="left")
        out += edges * cd_lo[i] - cdz_lo[i] - (edges * cd_hi[j] - cdz_hi[j])
    return out


def _support_table(tris, nz, proj, zmin, bed_mask, up_mask) -> Dict[str, List[float]]:
    """Overhang area, support volume and highest support point per overhang angle, for supports everywhere
    (down to the model surface below, or the bed) and touching the build plate only (clear path to the bed).

    Overhang angle is measured from vertical: a face needs support when it leans further than the angle, i.e.
    its downward normal satisfies -nz > sin(angle). Volumes come from a sampled height map.
    """
    n = len(SUPPORT_ANGLES)
    empty = {k: [0.0] * n for k in SUPPORT_TABLE_KEYS}
    min_sin = math.sin(math.radians(SUPPORT_ANGLES[0]))
    over = (-nz > min_sin) & ~bed_mask & (proj > 1e-9)
    if not over.any():
        return empty
    xy = tris[:, :, :2]
    xmin, ymin = float(xy[..., 0].min()), float(xy[..., 1].min())
    xmax, ymax = float(xy[..., 0].max()), float(xy[..., 1].max())
    up = up_mask & (proj > 1e-9)
    total_proj = float(proj[over].sum() + proj[up].sum())
    res = max(0.5, math.sqrt(total_proj / (SUPPORT_SAMPLE_BUDGET * 0.6)))
    n_faces = int(over.sum() + up.sum())
    if n_faces > SUPPORT_SAMPLE_BUDGET:
        res = max(res, math.sqrt((xmax - xmin + 1) * (ymax - ymin + 1) / 20000.0))
    rng = np.random.default_rng(20261009)

    def sample(mask):
        t = tris[mask]
        p = proj[mask]
        k = np.clip(np.ceil(p / (res * res)).astype(np.int64), 1, 4096)
        idx = np.repeat(np.arange(len(t)), k)
        u = rng.random(len(idx))
        v = rng.random(len(idx))
        flip = u + v > 1.0
        u[flip], v[flip] = 1.0 - u[flip], 1.0 - v[flip]
        single = (k == 1)[idx]
        u[single] = v[single] = 1.0 / 3.0
        tt = t[idx]
        pts = tt[:, 0] + u[:, None] * (tt[:, 1] - tt[:, 0]) + v[:, None] * (tt[:, 2] - tt[:, 0])
        return pts, (p / k)[idx], idx

    over_pts, over_w, over_idx = sample(over)
    lean = -nz[over][over_idx]
    up_pts, _w, _i = sample(up)
    ny = int(math.floor((ymax - ymin) / res)) + 1

    def cell(pts):
        ix = np.floor((pts[:, 0] - xmin) / res).astype(np.int64)
        iy = np.floor((pts[:, 1] - ymin) / res).astype(np.int64)
        return ix * ny + iy

    floor = np.full(len(over_pts), zmin)
    blocked = np.zeros(len(over_pts), dtype=bool)
    if len(up_pts):
        cells = cell(up_pts)
        order = np.lexsort((up_pts[:, 2], cells))
        c_sorted, zs = cells[order], up_pts[order, 2]
        oc = cell(over_pts)
        start = np.searchsorted(c_sorted, oc, side="left")
        end = np.searchsorted(c_sorted, oc, side="right")
        has = end > start
        if has.any():
            # The highest up-facing surface below each overhang point, in the same cell: one sorted key of
            # (cell, height) so a single searchsorted finds it.
            span = float(zs.max() - zs.min()) + 1.0
            key = c_sorted.astype(np.float64) * span * 4 + (zs - zs.min())
            q = oc.astype(np.float64) * span * 4 + (over_pts[:, 2] + 1e-3 - zs.min())
            j = np.searchsorted(key, q, side="right") - 1
            ok = has & (j >= start) & (j < end)
            floor[ok] = np.maximum(zmin, zs[j[ok]])
            blocked = ok & (floor > zmin + ON_BED_MM)
    above_bed = np.clip(over_pts[:, 2] - zmin, 0.0, None)
    column = np.clip(over_pts[:, 2] - floor, 0.0, None)
    out = {k: [] for k in SUPPORT_TABLE_KEYS}
    for angle in SUPPORT_ANGLES:
        m = lean > math.sin(math.radians(angle))
        p = m & ~blocked
        out["area_all"].append(float(over_w[m].sum()))
        out["vol_all"].append(float((column[m] * over_w[m]).sum()))
        out["top_all"].append(float(above_bed[m].max()) if m.any() else 0.0)
        out["area_plate"].append(float(over_w[p].sum()))
        out["vol_plate"].append(float((above_bed[p] * over_w[p]).sum()))
        out["top_plate"].append(float(above_bed[p].max()) if p.any() else 0.0)
    return out


@dataclass
class MeshFeatures:
    volume_mm3: float
    area_mm2: float
    size: Tuple[float, float, float]
    triangle_count: int
    lateral_area_mm2: float
    up_area_by_slope: List[float]
    down_area_by_slope: List[float]
    bed_area_mm2: float
    support: Dict[str, List[float]]
    profile_dz: float
    cross_section_mm2: List[float]
    contour_mm: List[float]

    @property
    def height(self) -> float:
        return float(self.size[2])

    def support_at(self, mode: str, angle_deg: float) -> Tuple[float, float, float]:
        """(overhang area mm², raw support column volume mm³, highest support point mm) for a support mode
        (``everywhere`` / ``buildplate``) at the nearest tabulated overhang angle."""
        suffix = "plate" if mode == SUPPORT_BUILDPLATE else "all"
        i = nearest_angle_index(angle_deg)
        table = self.support or {}
        try:
            return (
                float(table[f"area_{suffix}"][i]),
                float(table[f"vol_{suffix}"][i]),
                float(table[f"top_{suffix}"][i]),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0, 0.0, 0.0

    @property
    def overhang_area_mm2(self) -> float:
        return self.support_at(SUPPORT_EVERYWHERE, DEFAULT_SUPPORT_ANGLE)[0]

    def to_dict(self) -> Dict[str, Any]:
        r = lambda v: round(float(v), 3)  # noqa: E731
        return {
            "v": MODEL_VERSION,
            "volume_mm3": r(self.volume_mm3),
            "area_mm2": r(self.area_mm2),
            "size": [r(s) for s in self.size],
            "triangles": int(self.triangle_count),
            "lateral_mm2": r(self.lateral_area_mm2),
            "up_by_slope": [r(v) for v in self.up_area_by_slope],
            "down_by_slope": [r(v) for v in self.down_area_by_slope],
            "bed_mm2": r(self.bed_area_mm2),
            "support": {k: [r(v) for v in vals] for k, vals in (self.support or {}).items()},
            "dz": round(float(self.profile_dz), 5),
            "section_mm2": [r(v) for v in self.cross_section_mm2],
            "contour_mm": [r(v) for v in self.contour_mm],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MeshFeatures":
        return cls(
            volume_mm3=float(d["volume_mm3"]),
            area_mm2=float(d["area_mm2"]),
            size=tuple(float(s) for s in d["size"]),  # type: ignore[arg-type]
            triangle_count=int(d.get("triangles", 0)),
            lateral_area_mm2=float(d["lateral_mm2"]),
            up_area_by_slope=[float(v) for v in d["up_by_slope"]],
            down_area_by_slope=[float(v) for v in d["down_by_slope"]],
            bed_area_mm2=float(d["bed_mm2"]),
            support={k: [float(v) for v in vals] for k, vals in (d.get("support") or {}).items()},
            profile_dz=float(d["dz"]),
            cross_section_mm2=[float(v) for v in d["section_mm2"]],
            contour_mm=[float(v) for v in d["contour_mm"]],
        )


def compute_features(tris: np.ndarray) -> MeshFeatures:
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    cr = np.cross(b - a, c - a)
    dbl = np.sqrt(np.einsum("ij,ij->i", cr, cr))
    area = 0.5 * dbl
    signed = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)
    volume = abs(signed)
    orient = -1.0 if signed < 0 else 1.0
    safe = np.where(dbl > 0, dbl, 1.0)
    nz = np.where(dbl > 0, orient * cr[:, 2] / safe, 0.0)
    proj = 0.5 * np.abs(cr[:, 2])
    signed_proj = np.sign(nz) * proj
    sin = np.sqrt(np.clip(1.0 - nz * nz, 0.0, 1.0))
    lateral = area * sin
    z = tris[:, :, 2]
    zlo, zhi = z.min(axis=1), z.max(axis=1)
    zmin, zmax = float(zlo.min()), float(zhi.max())
    mins = tris.reshape(-1, 3).min(axis=0)
    maxs = tris.reshape(-1, 3).max(axis=0)
    size = tuple(float(v) for v in (maxs - mins))

    up_mask = nz > 1e-6
    down_mask = nz < -1e-6
    tan = np.where(np.abs(nz) > 1e-9, sin / np.maximum(np.abs(nz), 1e-9), np.inf)
    bucket = np.searchsorted(np.asarray(SLOPE_EDGES[1:]), tan, side="right")
    n_buckets = len(SLOPE_EDGES)
    up_by = np.bincount(bucket[up_mask], weights=proj[up_mask], minlength=n_buckets)[:n_buckets]
    down_by = np.bincount(bucket[down_mask], weights=proj[down_mask], minlength=n_buckets)[:n_buckets]
    bed_mask = down_mask & (zhi <= zmin + ON_BED_MM)
    bed_area = float(proj[bed_mask].sum())

    support = _support_table(tris, nz, proj, zmin, bed_mask, up_mask)

    height = max(zmax - zmin, 1e-6)
    bins = PROFILE_BINS if height > 1.0 else 1
    dz = height / bins
    edges = zmin + dz * np.arange(bins + 1)
    centers = zmin + dz * (np.arange(bins) + 0.5)
    below_signed = _ramp_below(centers, zlo, zhi, signed_proj)
    section = np.clip(float(signed_proj.sum()) - below_signed, 0.0, None)
    lat_below = _ramp_below(edges, zlo, zhi, lateral)
    contour = np.clip(np.diff(lat_below) / dz, 0.0, None)
    # Keep the profile consistent with the exact volume.
    approx_v = float(section.sum() * dz)
    if approx_v > 0 and volume > 0:
        section = section * (volume / approx_v)

    return MeshFeatures(
        volume_mm3=volume,
        area_mm2=float(area.sum()),
        size=size,  # type: ignore[arg-type]
        triangle_count=int(len(tris)),
        lateral_area_mm2=float(lateral.sum()),
        up_area_by_slope=[float(v) for v in up_by],
        down_area_by_slope=[float(v) for v in down_by],
        bed_area_mm2=bed_area,
        support=support,
        profile_dz=float(dz),
        cross_section_mm2=[float(v) for v in section],
        contour_mm=[float(v) for v in contour],
    )


def approximate_features(volume_mm3: float, area_mm2: float, size: Iterable[float]) -> MeshFeatures:
    """Box-like stand-in for analyses stored before mesh features were kept (no STL re-read)."""
    x, y, z = (max(float(v or 0), 0.0) for v in size)
    volume = max(float(volume_mm3 or 0), 0.0)
    z = z if z > 0 else max(volume ** (1 / 3), 1.0)
    section = volume / z if z > 0 else 0.0
    flat = min(section, x * y) if x > 0 and y > 0 else section
    area = float(area_mm2 or 0) or (2 * flat + 4 * math.sqrt(max(flat, 0)) * z)
    lateral = max(area - 2 * flat, 0.0)
    up = [flat] + [0.0] * (len(SLOPE_EDGES) - 1)
    return MeshFeatures(
        volume_mm3=volume,
        area_mm2=area,
        size=(x, y, z),
        triangle_count=0,
        lateral_area_mm2=lateral,
        up_area_by_slope=up,
        down_area_by_slope=list(up),
        bed_area_mm2=flat,
        support={k: [0.0] * len(SUPPORT_ANGLES) for k in SUPPORT_TABLE_KEYS},
        profile_dz=z,
        cross_section_mm2=[section],
        contour_mm=[lateral / z if z > 0 else 0.0],
    )


# --------------------------------------------------------------------------------------------- estimate

@dataclass
class SupportOptions:
    """The user's support choice. ``density_pct`` None uses the printer profile; ``material_density_g_cm3`` None
    means supports are printed in the model material."""

    mode: str = SUPPORT_AUTO
    density_pct: Optional[float] = None
    angle_deg: float = DEFAULT_SUPPORT_ANGLE
    material_code: str = ""
    material_density_g_cm3: Optional[float] = None

    @property
    def separate_material(self) -> bool:
        return bool(self.material_code) and self.material_density_g_cm3 is not None

    @classmethod
    def from_settings(cls, settings: Optional[Dict[str, Any]], material_density: Optional[float] = None) -> "SupportOptions":
        settings = settings or {}
        mode = str(settings.get("support_mode") or SUPPORT_AUTO).strip().lower()
        density = settings.get("support_density_pct")
        try:
            density = float(density) if density not in (None, "") else None
        except (TypeError, ValueError):
            density = None
        try:
            angle = float(settings.get("support_angle_deg") or DEFAULT_SUPPORT_ANGLE)
        except (TypeError, ValueError):
            angle = DEFAULT_SUPPORT_ANGLE
        code = str(settings.get("support_material_code") or "").strip()
        return cls(
            mode=mode if mode in SUPPORT_MODES else SUPPORT_AUTO,
            density_pct=density,
            angle_deg=SUPPORT_ANGLES[nearest_angle_index(angle)],
            material_code=code,
            material_density_g_cm3=material_density if code else None,
        )


@dataclass
class EstimateBreakdown:
    technology: str
    preset: str
    model_g: float
    support_g: float
    waste_g: float
    total_g: float
    print_min: float
    warmup_min: float
    total_min: float
    layers: int
    layer_height_mm: float
    infill_percent: Optional[float]
    notes: List[str] = field(default_factory=list)
    detail: Dict[str, float] = field(default_factory=dict)
    support_mode: str = SUPPORT_NONE
    support_mode_requested: str = SUPPORT_AUTO
    support_angle_deg: float = DEFAULT_SUPPORT_ANGLE
    support_density_pct: float = 0.0
    support_material_code: str = ""
    support_min: float = 0.0
    overhang_area_mm2: float = 0.0
    overhang_plate_mm2: float = 0.0

    @property
    def support_separate(self) -> bool:
        return bool(self.support_material_code)

    @property
    def model_material_g(self) -> float:
        """Grams charged at the model material's rate (supports too when printed in the same material)."""
        return self.total_g - (self.support_g if self.support_separate else 0.0)

    @property
    def support_material_g(self) -> float:
        """Grams charged at the separate support material's rate (0 when supports use the model material)."""
        return self.support_g if self.support_separate else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_version": MODEL_VERSION,
            "technology": self.technology,
            "preset": self.preset,
            "model_g": round(self.model_g, 2),
            "support_g": round(self.support_g, 2),
            "waste_g": round(self.waste_g, 2),
            "total_g": round(self.total_g, 2),
            "model_material_g": round(self.model_material_g, 2),
            "support_material_g": round(self.support_material_g, 2),
            "print_min": round(self.print_min, 1),
            "support_min": round(self.support_min, 1),
            "warmup_min": round(self.warmup_min, 1),
            "total_min": round(self.total_min, 1),
            "layers": int(self.layers),
            "layer_height_mm": self.layer_height_mm,
            "infill_percent": self.infill_percent,
            "support_mode": self.support_mode,
            "support_mode_requested": self.support_mode_requested,
            "support_mode_label": SUPPORT_MODE_LABELS.get(self.support_mode, self.support_mode),
            "support_angle_deg": self.support_angle_deg,
            "support_density_pct": round(self.support_density_pct, 1),
            "support_material_code": self.support_material_code,
            "overhang_area_mm2": round(self.overhang_area_mm2, 1),
            "overhang_plate_mm2": round(self.overhang_plate_mm2, 1),
            "notes": list(self.notes),
            "detail": {k: round(float(v), 3) for k, v in self.detail.items()},
        }


def _segment_time(length: float, v: float, accel: float, v_corner: float) -> float:
    """Seconds to travel one segment of ``length`` mm at up to ``v`` mm/s, starting / ending at the corner speed."""
    v = max(v, 0.1)
    v_corner = min(max(v_corner, 0.0), v)
    if accel <= 0:
        return length / v
    ramp = (v * v - v_corner * v_corner) / accel
    if length >= ramp:
        return length / v + (v - v_corner) ** 2 / (accel * v)
    peak = math.sqrt(v_corner * v_corner + accel * length)
    return 2.0 * (peak - v_corner) / accel


def _effective_speed(length: float, v: float, accel: float, v_corner: float) -> float:
    length = max(length, 0.2)
    return length / _segment_time(length, v, accel, v_corner)


def _skin_fraction(t_wall: float, t_skin: float) -> List[float]:
    """Share of each slope bucket's projected area printed as solid skin rather than by the walls."""
    out = []
    for i, lo in enumerate(SLOPE_EDGES):
        hi = SLOPE_EDGES[i + 1] if i + 1 < len(SLOPE_EDGES) else lo * 2
        tan_mid = 0.5 * (lo + hi)
        out.append(max(0.0, 1.0 - t_wall * tan_mid / t_skin) if t_skin > 0 else 0.0)
    return out


def _num(profile, key, default=0.0) -> float:
    try:
        v = float(profile.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) else float(default)


def resolve_support_mode(f: MeshFeatures, profile: Dict[str, Any], opts: SupportOptions) -> str:
    """The support mode actually estimated: printers without supports (powder, bioprinters) use none; MultiJet
    always fills every overhang with wax; Auto is 'touching build plate' on FDM (everywhere on resin) when the
    model has overhangs."""
    tech = profile.get("technology", TECH_FDM)
    if tech == TECH_SLS or not bool(profile.get("supports", True)):
        return SUPPORT_NONE
    if tech == TECH_MJP:
        return SUPPORT_EVERYWHERE
    if opts.mode != SUPPORT_AUTO:
        return opts.mode
    area = f.support_at(SUPPORT_EVERYWHERE, opts.angle_deg)[0]
    if tech == TECH_FDM:
        return SUPPORT_BUILDPLATE if area >= AUTO_SUPPORT_MIN_OVERHANG_MM2 else SUPPORT_NONE
    return SUPPORT_EVERYWHERE


def _support_common(f, profile, opts, mode):
    area_all = f.support_at(SUPPORT_EVERYWHERE, opts.angle_deg)[0]
    area_plate = f.support_at(SUPPORT_BUILDPLATE, opts.angle_deg)[0]
    density_pct = opts.density_pct if opts.density_pct is not None else _num(profile, "support_density_pct", 15.0)
    density_pct = max(0.0, min(100.0, density_pct))
    if mode == SUPPORT_NONE:
        return (0.0, 0.0, 0.0), density_pct, area_all, area_plate
    return f.support_at(mode, opts.angle_deg), density_pct, area_all, area_plate


def estimate_fdm(
    f: MeshFeatures,
    profile: Dict[str, Any],
    infill_percent: float,
    density: float,
    supports: Optional[SupportOptions] = None,
) -> EstimateBreakdown:
    opts = supports or SupportOptions()
    lh = max(_num(profile, "layer_height_mm", 0.1), 0.01)
    lw = max(_num(profile, "line_width_mm", 0.45), 0.1)
    walls = max(int(_num(profile, "wall_count", 2)), 1)
    t_wall = walls * lw
    t_top = max(_num(profile, "top_thickness_mm", 0.7), 0.0)
    t_bot = max(_num(profile, "bottom_thickness_mm", 0.5), 0.0)
    infill = max(0.0, min(100.0, float(infill_percent))) / 100.0
    pattern = _num(profile, "infill_pattern_factor", 1.0)
    V = max(f.volume_mm3, 0.0)

    shell = min(V, f.lateral_area_mm2 * t_wall)
    top_frac = _skin_fraction(t_wall, t_top)
    bot_frac = _skin_fraction(t_wall, t_bot)
    skin_raw = sum(a * k for a, k in zip(f.up_area_by_slope, top_frac)) * t_top
    skin_raw += sum(a * k for a, k in zip(f.down_area_by_slope, bot_frac)) * t_bot
    skin = min(max(V - shell, 0.0), skin_raw)
    inner = max(V - shell - skin, 0.0)
    sparse = inner * min(1.0, infill * pattern) if infill < 1.0 else inner

    mode = resolve_support_mode(f, profile, opts)
    (s_area, s_raw, s_top), s_density, area_all, area_plate = _support_common(f, profile, opts, mode)
    support = 0.0
    support_layers = 0
    if s_area > 0:
        interface_layers = max(_num(profile, "support_interface_layers", 2.0), 0.0)
        support = s_raw * s_density / 100.0 + s_area * interface_layers * lh * 0.7
        support_layers = int(math.ceil(s_top / lh))
    separate = opts.separate_material and support > 0

    model_g = (shell + skin + sparse) / 1000.0 * density
    support_g = support / 1000.0 * (opts.material_density_g_cm3 if separate else density)
    purge_g = support_layers * _num(profile, "toolchange_purge_g", 0.0) if separate else 0.0
    waste_g = _num(profile, "waste_g", 0.0) + purge_g + (model_g + support_g) * _num(profile, "waste_pct", 0.0) / 100.0

    # --- time
    accel = _num(profile, "acceleration_mm_s2", 1500.0)
    corner = _num(profile, "corner_speed_mm_s", 10.0)
    qmax = max(_num(profile, "max_flow_mm3_s", 12.0), 0.1)
    xsec = lw * lh
    v_cap = qmax / xsec

    height = max(f.height, lh)
    layers = max(1, int(math.ceil(height / lh - 1e-9)))
    sections = np.asarray(f.cross_section_mm2 or [V / height], dtype=np.float64)
    contours = np.asarray(f.contour_mm or [f.lateral_area_mm2 / height], dtype=np.float64)
    nb = len(sections)
    layers_per_bin = layers / nb

    wall_area = np.minimum(sections, contours * t_wall)
    inner_area = np.clip(sections - wall_area, 0.0, None)
    seg_wall = np.clip(contours / 4.0, 1.0, 300.0)
    seg_fill = np.clip(np.sqrt(inner_area), 1.0, 300.0)
    seg_sparse = np.minimum(seg_fill, max(_num(profile, "infill_segment_mm", 300.0), 0.5))

    def speeds(v_nominal, segs):
        v = min(v_nominal, v_cap)
        return np.array([_effective_speed(L, v, accel, corner) for L in segs])

    v_wall = speeds(_num(profile, "perimeter_speed_mm_s", 45.0), seg_wall)
    v_solid = speeds(_num(profile, "solid_infill_speed_mm_s", 60.0), seg_fill)
    v_sparse = v_solid if infill >= 0.99 else speeds(_num(profile, "infill_speed_mm_s", 80.0), seg_sparse)
    v_supp = speeds(_num(profile, "support_speed_mm_s", 60.0), np.full(nb, 20.0))

    wall_w = wall_area / max(wall_area.sum(), 1e-9)
    inner_w = inner_area / max(inner_area.sum(), 1e-9) if inner_area.sum() > 0 else np.full(nb, 1.0 / nb)
    centers = (np.arange(nb) + 0.5) * (height / nb)
    support_bins = centers <= max(s_top, height / nb)
    support_w = support_bins / max(support_bins.sum(), 1)
    model_bins = (
        shell * wall_w / (xsec * v_wall)
        + skin * inner_w / (xsec * v_solid)
        + sparse * inner_w / (xsec * v_sparse)
    )
    support_bins_s = support * support_w / (xsec * v_supp)
    min_layer = _num(profile, "min_layer_time_s", 0.0)
    per_layer = (model_bins + support_bins_s) / layers_per_bin
    slowed = np.maximum(per_layer, min_layer) if min_layer > 0 else per_layer
    extrude_s = float((slowed * layers_per_bin).sum())
    overhead_s = layers * _num(profile, "layer_overhead_s", 2.0)
    support_overhead_s = support_layers * _num(profile, "support_layer_overhead_s", 0.0)
    if separate:
        support_overhead_s += support_layers * _num(profile, "toolchange_s", 0.0)
    support_s = float(support_bins_s.sum()) + support_overhead_s
    print_s = extrude_s + overhead_s + support_overhead_s

    b = _finish(
        TECH_FDM, profile, model_g, support_g, waste_g, print_s, layers, lh, infill_percent, [],
        {"shell_mm3": shell, "skin_mm3": skin, "infill_mm3": sparse, "support_mm3": support,
         "support_layers": support_layers, "extrude_min": extrude_s / 60.0, "layer_overhead_min": overhead_s / 60.0},
    )
    return _with_support(b, profile, opts, mode, s_density, separate, support_s, area_all, area_plate)


def estimate_layered(
    f: MeshFeatures, profile: Dict[str, Any], density: float, supports: Optional[SupportOptions] = None
) -> EstimateBreakdown:
    opts = supports or SupportOptions()
    tech = profile.get("technology", TECH_RESIN)
    lh = max(_num(profile, "layer_height_mm", 0.05), 0.005)
    V = max(f.volume_mm3, 0.0)
    mode = resolve_support_mode(f, profile, opts)
    (s_area, s_raw, _s_top), s_density, area_all, area_plate = _support_common(f, profile, opts, mode)
    lift = _num(profile, "lift_mm", 0.0) if mode != SUPPORT_NONE else 0.0
    support = 0.0
    if mode != SUPPORT_NONE:
        support = (s_raw + f.bed_area_mm2 * lift) * s_density / 100.0
        x, y = f.size[0], f.size[1]
        support += (x + 2.0) * (y + 2.0) * _num(profile, "raft_mm", 0.0) * 0.6
    separate = opts.separate_material and support > 0
    if separate:
        support_density = opts.material_density_g_cm3
    else:
        try:
            raw = profile.get("support_material_density_g_cm3")
            support_density = float(raw) if raw not in (None, "") else density
        except (TypeError, ValueError):
            support_density = density
    model_g = V / 1000.0 * density
    support_g = support / 1000.0 * support_density
    waste_g = _num(profile, "waste_g", 0.0) + (model_g + support_g) * _num(profile, "waste_pct", 0.0) / 100.0

    height = max(f.height + lift, lh)
    layers = max(1, int(math.ceil(height / lh - 1e-9)))
    bottom = min(int(_num(profile, "bottom_layers", 0)), layers)
    per_layer = _num(profile, "per_layer_s", 8.0)
    print_s = bottom * _num(profile, "bottom_layer_s", per_layer) + (layers - bottom) * per_layer
    support_s = int(math.ceil(lift / lh)) * per_layer if lift > 0 else 0.0
    area_rate = _num(profile, "area_s_per_cm2", 0.0)
    if area_rate > 0:
        model_layers = max(1, int(math.ceil(f.height / lh)))
        avg_section_cm2 = (V / max(f.height, lh)) / 100.0
        print_s += model_layers * avg_section_cm2 * area_rate
    notes = ["Printed solid; the density setting does not apply to this printer type."]
    b = _finish(tech, profile, model_g, support_g, waste_g, print_s, layers, lh, None, notes, {"support_mm3": support})
    return _with_support(b, profile, opts, mode, s_density, separate, support_s, area_all, area_plate)


def _with_support(b, profile, opts, mode, density_pct, separate, support_s, area_all, area_plate):
    b.support_mode = mode
    b.support_mode_requested = opts.mode
    b.support_angle_deg = opts.angle_deg
    b.support_density_pct = density_pct if mode != SUPPORT_NONE else 0.0
    b.support_material_code = opts.material_code if separate else ""
    b.support_min = support_s / 60.0 * float(profile.get("time_factor", 1.0) or 1.0)
    b.overhang_area_mm2 = area_all
    b.overhang_plate_mm2 = area_plate
    return b


def _finish(tech, profile, model_g, support_g, waste_g, print_s, layers, lh, infill, notes, detail):
    wf = float(profile.get("weight_factor", 1.0) or 1.0)
    tf = float(profile.get("time_factor", 1.0) or 1.0)
    model_g, support_g = model_g * wf, support_g * wf
    print_min = print_s / 60.0 * tf
    warmup = max(_num(profile, "warmup_min", 0.0), 0.0)
    return EstimateBreakdown(
        technology=tech,
        preset=str(profile.get("preset", "")),
        model_g=model_g,
        support_g=support_g,
        waste_g=waste_g,
        total_g=model_g + support_g + waste_g,
        print_min=print_min,
        warmup_min=warmup,
        total_min=print_min + warmup,
        layers=layers,
        layer_height_mm=round(lh, 4),
        infill_percent=infill,
        notes=notes,
        detail=detail,
    )


def estimate(
    f: MeshFeatures,
    profile: Dict[str, Any],
    *,
    infill_percent: float,
    density_g_cm3: float,
    supports: Optional[SupportOptions] = None,
) -> EstimateBreakdown:
    if profile.get("technology", TECH_FDM) == TECH_FDM:
        return estimate_fdm(f, profile, infill_percent, density_g_cm3, supports)
    return estimate_layered(f, profile, density_g_cm3, supports)


# --------------------------------------------------------------------------------------------- calibration

def fit_calibration(samples: List[Dict[str, float]]) -> Dict[str, Any]:
    """Robust per-printer factors from parts with staff-entered actuals.

    Each sample: {"est_g", "act_g", "est_min", "act_min"} per copy, estimates *without* any calibration.
    The factor is the median actual / estimate ratio; errors before / after are median absolute % errors.
    """
    def fit(pairs):
        pairs = [(e, a) for e, a in pairs if e and a and e > 0 and a > 0]
        if len(pairs) < CALIBRATION_MIN_SAMPLES:
            return None, len(pairs), None, None
        ratios = sorted(a / e for e, a in pairs)
        factor = _clamped_factor(float(np.median(ratios)))
        before = float(np.median([abs(e - a) / a * 100.0 for e, a in pairs]))
        after = float(np.median([abs(e * factor - a) / a * 100.0 for e, a in pairs]))
        return round(factor, 3), len(pairs), round(before, 1), round(after, 1)

    wf, wn, wb, wa = fit([(s.get("est_g"), s.get("act_g")) for s in samples])
    tf, tn, tb, ta = fit([(s.get("est_min"), s.get("act_min")) for s in samples])
    return {
        "weight_factor": wf,
        "weight_samples": wn,
        "weight_error_before_pct": wb,
        "weight_error_after_pct": wa,
        "time_factor": tf,
        "time_samples": tn,
        "time_error_before_pct": tb,
        "time_error_after_pct": ta,
        "min_samples": CALIBRATION_MIN_SAMPLES,
    }
