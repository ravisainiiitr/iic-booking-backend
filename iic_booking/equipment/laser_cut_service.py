"""DXF parsing and sheet-cost maths for 2D laser cutting bookings."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)

CUT_ENTITY_TYPES = frozenset(
    {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}
)
MAX_BLOCK_DEPTH = 8
MAX_CUT_ENTITIES = 200_000

# $INSUNITS header codes -> (unit key, millimetres per unit)
INSUNITS_TO_UNIT = {
    1: "in",
    2: "ft",
    4: "mm",
    5: "cm",
    6: "m",
}
UNIT_TO_MM = {
    "mm": Decimal("1"),
    "cm": Decimal("10"),
    "m": Decimal("1000"),
    "in": Decimal("25.4"),
    "ft": Decimal("304.8"),
}
UNIT_LABELS = {
    "mm": "millimetres",
    "cm": "centimetres",
    "m": "metres",
    "in": "inches",
    "ft": "feet",
}
UNITLESS = "unitless"
MONEY_2DP = Decimal("0.01")


class DxfParseError(ValueError):
    """Raised when a DXF cannot be read or contains no cuttable geometry."""


@dataclass
class DxfAnalysisResult:
    detected_units: str
    units: str
    units_assumed: bool
    bbox: dict
    width_mm: Decimal
    height_mm: Decimal
    area_mm2: Decimal
    entity_count: int
    warnings: list[str] = field(default_factory=list)
    cut_features: dict = field(default_factory=dict)


def _iter_cut_entities(entities, depth: int, counter: list[int], warnings: list[str]):
    for entity in entities:
        dxftype = entity.dxftype()
        if dxftype == "INSERT":
            if depth >= MAX_BLOCK_DEPTH:
                if "Deeply nested blocks were skipped." not in warnings:
                    warnings.append("Deeply nested blocks were skipped.")
                continue
            try:
                inserts = list(entity.multi_insert()) if entity.mcount > 1 else [entity]
                for ins in inserts:
                    yield from _iter_cut_entities(ins.virtual_entities(), depth + 1, counter, warnings)
            except Exception as exc:  # noqa: BLE001 - malformed block refs should not abort the whole file
                logger.info("Skipping unreadable INSERT in DXF: %s", exc)
                if "Some block references could not be read and were skipped." not in warnings:
                    warnings.append("Some block references could not be read and were skipped.")
            continue
        if dxftype not in CUT_ENTITY_TYPES:
            continue
        if getattr(entity.dxf, "invisible", 0):
            continue
        counter[0] += 1
        if counter[0] > MAX_CUT_ENTITIES:
            raise DxfParseError(
                f"The drawing has more than {MAX_CUT_ENTITIES:,} cut entities; please simplify it."
            )
        yield entity


def _read_document(data: bytes):
    import ezdxf
    from ezdxf import recover

    try:
        doc, auditor = recover.read(io.BytesIO(data))
    except ezdxf.DXFStructureError as exc:
        raise DxfParseError("This file is not a valid DXF drawing.") from exc
    except Exception as exc:  # noqa: BLE001
        raise DxfParseError("This file could not be read as a DXF drawing.") from exc
    return doc, auditor


def detect_units(doc) -> str:
    try:
        code = int(doc.header.get("$INSUNITS", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    return INSUNITS_TO_UNIT.get(code, UNITLESS)


def bbox_to_mm(bbox: dict, units: str) -> tuple[Decimal, Decimal, Decimal]:
    factor = UNIT_TO_MM.get(units, Decimal("1"))
    width = (Decimal(str(bbox["max_x"])) - Decimal(str(bbox["min_x"]))) * factor
    height = (Decimal(str(bbox["max_y"])) - Decimal(str(bbox["min_y"]))) * factor
    width = width.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    height = height.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    area = (width * height).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return width, height, area


def analyze_dxf_bytes(data: bytes, *, units_override: str | None = None) -> DxfAnalysisResult:
    """Parse a DXF and return the bounding rectangle of all cut geometry (model space)."""
    from ezdxf import bbox as ezbbox

    if not data:
        raise DxfParseError("The file is empty.")
    doc, auditor = _read_document(data)
    warnings: list[str] = []
    if getattr(auditor, "has_errors", False):
        warnings.append("The drawing had structural errors; they were repaired automatically.")

    counter = [0]
    entities = list(_iter_cut_entities(doc.modelspace(), 0, counter, warnings))
    if not entities:
        raise DxfParseError(
            "No cut geometry (lines, polylines, circles, arcs, ellipses or splines) was found in the drawing."
        )

    extents = ezbbox.extents(entities, fast=False)
    if not extents.has_data:
        raise DxfParseError("Could not measure the drawing's size.")
    bbox = {
        "min_x": float(extents.extmin.x),
        "min_y": float(extents.extmin.y),
        "max_x": float(extents.extmax.x),
        "max_y": float(extents.extmax.y),
    }

    detected = detect_units(doc)
    units_assumed = detected == UNITLESS
    if units_override and units_override in UNIT_TO_MM:
        units = units_override
    elif units_assumed:
        units = "mm"
    else:
        units = detected
    if units_assumed:
        warnings.append("The drawing has no units set; millimetres were assumed. Change the unit if this is wrong.")

    width, height, area = bbox_to_mm(bbox, units)
    if width <= 0 or height <= 0:
        raise DxfParseError("The drawing has zero width or height; a 2D outline is required.")

    cut_features = cut_features_or_warning(entities, bbox, warnings)

    return DxfAnalysisResult(
        detected_units=detected,
        units=units,
        units_assumed=units_assumed,
        bbox=bbox,
        width_mm=width,
        height_mm=height,
        area_mm2=area,
        entity_count=counter[0],
        warnings=warnings,
        cut_features=cut_features,
    )


def cut_features_or_warning(entities, bbox: dict, warnings: list[str]) -> dict:
    """Toolpath features for the time estimate; a failure only means no estimate for this part."""
    from .laser_time_model import compute_cut_features

    try:
        features = compute_cut_features(entities, bbox)
    except Exception:  # noqa: BLE001 - the upload must not fail because the time estimate could not be made
        logger.exception("Could not measure the cut path of a DXF")
        warnings.append("The cutting path could not be measured, so no machine time is estimated for this part.")
        return {}
    if features.get("duplicates"):
        warnings.append(
            f"{features['duplicates']} overlapping duplicate line(s) were ignored; remove them so they are not cut twice."
        )
    return features


def analyze_dxf_cut_features(data: bytes) -> dict:
    """Cut features of a stored DXF (for parts uploaded before the time estimate). Raises DxfParseError."""
    doc, _auditor = _read_document(data)
    warnings: list[str] = []
    entities = list(_iter_cut_entities(doc.modelspace(), 0, [0], warnings))
    if not entities:
        raise DxfParseError("No cut geometry was found in the drawing.")
    from ezdxf import bbox as ezbbox

    extents = ezbbox.extents(entities, fast=False)
    if not extents.has_data:
        raise DxfParseError("Could not measure the drawing's size.")
    bbox = {
        "min_x": float(extents.extmin.x),
        "min_y": float(extents.extmin.y),
        "max_x": float(extents.extmax.x),
        "max_y": float(extents.extmax.y),
    }
    from .laser_time_model import compute_cut_features

    return compute_cut_features(entities, bbox)


def part_fits_sheet(width_mm, height_mm, sheet_width_mm, sheet_height_mm) -> bool:
    """True if the part's bounding rectangle fits on the sheet in either orientation."""
    w, h = Decimal(str(width_mm)), Decimal(str(height_mm))
    sw, sh = Decimal(str(sheet_width_mm)), Decimal(str(sheet_height_mm))
    return (w <= sw and h <= sh) or (w <= sh and h <= sw)


