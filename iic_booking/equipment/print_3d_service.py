"""STL parsing, heuristic estimation, and optional CuraEngine slicing for 3D print quotes."""

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


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _triangle_area(v1: Vec3, v2: Vec3, v3: Vec3) -> float:
    ab = (v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2])
    ac = (v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2])
    c = _cross(ab, ac)
    return 0.5 * math.sqrt(_dot(c, c))


def _signed_tetra_volume(v1: Vec3, v2: Vec3, v3: Vec3) -> float:
    return _dot(v1, _cross(v2, v3)) / 6.0


def _is_ascii_stl(data: bytes) -> bool:
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


def compute_mesh_metrics(triangles: List[Triangle], bed_size_mm: Optional[Dict[str, float]] = None) -> StlMeshMetrics:
    if not triangles:
        raise ValueError("STL contains no triangles.")

    warnings: List[str] = []
    signed_volume = 0.0
    surface_area = 0.0
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    for v1, v2, v3 in triangles:
        signed_volume += _signed_tetra_volume(v1, v2, v3)
        surface_area += _triangle_area(v1, v2, v3)
        for x, y, z in (v1, v2, v3):
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            min_z, max_z = min(min_z, z), max(max_z, z)

    volume_mm3 = abs(signed_volume)
    volume_cm3 = volume_mm3 / 1000.0
    size = {"x": max_x - min_x, "y": max_y - min_y, "z": max_z - min_z}
    bbox = {
        "min": {"x": min_x, "y": min_y, "z": min_z},
        "max": {"x": max_x, "y": max_y, "z": max_z},
        "size": size,
    }

    if volume_mm3 < 1e-6:
        warnings.append("Computed volume is near zero — mesh may be open or invalid.")
    if len(triangles) < 12:
        warnings.append("Very low triangle count — model may be overly simplified.")
    if bed_size_mm:
        if size["x"] > bed_size_mm.get("x", 0) or size["y"] > bed_size_mm.get("y", 0) or size["z"] > bed_size_mm.get("z", 0):
            warnings.append(
                f"Model ({size['x']:.1f}×{size['y']:.1f}×{size['z']:.1f} mm) exceeds bed "
                f"({bed_size_mm.get('x')}×{bed_size_mm.get('y')}×{bed_size_mm.get('z')} mm)."
            )

    return StlMeshMetrics(
        triangle_count=len(triangles),
        volume_mm3=volume_mm3,
        volume_cm3=volume_cm3,
        surface_area_mm2=surface_area,
        bounding_box=bbox,
        warnings=warnings,
    )


def material_usage_factor(infill_percent: float) -> float:
    infill = max(0.0, min(100.0, infill_percent)) / 100.0
    shell_share = 0.28
    return shell_share + (1.0 - shell_share) * infill


def estimate_print_time_minutes(
    volume_mm3: float,
    surface_area_mm2: float,
    bbox_height_mm: float,
    *,
    layer_height_mm: float = 0.2,
    infill_percent: float = 20.0,
    perimeter_speed_mm_per_sec: float = 45.0,
    flow_rate_mm3_per_sec: float = 8.0,
    startup_minutes: float = 2.0,
) -> int:
    usage = material_usage_factor(infill_percent)
    material_volume_mm3 = volume_mm3 * usage
    extrude_sec = material_volume_mm3 / max(flow_rate_mm3_per_sec, 0.1)
    layers = max(1.0, bbox_height_mm / max(layer_height_mm, 0.05))
    perimeter_sec = surface_area_mm2 / max(perimeter_speed_mm_per_sec, 1.0)
    layer_overhead_sec = layers * 1.5
    total_sec = extrude_sec + perimeter_sec + layer_overhead_sec + startup_minutes * 60.0
    return max(1, int(round(total_sec / 60.0)))


def estimate_weight_grams(volume_cm3: float, density_g_per_cm3: float, infill_percent: float) -> Decimal:
    usage = material_usage_factor(infill_percent)
    weight = volume_cm3 * density_g_per_cm3 * usage
    return ceil_weight_grams(weight)


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


def analyze_stl_file(
    stl_bytes: bytes,
    *,
    density_g_per_cm3: float,
    slicer_settings: Optional[Dict[str, Any]] = None,
    bed_size_mm: Optional[Dict[str, float]] = None,
) -> PrintEstimate:
    slicer_settings = slicer_settings or {}
    layer_height = float(slicer_settings.get("layer_height_mm", 0.2))
    infill = float(slicer_settings.get("infill_percent", 20))

    triangles = parse_stl_bytes(stl_bytes)
    metrics = compute_mesh_metrics(triangles, bed_size_mm=bed_size_mm)
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

    if weight_g is None:
        weight_g = estimate_weight_grams(metrics.volume_cm3, density_g_per_cm3, infill)
    if time_min is None:
        time_min = estimate_print_time_minutes(
            metrics.volume_mm3,
            metrics.surface_area_mm2,
            metrics.bounding_box["size"]["z"],
            layer_height_mm=layer_height,
            infill_percent=infill,
        )

    bbox = dict(metrics.bounding_box)
    bbox["_volume_mm3"] = metrics.volume_mm3
    bbox["_surface_area_mm2"] = metrics.surface_area_mm2

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


def recalculate_print_estimate(
    analysis,
    *,
    material,
    layer_height_mm: float,
    infill_percent: float,
) -> PrintEstimate:
    """
    Fast re-quote from stored mesh metrics — no STL re-parse or re-slice.
    Used when only material, layer height, or infill changes after initial analysis.
    """
    volume_mm3, surface_area_mm2, bbox_height = mesh_metrics_from_analysis(analysis)
    volume_cm3 = volume_mm3 / 1000.0
    density = float(material.density_g_per_cm3) if material else 1.24
    slicer_settings = default_slicer_settings(layer_height_mm, infill_percent)
    infill = float(slicer_settings.get("infill_percent", infill_percent))
    layer_height = float(slicer_settings.get("layer_height_mm", layer_height_mm))

    weight_g = estimate_weight_grams(volume_cm3, density, infill)
    time_min = estimate_print_time_minutes(
        volume_mm3,
        surface_area_mm2,
        bbox_height,
        layer_height_mm=layer_height,
        infill_percent=infill,
        perimeter_speed_mm_per_sec=float(slicer_settings.get("perimeter_speed_mm_per_sec", 45.0)),
        flow_rate_mm3_per_sec=float(slicer_settings.get("flow_rate_mm3_per_sec", 8.0)),
        startup_minutes=float(slicer_settings.get("startup_minutes", 2.0)),
    )

    bbox = dict(analysis.bounding_box or {})
    bbox["_volume_mm3"] = volume_mm3
    bbox["_surface_area_mm2"] = surface_area_mm2

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


def default_slicer_settings(layer_height_mm: float = 0.1, infill_percent: float = 100.0) -> Dict[str, Any]:
    return {
        "layer_height_mm": layer_height_mm,
        "infill_percent": infill_percent,
        "perimeter_speed_mm_per_sec": 45.0,
        "flow_rate_mm3_per_sec": 8.0,
        "startup_minutes": 2.0,
    }
