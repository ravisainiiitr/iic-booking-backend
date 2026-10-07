"""Maximum print size of a 3D printer and the check of an STL model against it.

The booking form runs the same rule in the browser (``src/lib/printSizeLimit.ts``): STL units are millimetres,
each axis may exceed the maximum by ``TOLERANCE_MM``, and when rotation is allowed the model fits if its sorted
dimensions fit the sorted maximum dimensions (turning it by 90 degrees about any axis). A blank axis has no limit.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

TOLERANCE_MM = 0.5
TINY_MODEL_MM = 1.0
AXES = ("x", "y", "z")

Size3 = Tuple[float, float, float]

_ASCII_VERTEX_RE = re.compile(
    rb"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", re.IGNORECASE
)
_BINARY_DTYPE = np.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


@dataclass(frozen=True)
class PrintSizeLimit:
    x: Optional[float]
    y: Optional[float]
    z: Optional[float]
    allow_rotation: bool = True

    def as_tuple(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return (self.x, self.y, self.z)

    def as_dict(self) -> Dict[str, object]:
        return {"x": self.x, "y": self.y, "z": self.z, "allow_rotation": self.allow_rotation}


def is_binary_stl(data: bytes) -> bool:
    """Same rule as the preview: an exact binary size wins, otherwise an ASCII header with facets/vertices."""
    if len(data) < 84:
        return False
    count = struct.unpack_from("<I", data, 80)[0]
    if 84 + count * 50 == len(data):
        return True
    head = data[:1024].decode("utf-8", errors="ignore")
    if not re.match(r"^\s*solid", head, re.IGNORECASE):
        return True
    return not re.search(r"facet\s+normal|vertex\s", head, re.IGNORECASE)


def stl_bounding_box_size(data: bytes) -> Size3:
    """Width, depth and height (file units, taken as mm) of the model's axis-aligned bounding box."""
    if is_binary_stl(data):
        count = struct.unpack_from("<I", data, 80)[0]
        count = min(count, (len(data) - 84) // 50)
        if count <= 0:
            raise ValueError("The STL file has no triangles.")
        tris = np.frombuffer(data, dtype=_BINARY_DTYPE, count=count, offset=84)
        verts = tris["v"].reshape(-1, 3).astype(np.float64)
    else:
        values = [tuple(map(float, m)) for m in _ASCII_VERTEX_RE.findall(data)]
        if not values:
            raise ValueError("The STL file has no triangles.")
        verts = np.asarray(values, dtype=np.float64)
    verts = verts[np.isfinite(verts).all(axis=1)]
    if not len(verts):
        raise ValueError("The STL file has no valid vertices.")
    size = verts.max(axis=0) - verts.min(axis=0)
    return (float(size[0]), float(size[1]), float(size[2]))


def _positive_or_none(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def equipment_print_size_limit(equipment) -> Optional[PrintSizeLimit]:
    """The equipment's maximum print size, or None when no axis is set (no limit)."""
    if equipment is None:
        return None
    x, y, z = (_positive_or_none(getattr(equipment, f"max_print_size_{a}_mm", None)) for a in AXES)
    if x is None and y is None and z is None:
        return None
    allow = getattr(equipment, "allow_print_rotation_to_fit", True)
    return PrintSizeLimit(x, y, z, allow_rotation=allow is not False)


def fits_print_size(size: Sequence[float], limit: Optional[PrintSizeLimit], tolerance: float = TOLERANCE_MM) -> bool:
    if limit is None:
        return True
    maxima = [m if m is not None else float("inf") for m in limit.as_tuple()]
    dims = [float(s) for s in size]
    if limit.allow_rotation:
        dims = sorted(dims)
        maxima = sorted(maxima)
    return all(d <= m + tolerance for d, m in zip(dims, maxima))


def fits_only_when_rotated(size: Sequence[float], limit: Optional[PrintSizeLimit]) -> bool:
    if limit is None or not limit.allow_rotation:
        return False
    as_is = PrintSizeLimit(*limit.as_tuple(), allow_rotation=False)
    return fits_print_size(size, limit) and not fits_print_size(size, as_is)


def _fmt(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_size(size: Sequence[Optional[float]]) -> str:
    return " × ".join("any" if v is None else _fmt(float(v)) for v in size) + " mm"


def print_size_error(filename: str, size: Sequence[float], limit: Optional[PrintSizeLimit]) -> Optional[str]:
    if fits_print_size(size, limit):
        return None
    rotation = " even when rotated" if limit.allow_rotation else ""
    return (
        f"{filename} is {format_size(size)} (W × D × H), larger than this printer's maximum print size of "
        f"{format_size(limit.as_tuple())}{rotation}. Scale the model down or split it into parts, then upload it again."
    )


def tiny_model_warning(size: Sequence[float]) -> Optional[str]:
    largest = max(float(s) for s in size) if size else 0.0
    if largest <= 0 or largest >= TINY_MODEL_MM:
        return None
    return (
        f"The model is only {format_size(size)}. STL sizes are read in millimetres; if it was exported in metres or "
        "inches, export it again in millimetres."
    )


def check_stl_files(equipment, files: List[Tuple[str, bytes]]) -> Tuple[List[Dict[str, object]], Optional[PrintSizeLimit]]:
    """Return one row per STL that is too large for the equipment (empty when all fit or there is no limit).

    A file that cannot be parsed is left to the regular STL analysis, which reports it.
    """
    limit = equipment_print_size_limit(equipment)
    if limit is None:
        return [], None
    too_large = []
    for name, data in files:
        try:
            size = stl_bounding_box_size(data)
        except (ValueError, struct.error):
            continue
        message = print_size_error(name, size, limit)
        if message:
            too_large.append({"filename": name, "size_mm": [round(s, 2) for s in size], "message": message})
    return too_large, limit


def analysis_size_mm(analysis) -> Optional[Size3]:
    size = (getattr(analysis, "bounding_box", None) or {}).get("size") or {}
    try:
        values = tuple(float(size[a]) for a in AXES)
    except (KeyError, TypeError, ValueError):
        return None
    return values  # type: ignore[return-value]


def analyses_size_error(equipment, analyses) -> Optional[str]:
    """Size error for already-analysed STL files (booking creation and file replacement), else None.

    Every file is checked; the message names the first one too large and counts the others.
    """
    limit = equipment_print_size_limit(equipment)
    if limit is None:
        return None
    messages = []
    for analysis in analyses:
        size = analysis_size_mm(analysis)
        if size is None:
            continue
        name = getattr(analysis, "original_filename", "") or getattr(analysis, "display_part_name", "") or "model.stl"
        message = print_size_error(name, size, limit)
        if message:
            messages.append(message)
    if not messages:
        return None
    if len(messages) > 1:
        return f"{len(messages)} STL files are larger than this printer's maximum print size. {messages[0]}"
    return messages[0]


def print_size_limit_payload(equipment) -> Optional[Dict[str, object]]:
    limit = equipment_print_size_limit(equipment)
    if limit is None:
        return None
    return {**limit.as_dict(), "tolerance_mm": TOLERANCE_MM}
