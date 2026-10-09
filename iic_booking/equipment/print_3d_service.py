"""STL parsing, model-based estimates (``print_estimate_model``) and optional CuraEngine slicing for 3D print quotes."""

from __future__ import annotations

import logging
import math
import os
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

from .print_size_limit import tiny_model_warning

logger = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]
Triangle = Tuple[Vec3, Vec3, Vec3]


@dataclass
class StlMeshMetrics:
    triangle_count: int
    volume_mm3: float
    volume_cm3: float
    surface_area_mm2: float
    bounding_box: Dict[str, Any]
    warnings: List[str]


@dataclass
class PrintEstimate:
    weight_grams: Decimal
    volume_cm3: Decimal
    estimated_time_minutes: int
    bounding_box: Dict[str, Any]
    warnings: List[str]
    analysis_method: str
    volume_mm3: float = 0.0
    surface_area_mm2: float = 0.0


def _is_ascii_stl(data: bytes) -> bool:
    if len(data) >= 84 and 84 + struct.unpack_from("<I", data, 80)[0] * 50 == len(data):
        return False
    preview = data[:256].decode("utf-8", errors="ignore")
    return preview.lstrip().lower().startswith("solid") and "facet" in preview


def parse_stl_bytes(data: bytes) -> List[Triangle]:
    if _is_ascii_stl(data):
        return _parse_ascii_stl(data.decode("utf-8", errors="ignore"))
    return _parse_binary_stl(data)


def _parse_binary_stl(data: bytes) -> List[Triangle]:
    if len(data) < 84:
        raise ValueError("File is too small to be a valid binary STL.")
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + triangle_count * 50
    if len(data) < expected:
        raise ValueError("Binary STL header reports more triangles than file contains.")
    triangles: List[Triangle] = []
    offset = 84
    for _ in range(triangle_count):
        offset += 12
        v1 = struct.unpack_from("<fff", data, offset)
        v2 = struct.unpack_from("<fff", data, offset + 12)
        v3 = struct.unpack_from("<fff", data, offset + 24)
        triangles.append((v1, v2, v3))
        offset += 38
    return triangles


def _parse_ascii_stl(text: str) -> List[Triangle]:
    triangles: List[Triangle] = []
    vertex_re = re.compile(
        r"vertex\s+([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)",
        re.IGNORECASE,
    )
    for chunk in text.split("endfacet"):
        verts: List[Vec3] = []
        for match in vertex_re.finditer(chunk):
            verts.append((float(match.group(1)), float(match.group(2)), float(match.group(3))))
        if len(verts) >= 3:
            triangles.append((verts[0], verts[1], verts[2]))
    if not triangles:
        raise ValueError("No triangles found in ASCII STL.")
    return triangles


def ceil_weight_grams(weight) -> Decimal:
    """Round weight up to the next whole gram (no fractional grams for billing)."""
    if weight is None:
        return Decimal("0")
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return Decimal("0")
    if w <= 0:
        return Decimal("0")
    return Decimal(int(math.ceil(w)))


def _parse_gcode_time_minutes(gcode_text: str) -> Optional[int]:
    time_match = re.search(r";TIME:(\d+(?:\.\d+)?)", gcode_text, re.IGNORECASE)
    if time_match:
        return max(1, int(round(float(time_match.group(1)) / 60.0)))

    prusa_match = re.search(r"; estimated printing time \(normal mode\) = (.+)", gcode_text, re.IGNORECASE)
    if prusa_match:
        raw = prusa_match.group(1).strip()
        hours = minutes = seconds = 0
        hm = re.search(r"(\d+)\s*h", raw)
        mm = re.search(r"(\d+)\s*m", raw)
        sm = re.search(r"(\d+)\s*s", raw)
        if hm:
            hours = int(hm.group(1))
        if mm:
            minutes = int(mm.group(1))
        if sm:
            seconds = int(sm.group(1))
        total_sec = hours * 3600 + minutes * 60 + seconds
        if total_sec > 0:
            return max(1, int(round(total_sec / 60.0)))

    print_match = re.search(r";Print time: (\d+(?:\.\d+)?)", gcode_text, re.IGNORECASE)
    if print_match:
        return max(1, int(round(float(print_match.group(1)) / 60.0)))
    return None