def sheet_fit_error(width_mm, height_mm, material) -> str | None:
    if material is None or width_mm is None or height_mm is None:
        return None
    if part_fits_sheet(width_mm, height_mm, material.sheet_width_mm, material.sheet_height_mm):
        return None
    return (
        f"The part is {Decimal(str(width_mm)).normalize():f} × {Decimal(str(height_mm)).normalize():f} mm, "
        f"which does not fit on a {Decimal(material.sheet_width_mm).normalize():f} × "
        f"{Decimal(material.sheet_height_mm).normalize():f} mm sheet of {material.name}."
    )


def laser_part_material_cost(area_mm2, quantity, sheet_width_mm, sheet_height_mm, sheet_rate) -> Decimal:
    """(part bounding area × quantity / sheet area) × sheet rate, rounded to 2 decimals."""
    sheet_area = Decimal(str(sheet_width_mm)) * Decimal(str(sheet_height_mm))
    if sheet_area <= 0:
        return Decimal("0.00")
    qty = max(1, int(quantity or 1))
    cost = (Decimal(str(area_mm2)) * qty / sheet_area) * Decimal(str(sheet_rate))
    return cost.quantize(MONEY_2DP, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- user's own sheet size

# Edge left on every side of the part on the user's own sheet (clamping and kerf). Equipment has no setting for it.
OWN_SHEET_MARGIN_MM = Decimal("5")
MAX_OWN_SHEET_MM = Decimal("20000")


@dataclass(frozen=True)
class OwnSheetSize:
    width_mm: Decimal
    height_mm: Decimal
    rotated: bool = False

    def as_dict(self) -> dict:
        return {"width_mm": format_mm(self.width_mm), "height_mm": format_mm(self.height_mm), "rotated": self.rotated}


def format_mm(value) -> str:
    return f"{Decimal(str(value)).normalize():f}"


def _overflow(w: Decimal, h: Decimal, bed_w: Decimal, bed_h: Decimal) -> Decimal:
    return max(Decimal("0"), w - bed_w) + max(Decimal("0"), h - bed_h)


def _smallest_standard_size(w: Decimal, h: Decimal, standard_sizes) -> tuple[Decimal, Decimal] | None:
    best = None
    for size in standard_sizes or ():
        sw, sh = Decimal(str(size[0])), Decimal(str(size[1]))
        if w <= sw and h <= sh:
            fitted = (sw, sh)
        elif w <= sh and h <= sw:
            fitted = (sh, sw)
        else:
            continue
        if best is None or fitted[0] * fitted[1] < best[0] * best[1]:
            best = fitted
    return best


def own_sheet_size(width_mm, height_mm, *, margin_mm=OWN_SHEET_MARGIN_MM, bed=None, standard_sizes=()):
    """Sheet the user should bring for one part: its bounding box plus ``margin_mm`` on every side, rounded up
    to the next whole mm (or the smallest of ``standard_sizes`` it fits on), turned 90° when that makes it
    overflow the machine ``bed`` (width, height) less. None when the part has no measured size."""
    if width_mm is None or height_mm is None:
        return None
    w, h = Decimal(str(width_mm)), Decimal(str(height_mm))
    if w <= 0 or h <= 0:
        return None
    margin = max(Decimal("0"), Decimal(str(margin_mm or 0)))
    w = (w + 2 * margin).to_integral_value(rounding=ROUND_CEILING)
    h = (h + 2 * margin).to_integral_value(rounding=ROUND_CEILING)
    standard = _smallest_standard_size(w, h, standard_sizes)
    if standard:
        w, h = standard
    rotated = False
    if bed and bed[0] and bed[1]:
        bed_w, bed_h = Decimal(str(bed[0])), Decimal(str(bed[1]))
        if bed_w > 0 and bed_h > 0 and _overflow(h, w, bed_w, bed_h) < _overflow(w, h, bed_w, bed_h):
            w, h, rotated = h, w, True
    return OwnSheetSize(w, h, rotated)


def suggested_own_sheet(analysis) -> OwnSheetSize | None:
    """Own-sheet size from the part's model; the chosen IIC sheet stands in for the machine bed."""
    material = getattr(analysis, "material", None)
    bed = (material.sheet_width_mm, material.sheet_height_mm) if material is not None else None
    return own_sheet_size(analysis.width_mm, analysis.height_mm, bed=bed)


def effective_own_sheet(analysis) -> OwnSheetSize | None:
    """The size the user entered, else the size from the model."""
    if analysis.own_sheet_width_mm is not None and analysis.own_sheet_height_mm is not None:
        return OwnSheetSize(Decimal(analysis.own_sheet_width_mm), Decimal(analysis.own_sheet_height_mm))
    return suggested_own_sheet(analysis)
