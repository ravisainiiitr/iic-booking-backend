"""User-chosen print orientation for an analysed STL, and the search for the orientation needing least support.

An orientation is a 3 x 3 rotation matrix (row-major, 9 numbers) applied to the STL's own coordinates (Z up, as
slicers read it): ``p' = R p``. The model then rests on the build plate. It is stored in the analysis's
``slicer_settings["orientation"]`` (absent = as uploaded) and the mesh features saved in ``bounding_box`` belong
to it (``bounding_box["_orientation"]`` holds its key).
"""

from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

ORIENTATION_KEY = "orientation"
ORIENTATION_STORED_KEY = "_orientation"
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
MAX_FLAT_FACE_CANDIDATES = 4
# Two candidates whose "down" directions are this close (cosine) are the same orientation for supports.
SAME_DOWN_COS = math.cos(math.radians(3.0))
# Support grams within this of the least are a tie; the faster print then wins.
SUPPORT_TIE_G = 0.5
SUPPORT_TIE_PCT = 5.0

_CACHE_SIZE = 64
_cache: "OrderedDict[Tuple[str, str, str], Tuple[Dict[str, Any], List[float]]]" = OrderedDict()
_cache_lock = threading.Lock()

_DOWN_LABELS = {
    (0, 0, -1): "As uploaded",
    (0, 0, 1): "Upside down",
    (1, 0, 0): "On its right side",
    (-1, 0, 0): "On its left side",
    (0, 1, 0): "On its back",
    (0, -1, 0): "On its front",
}


def parse_orientation(raw) -> Tuple[Optional[Tuple[float, ...]], Optional[str]]:
    """(matrix or None for 'as uploaded', error). Accepts 9 numbers (list, nested 3 x 3 or comma string)."""
    if raw in (None, "", [], "identity", "reset"):
        return None, None
    values: List[Any]
    if isinstance(raw, str):
        values = [v for v in raw.replace(";", ",").split(",") if v.strip()]
    elif isinstance(raw, (list, tuple)):
        values = [v for row in raw for v in (row if isinstance(row, (list, tuple)) else [row])]
    else:
        return None, "Orientation must be a 3 x 3 rotation matrix (9 numbers)."
    try:
        m = np.asarray([float(v) for v in values], dtype=np.float64)
    except (TypeError, ValueError):
        return None, "Orientation must be a 3 x 3 rotation matrix (9 numbers)."
    if m.shape != (9,) or not np.isfinite(m).all():
        return None, "Orientation must be a 3 x 3 rotation matrix (9 numbers)."
    r = m.reshape(3, 3)
    if not np.allclose(r @ r.T, np.eye(3), atol=2e-3) or np.linalg.det(r) <= 0:
        return None, "Orientation must be a rotation (no scaling or mirroring)."
    clean = tuple(_clean(v) for v in m)
    if np.allclose(clean, IDENTITY, atol=1e-6):
        return None, None
    return clean, None


def _clean(v: float) -> float:
    v = round(float(v), 6)
    return 0.0 if v == 0 else v


def orientation_key(m: Optional[Sequence[float]]) -> str:
    if not m:
        return ""
    return ",".join(f"{v:.4f}" for v in m)


def stored_orientation(analysis) -> Optional[Tuple[float, ...]]:
    raw = (getattr(analysis, "slicer_settings", None) or {}).get(ORIENTATION_KEY)
    m, err = parse_orientation(raw)
    return None if err else m


def rotate_triangles(tris: np.ndarray, m: Optional[Sequence[float]]) -> np.ndarray:
    if not m:
        return tris
    r = np.asarray(m, dtype=np.float64).reshape(3, 3)
    return tris @ r.T


def face_down_rotation(direction: Sequence[float]) -> Tuple[float, ...]:
    """Smallest rotation turning ``direction`` to point straight down (-Z)."""
    d = np.asarray(direction, dtype=np.float64)
    d = d / max(np.linalg.norm(d), 1e-12)
    target = np.array([0.0, 0.0, -1.0])
    c = float(np.dot(d, target))
    if c > 1 - 1e-9:
        return IDENTITY
    if c < -1 + 1e-9:
        r = np.diag([1.0, -1.0, -1.0])  # 180 degrees about X
    else:
        axis = np.cross(d, target)
        s = float(np.linalg.norm(axis))
        k = axis / s
        kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        angle = math.atan2(s, c)
        r = np.eye(3) + math.sin(angle) * kx + (1 - math.cos(angle)) * (kx @ kx)
    return tuple(_clean(v) for v in r.reshape(-1))