def _parse_gcode_filament_grams(gcode_text: str) -> Optional[float]:
    patterns = [
        r"; total filament used \[g\] = ([\d.]+)",
        r";Filament used: ([\d.]+)g",
        r"; filament used \[g\] = ([\d.]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, gcode_text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    length_match = re.search(r";Filament used: ([\d.]+)m", gcode_text, re.IGNORECASE)
    if length_match:
        length_m = float(length_match.group(1))
        diameter_mm = 1.75
        dia_match = re.search(r"filament_diameter\s*=\s*([\d.]+)", gcode_text)
        if dia_match:
            diameter_mm = float(dia_match.group(1))
        radius_cm = (diameter_mm / 10.0) / 2.0
        volume_cm3 = math.pi * radius_cm * radius_cm * (length_m * 100.0)
        return volume_cm3 * 1.24
    return None


def run_curaengine_slice(stl_path: Path, gcode_path: Path, slicer_settings: Dict[str, Any]) -> str:
    cura_path = getattr(settings, "CURAENGINE_PATH", "") or os.environ.get("CURAENGINE_PATH", "")
    if not cura_path:
        raise FileNotFoundError("CURAENGINE_PATH is not configured.")

    layer_height = slicer_settings.get("layer_height_mm", 0.2)
    infill = slicer_settings.get("infill_percent", 20)
    cmd = [
        cura_path,
        "slice",
        "-v",
        "-l",
        str(stl_path),
        "-o",
        str(gcode_path),
        "-s",
        f"layer_height={layer_height}",
        "-s",
        f"infill_sparse_density={infill}",
        "-s",
        "machine_width=220",
        "-s",
        "machine_depth=220",
        "-s",
        "machine_height=250",
        "-s",
        "material_print_temperature=210",
        "-s",
        "material_flow=100",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "CuraEngine failed")
    return gcode_path.read_text(encoding="utf-8", errors="ignore")


def _metrics_from_features(features, bed_size_mm: Optional[Dict[str, float]], mins) -> StlMeshMetrics:
    warnings: List[str] = []
    sx, sy, sz = features.size
    size = {"x": sx, "y": sy, "z": sz}
    bbox = {
        "min": {"x": mins[0], "y": mins[1], "z": mins[2]},
        "max": {"x": mins[0] + sx, "y": mins[1] + sy, "z": mins[2] + sz},
        "size": size,
    }
    if features.volume_mm3 < 1e-6:
        warnings.append("Computed volume is near zero — mesh may be open or invalid.")
    if features.triangle_count < 12:
        warnings.append("Very low triangle count — model may be overly simplified.")
    tiny = tiny_model_warning((sx, sy, sz))
    if tiny:
        warnings.append(tiny)
    if bed_size_mm:
        if sx > bed_size_mm.get("x", 0) or sy > bed_size_mm.get("y", 0) or sz > bed_size_mm.get("z", 0):
            warnings.append(
                f"Model ({sx:.1f}×{sy:.1f}×{sz:.1f} mm) exceeds bed "
                f"({bed_size_mm.get('x')}×{bed_size_mm.get('y')}×{bed_size_mm.get('z')} mm)."
            )
    return StlMeshMetrics(
        triangle_count=features.triangle_count,
        volume_mm3=features.volume_mm3,
        volume_cm3=features.volume_mm3 / 1000.0,
        surface_area_mm2=features.area_mm2,
        bounding_box=bbox,
        warnings=warnings,
    )


SUPPORT_SETTING_KEYS = (
    "support_mode",
    "support_density_pct",
    "support_angle_deg",
    "support_material_id",
    "support_material_code",
)


def allowed_support_materials(equipment):
    """Separate support materials the OIC offers on this printer (active master-list materials only)."""
    from .models import PrintMaterial
    from .print_estimate_model import stored_profile

    ids = [i for i in (stored_profile(equipment).get("support_material_ids") or []) if isinstance(i, int)]
    if not ids:
        return PrintMaterial.objects.none()
    return PrintMaterial.objects.filter(pk__in=ids, is_active=True).order_by("display_order", "name")


def support_options(settings: Optional[Dict[str, Any]]):
    """SupportOptions from stored / requested settings; the support material's density is looked up by id."""
    from .models import PrintMaterial
    from .print_estimate_model import SupportOptions

    settings = settings or {}
    density = None
    material_id = settings.get("support_material_id")
    if material_id:
        material = PrintMaterial.objects.filter(pk=material_id).only("density_g_per_cm3").first()
        if material is not None:
            density = float(material.density_g_per_cm3)
    return SupportOptions.from_settings(settings, density)


def model_estimate(
    features,
    profile: Dict[str, Any],
    *,
    infill_percent: float,
    density_g_per_cm3: float,
    supports=None,
):
    """(model-material weight Decimal, minutes int, breakdown dict) from mesh features and a resolved profile.

    The weight is what is charged at the model material's rate: supports are included unless they are printed
    in a separate support material (then they are ``breakdown["support_material_g"]``)."""
    from .print_estimate_model import estimate

    breakdown = estimate(
        features, profile, infill_percent=infill_percent, density_g_cm3=density_g_per_cm3, supports=supports
    )
    return (
        ceil_weight_grams(breakdown.model_material_g),
        max(1, int(round(breakdown.total_min))),
        breakdown.to_dict(),
    )


def analyze_stl_file(
    stl_bytes: bytes,
    *,
    density_g_per_cm3: float,
    slicer_settings: Optional[Dict[str, Any]] = None,
    bed_size_mm: Optional[Dict[str, float]] = None,
    profile: Optional[Dict[str, Any]] = None,
    supports=None,
) -> PrintEstimate:
    from .print_estimate_model import ESTIMATE_KEY, FEATURES_KEY, compute_features, resolve_profile, stl_triangles

    slicer_settings = slicer_settings or {}
    profile = profile if profile is not None else resolve_profile(None)
    infill = float(slicer_settings.get("infill_percent", 20))

    tris = stl_triangles(stl_bytes)
    features = compute_features(tris)
    metrics = _metrics_from_features(features, bed_size_mm, tris.reshape(-1, 3).min(axis=0).tolist())
    warnings = list(metrics.warnings)
    method = "HEURISTIC"

    weight_g: Optional[Decimal] = None
    time_min: Optional[int] = None

    cura_available = bool(getattr(settings, "CURAENGINE_PATH", "") or os.environ.get("CURAENGINE_PATH", ""))
    use_cura = bool(getattr(settings, "PRINT_3D_USE_CURAENGINE", True))
    if cura_available and use_cura:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                stl_path = Path(tmp) / "model.stl"
                gcode_path = Path(tmp) / "output.gcode"
                stl_path.write_bytes(stl_bytes)
                gcode_text = run_curaengine_slice(stl_path, gcode_path, slicer_settings)
                filament_g = _parse_gcode_filament_grams(gcode_text)
                parsed_time = _parse_gcode_time_minutes(gcode_text)
                if filament_g is not None:
                    weight_g = ceil_weight_grams(filament_g)
                if parsed_time is not None:
                    time_min = parsed_time
                if weight_g is not None and time_min is not None:
                    method = "CURAENGINE"
        except Exception as exc:
            logger.warning("CuraEngine slice failed, falling back to heuristic: %s", exc)
            warnings.append("Slicer unavailable or failed; using heuristic estimate.")

    model_weight, model_time, breakdown = model_estimate(
        features,
        profile,
        infill_percent=infill,
        density_g_per_cm3=density_g_per_cm3,
        supports=supports if supports is not None else support_options(slicer_settings),
    )
    if weight_g is None:
        weight_g = model_weight
    if time_min is None:
        time_min = model_time

    bbox = dict(metrics.bounding_box)
    bbox["_volume_mm3"] = metrics.volume_mm3
    bbox["_surface_area_mm2"] = metrics.surface_area_mm2
    bbox[FEATURES_KEY] = features.to_dict()
    if method == "HEURISTIC":
        bbox[ESTIMATE_KEY] = breakdown

    return PrintEstimate(
        weight_grams=weight_g,
        volume_cm3=Decimal(str(round(metrics.volume_cm3, 4))),
        estimated_time_minutes=time_min,
        bounding_box=bbox,
        warnings=warnings,
        analysis_method=method,
        volume_mm3=metrics.volume_mm3,
        surface_area_mm2=metrics.surface_area_mm2,
    )


def mesh_metrics_from_analysis(analysis) -> Tuple[float, float, float]:
    """Return (volume_mm3, surface_area_mm2, bbox_height_mm) from a stored PrintAnalysis."""
    volume_cm3 = float(analysis.volume_cm3 or 0)
    bbox = analysis.bounding_box or {}
    volume_mm3 = float(bbox.get("_volume_mm3") or volume_cm3 * 1000.0)
    surface = bbox.get("_surface_area_mm2")
    if surface is not None:
        surface_area_mm2 = float(surface)
    else:
        size = bbox.get("size", {})
        x = float(size.get("x", 0) or 0)
        y = float(size.get("y", 0) or 0)
        z = float(size.get("z", 0) or 0)
        if x > 0 and y > 0 and z > 0:
            surface_area_mm2 = 2.0 * (x * y + x * z + y * z)
        else:
            surface_area_mm2 = max(volume_mm3 ** (2.0 / 3.0) * 6.0, 1.0)
    height = float(bbox.get("size", {}).get("z", 0) or 0)
    return volume_mm3, surface_area_mm2, height


def analysis_features(analysis):
    """Mesh features of a stored analysis: saved ones, else re-read from its STL, else a box-like stand-in."""
    from .print_estimate_model import FEATURES_KEY, MODEL_VERSION, MeshFeatures, approximate_features, compute_features, stl_triangles

    bbox = analysis.bounding_box or {}
    saved = bbox.get(FEATURES_KEY)
    if isinstance(saved, dict) and saved.get("v") == MODEL_VERSION:
        try:
            return MeshFeatures.from_dict(saved)
        except (KeyError, TypeError, ValueError):
            pass
    stl = getattr(analysis, "stl_file", None)
    if stl:
        try:
            with stl.open("rb") as fh:
                return compute_features(stl_triangles(fh.read()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not re-read STL for analysis %s: %s", getattr(analysis, "pk", None), exc)
    volume_mm3, surface_area_mm2, _height = mesh_metrics_from_analysis(analysis)
    size = bbox.get("size") or {}
    return approximate_features(volume_mm3, surface_area_mm2, (size.get("x"), size.get("y"), size.get("z")))


def recalculate_print_estimate(
    analysis,
    *,
    material,
    layer_height_mm: float,
    infill_percent: float,
    support_settings: Optional[Dict[str, Any]] = None,
) -> PrintEstimate:
    """
    Fast re-quote from the stored mesh features with the printer's current estimate profile (which sets the
    layer height). Used when material, density or supports change after the initial analysis; analyses stored
    before features were kept re-read their STL once. ``support_settings`` (``SUPPORT_SETTING_KEYS``) default
    to the analysis's stored choice. Does not save the analysis.
    """
    from .print_estimate_model import ESTIMATE_KEY, FEATURES_KEY, resolve_profile

    volume_mm3, surface_area_mm2, _height = mesh_metrics_from_analysis(analysis)
    volume_cm3 = volume_mm3 / 1000.0
    density = float(material.density_g_per_cm3) if material else 1.24
    features = analysis_features(analysis)
    profile = resolve_profile(getattr(analysis, "equipment", None))
    if support_settings is None:
        support_settings = {k: v for k, v in (analysis.slicer_settings or {}).items() if k in SUPPORT_SETTING_KEYS}
    weight_g, time_min, breakdown = model_estimate(
        features,
        profile,
        infill_percent=float(infill_percent),
        density_g_per_cm3=density,
        supports=support_options(support_settings),
    )

    bbox = dict(analysis.bounding_box or {})
    bbox["_volume_mm3"] = volume_mm3
    bbox["_surface_area_mm2"] = surface_area_mm2
    bbox[FEATURES_KEY] = features.to_dict()
    bbox[ESTIMATE_KEY] = breakdown

    return PrintEstimate(
        weight_grams=weight_g,
        volume_cm3=Decimal(str(round(volume_cm3, 4))),
        estimated_time_minutes=time_min,
        bounding_box=bbox,
        warnings=list(analysis.warnings or []),
        analysis_method="HEURISTIC",
        volume_mm3=volume_mm3,
        surface_area_mm2=surface_area_mm2,
    )


def preview_print_estimate(
    analysis,
    *,
    material=None,
    infill_percent: Optional[float] = None,
    support_settings: Optional[Dict[str, Any]] = None,
) -> PrintEstimate:
    """Stable, read-only estimate for one analysed STL (one copy). Anything not given keeps the analysis's
    stored choice (material, density, supports). Never saves; safe for live previews and booked analyses.

    Returned ``bounding_box["_estimate"]`` holds the breakdown (model / support / waste grams, separate
    support-material grams, print / support / warm-up minutes)."""
    stored = analysis.slicer_settings or {}
    if infill_percent is None:
        infill_percent = float(stored.get("infill_percent") or 100.0)
    merged_support = {k: v for k, v in stored.items() if k in SUPPORT_SETTING_KEYS}
    if support_settings is not None:
        merged_support = dict(support_settings)
    return recalculate_print_estimate(
        analysis,
        material=material if material is not None else analysis.material,
        layer_height_mm=float(stored.get("layer_height_mm") or 0.1),
        infill_percent=float(infill_percent),
        support_settings=merged_support,
    )


def default_slicer_settings(layer_height_mm: float = 0.1, infill_percent: float = 100.0) -> Dict[str, Any]:
    return {
        "layer_height_mm": layer_height_mm,
        "infill_percent": infill_percent,
        "perimeter_speed_mm_per_sec": 45.0,
        "flow_rate_mm3_per_sec": 8.0,
        "startup_minutes": 2.0,
    }
