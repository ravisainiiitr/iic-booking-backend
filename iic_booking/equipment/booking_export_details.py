"""Every user input of exported bookings, as the booking details page shows it.

Field definitions, values and labels come from ``input_display`` (the server copy of the booking page's
formatter): the equipment's fields for the booking's user type, in field order, labelled with their key
("No. of Samples (A)"), option labels for choices, Yes/No for toggles, tables as rows, one value per sample
set. Keys without a field definition are only shown when the equipment has no fields at all, as on the page.
Everything is loaded in a few batched queries for the whole export.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation

from .input_display import booking_display_values
from .input_display import clean_label
from .input_display import field_item
from .input_display import formatted_as_text
from .input_display import humanize_key
from .input_display import readable_inputs
from .input_display import with_fabrication_quantity_field

_CHUNK = 500


@dataclass
class InputField:
    key: str
    label: str
    values: list[dict]  # formatted value per sample set: {"kind": "empty" | "text" | "table", ...}


@dataclass
class UploadedFile:
    name: str
    part: str = ""
    material: str = ""
    quantity: int = 1


@dataclass
class BookingDetail:
    fields: list[InputField] = field(default_factory=list)
    sets: int = 1
    comments: str = ""
    atmosphere_sensitive: bool = False
    files: list[UploadedFile] = field(default_factory=list)
    charges: list[tuple[str, Decimal | None]] = field(default_factory=list)

    @property
    def has_inputs(self) -> bool:
        return bool(self.fields or self.comments)


def field_label_with_key(key, label) -> str:
    """"No. of Samples (A)": the label as configured on the equipment followed by its field key."""
    text = clean_label(label) or humanize_key(key)
    key = str(key or "")
    if len(key) == 1 and key.isalpha() and not text.endswith(f"({key})"):
        return f"{text} ({key})"
    return text


def field_text(item: InputField, sets: int, *, row_separator: str = "; ") -> str:
    """One-line text of a field over every sample set: "Set 1: … | Set 2: …" when there are several."""
    texts = [formatted_as_text(v, row_separator=row_separator) for v in item.values]
    if sets > 1:
        return " | ".join(f"Set {i + 1}: {t or '—'}" for i, t in enumerate(texts))
    return texts[0] if texts else ""


def files_text(files: list[UploadedFile]) -> str:
    parts = []
    for f in files:
        extra = [x for x in (f.part, f.material) if x]
        if f.quantity and f.quantity != 1:
            extra.append(f"×{f.quantity}")
        parts.append(f"{f.name} ({', '.join(extra)})" if extra else f.name)
    return "; ".join(parts)


def _money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _chunks(ids):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _field_items_by_booking(bookings) -> dict:
    """Field definitions per booking id, with one query for every equipment in the export."""
    from .models import DynamicInputField

    rows_by_equipment = defaultdict(list)
    for chunk in _chunks({b.equipment_id for b in bookings if b.equipment_id}):
        for f in DynamicInputField.objects.filter(equipment_id__in=chunk).order_by("field_key"):
            rows_by_equipment[f.equipment_id].append(f)

    cache: dict = {}
    out = {}
    for b in bookings:
        user_type = (
            (b.user_type_snapshot or "").strip()
            or (getattr(getattr(b, "charge_profile", None), "user_type", None) or "").strip()
            or (getattr(getattr(b, "user", None), "user_type", None) or "").strip()
        )
        key = (b.equipment_id, user_type)
        if key not in cache:
            rows = rows_by_equipment.get(b.equipment_id, [])
            typed = [f for f in rows if (f.user_type or "") == user_type] if user_type else []
            chosen = typed or [f for f in rows if not (f.user_type or "")]
            cache[key] = with_fabrication_quantity_field(b.equipment, [field_item(f) for f in chosen])
        out[b.pk] = cache[key]
    return out


def _uploaded_files(bookings) -> dict:
    """Active (not removed or replaced) 3D print / laser files per booking id; names only, never URLs."""
    from .models import FABRICATION_PROFILE_TYPES
    from .models import LaserCutAnalysis
    from .models import PrintAnalysis

    ids = [b.pk for b in bookings if getattr(b.equipment, "profile_type", None) in FABRICATION_PROFILE_TYPES]
    files = defaultdict(list)
    for model in (PrintAnalysis, LaserCutAnalysis):
        for chunk in _chunks(ids):
            qs = (
                model.objects.filter(booking_id__in=chunk, cancelled_at__isnull=True)
                .select_related("material")
                .only("booking_id", "original_filename", "part_name", "quantity", "material_code_snapshot",
                      "material__name", "sequence", "created_at")
                .order_by("booking_id", "sequence", "created_at")
            )
            for item in qs:
                name = (item.original_filename or "").strip() or "File"
                part = (item.part_name or "").strip()
                material = getattr(item.material, "name", "") if item.material_id else ""
                files[item.booking_id].append(
                    UploadedFile(
                        name=name,
                        part=part if part and part != name else "",
                        material=(material or item.material_code_snapshot or "").strip(),
                        quantity=int(item.quantity or 1),
                    ),
                )
    return files


def _charge_lines(booking) -> list[tuple[str, Decimal | None]]:
    lines = []
    for line in booking.charge_breakdown or []:
        if not isinstance(line, dict):
            continue
        description = str(line.get("description") or "").strip()
        amount = _money(line.get("amount"))
        if description or amount is not None:
            lines.append((description, amount))
    return lines


def build_booking_details(bookings, *, include_charges: bool) -> dict:
    """{booking pk: BookingDetail} for already loaded bookings (with equipment, user and charge profile)."""
    field_items = _field_items_by_booking(bookings)
    files = _uploaded_files(bookings)
    out = {}
    for b in bookings:
        defs = field_items.get(b.pk) or []
        data = readable_inputs(booking_display_values(b), defs)
        known = {d.get("field_key") for d in defs}
        fields = [
            InputField(key=item["key"], label=field_label_with_key(item["key"], item["label"]), values=item["values"])
            for item in data["fields"]
            if not defs or item["key"] in known
        ]
        out[b.pk] = BookingDetail(
            fields=fields,
            sets=data["sets"],
            comments=data["comments"],
            atmosphere_sensitive=bool(getattr(b, "atmosphere_sensitive_sample", False)),
            files=files.get(b.pk, []),
            charges=_charge_lines(b) if include_charges else [],
        )
    return out