def _down_direction(m: Optional[Sequence[float]]) -> np.ndarray:
    """The STL's own direction that points down once oriented: R^T (0, 0, -1)."""
    r = np.asarray(m or IDENTITY, dtype=np.float64).reshape(3, 3)
    return r.T @ np.array([0.0, 0.0, -1.0])


def _face_normals(tris: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    cr = np.cross(b - a, c - a)
    dbl = np.sqrt(np.einsum("ij,ij->i", cr, cr))
    signed = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())
    sign = -1.0 if signed < 0 else 1.0
    safe = np.where(dbl > 0, dbl, 1.0)
    return sign * cr / safe[:, None], 0.5 * dbl


def flat_faces(tris: np.ndarray, limit: int = MAX_FLAT_FACE_CANDIDATES) -> List[Tuple[np.ndarray, float]]:
    """Largest flat areas as (outward normal, area mm²): faces grouped by normal direction (about 3 degrees)."""
    normals, area = _face_normals(tris)
    ok = area > 0
    if not ok.any():
        return []
    n, w = normals[ok], area[ok]
    q = np.round(n * 20.0).astype(np.int64)
    keys, inverse = np.unique(q, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    sums = np.bincount(inverse, weights=w)
    total = float(w.sum())
    out: List[Tuple[np.ndarray, float]] = []
    for idx in np.argsort(sums)[::-1]:
        if len(out) >= limit or sums[idx] < max(total * 0.01, 1.0):
            break
        mask = inverse == idx
        normal = (n[mask] * w[mask, None]).sum(axis=0)
        norm = float(np.linalg.norm(normal))
        if norm <= 0:
            continue
        out.append((normal / norm, float(sums[idx])))
    del keys
    return out


def candidate_orientations(tris: np.ndarray) -> List[Dict[str, Any]]:
    """Six 'which side down' orientations plus laying the largest flat faces on the plate (duplicates removed)."""
    cands: List[Dict[str, Any]] = []
    downs: List[np.ndarray] = []

    def add(direction, label, kind):
        d = np.asarray(direction, dtype=np.float64)
        d = d / max(np.linalg.norm(d), 1e-12)
        if any(float(np.dot(d, o)) > SAME_DOWN_COS for o in downs):
            return
        downs.append(d)
        cands.append({"orientation": face_down_rotation(d), "label": label, "kind": kind})

    for direction, label in _DOWN_LABELS.items():
        add(direction, label, "axis")
    for normal, area in flat_faces(tris):
        add(normal, f"Flat face down ({area:,.0f} mm²)", "face")
    for c in cands:
        if np.allclose(c["orientation"], IDENTITY, atol=1e-6):
            c["orientation"] = None
    return cands


def _read_triangles(analysis) -> np.ndarray:
    from .print_estimate_model import stl_triangles

    stl = getattr(analysis, "stl_file", None)
    if not stl:
        raise ValueError("The STL file of this analysis is not available.")
    with stl.open("rb") as fh:
        return stl_triangles(fh.read())


def _cache_key(analysis, m) -> Tuple[str, str, str]:
    stl = getattr(analysis, "stl_file", None)
    return (str(getattr(analysis, "pk", "")), str(getattr(stl, "name", "") or ""), orientation_key(m))


def features_for(analysis, m: Optional[Sequence[float]], tris: Optional[np.ndarray] = None):
    """(MeshFeatures, min corner [x, y, z]) of the STL in orientation ``m``; cached per analysis and orientation."""
    from .print_estimate_model import MeshFeatures, compute_features

    key = _cache_key(analysis, m)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
    if hit is not None:
        return MeshFeatures.from_dict(hit[0]), list(hit[1])
    if tris is None:
        tris = _read_triangles(analysis)
    rotated = rotate_triangles(tris, m)
    features = compute_features(rotated)
    mins = rotated.reshape(-1, 3).min(axis=0).tolist()
    with _cache_lock:
        _cache[key] = (features.to_dict(), mins)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return features, mins


def oriented_bounding_box(features, mins: Sequence[float]) -> Dict[str, Any]:
    sx, sy, sz = (float(v) for v in features.size)
    return {
        "min": {"x": mins[0], "y": mins[1], "z": mins[2]},
        "max": {"x": mins[0] + sx, "y": mins[1] + sy, "z": mins[2] + sz},
        "size": {"x": sx, "y": sy, "z": sz},
    }


def evaluate_orientations(analysis, *, material=None, infill_percent=None, support_settings=None) -> Dict[str, Any]:
    """Estimate every candidate orientation with the file's settings (or the given what-ifs) and pick the one with
    the least support material, then the shortest print, among those that fit the printer.

    With supports turned off the candidates are compared as if Auto were chosen, so the overhangs that would need
    support still count. Read-only."""
    from .print_3d_service import SUPPORT_SETTING_KEYS, model_estimate, support_options
    from .print_estimate_model import SUPPORT_AUTO, SUPPORT_NONE, resolve_profile
    from .print_size_limit import equipment_print_size_limit, fits_print_size

    stored = analysis.slicer_settings or {}
    material = material if material is not None else analysis.material
    density = float(material.density_g_per_cm3) if material else 1.24
    infill = float(infill_percent if infill_percent is not None else (stored.get("infill_percent") or 100.0))
    supports = dict(support_settings if support_settings is not None else {k: v for k, v in stored.items() if k in SUPPORT_SETTING_KEYS})
    scored_mode = str(supports.get("support_mode") or SUPPORT_AUTO)
    if scored_mode == SUPPORT_NONE:
        supports["support_mode"] = SUPPORT_AUTO
        scored_mode = SUPPORT_AUTO
    opts = support_options(supports)
    profile = resolve_profile(getattr(analysis, "equipment", None))
    limit = equipment_print_size_limit(getattr(analysis, "equipment", None))

    tris = _read_triangles(analysis)
    current = stored_orientation(analysis)
    cands = candidate_orientations(tris)
    current_down = _down_direction(current)
    current_idx = next(
        (i for i, c in enumerate(cands) if float(np.dot(_down_direction(c["orientation"]), current_down)) > SAME_DOWN_COS),
        None,
    )
    if current_idx is None:
        cands.insert(0, {"orientation": current, "label": "Current orientation", "kind": "current"})
        current_idx = 0
    else:
        # Same side down as the current orientation: keep the user's exact turn on the plate.
        cands[current_idx]["orientation"] = current

    rows = []
    for i, c in enumerate(cands):
        features, _mins = features_for(analysis, c["orientation"], tris)
        _w, _t, b = model_estimate(features, profile, infill_percent=infill, density_g_per_cm3=density, supports=opts)
        size = [round(float(v), 2) for v in features.size]
        rows.append(
            {
                "label": c["label"],
                "kind": c["kind"],
                "orientation": list(c["orientation"]) if c["orientation"] else None,
                "is_current": i == current_idx,
                "size_mm": size,
                "fits": fits_print_size(size, limit),
                "support_g": round(float(b.get("support_g") or 0), 2),
                "total_g": round(float(b.get("total_g") or 0), 2),
                "total_min": round(float(b.get("total_min") or 0), 1),
                "height_mm": size[2],
                "overhang_area_mm2": round(float(b.get("overhang_area_mm2") or 0), 1),
                "support_mode": b.get("support_mode"),
            }
        )

    fitting = [r for r in rows if r["fits"]] or rows
    least = min(r["support_g"] for r in fitting)
    tie = max(SUPPORT_TIE_G, least * SUPPORT_TIE_PCT / 100.0)
    contenders = [r for r in fitting if r["support_g"] <= least + tie]
    cur = rows[current_idx]
    if cur in contenders and cur["fits"]:
        # Keep the user's orientation unless another one saves support or is clearly faster.
        best = min(contenders, key=lambda r: (r["total_min"] + (0 if r is cur else 1.0), r["height_mm"]))
    else:
        best = min(contenders, key=lambda r: (r["total_min"], r["height_mm"]))
    best_idx = rows.index(best)
    return {
        "scored_support_mode": scored_mode,
        "current_index": current_idx,
        "best_index": best_idx,
        "candidates": rows,
        "saving": {
            "support_g": round(cur["support_g"] - best["support_g"], 2),
            "total_g": round(cur["total_g"] - best["total_g"], 2),
            "total_min": round(cur["total_min"] - best["total_min"], 1),
        },
    }
