"""
Booking inputs as people read them: field labels in field order, option labels instead of stored codes,
Yes/No for toggles, chosen elements for the periodic table and rows for table inputs.

Mirrors ``formatBookingInputValue`` in the frontend (src/lib/bookingInputDisplay.ts) so emails, exports,
the Django admin and API summaries say the same as the booking pages.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

SAMPLE_SETS_KEY = "_sample_sets"
COMMENTS_KEY = "comments"
ELEMENTS_SUFFIX = "_elements"

_TABLE_TYPES = ("TABLE", "TYPED_TABLE")


def humanize_key(key: Any) -> str:
    """Readable label for a key without a field definition: "sample_type" -> "Sample type", "B_elements" -> "B elements"."""
    text = str(key or "").strip()
    if not text:
        return ""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_\-]+", " ", text).strip()
    text = " ".join(text.split())
    return text[:1].upper() + text[1:] if text else ""


def clean_label(label: Any) -> str:
    return re.sub(r":\s*$", "", str(label or "").strip())


def normalize_choice_option(option: Any, index: int = 0) -> tuple[str, str]:
    """(value, label) of a RADIO / COMBO / MULTI_SELECT option (string, number or {value, label})."""
    fallback = str(index + 1)
    if option is None:
        return fallback, fallback
    if isinstance(option, bool):
        text = "true" if option else "false"
        return text, text
    if isinstance(option, (str, int, float)):
        return str(option), str(option)
    if isinstance(option, dict):
        def scalar(v):
            return isinstance(v, (str, int, float, bool)) and not isinstance(v, dict)

        raw_value = next((option.get(k) for k in ("value", "label", "id", "name") if option.get(k) is not None), None)
        raw_label = next((option.get(k) for k in ("label", "value", "name", "id") if option.get(k) is not None), None)
        value = str(raw_value) if scalar(raw_value) else fallback
        label = str(raw_label) if scalar(raw_label) else value
        return value, label
    return fallback, fallback


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return ", ".join(_scalar_text(v) for v in value if not isinstance(v, (list, dict)) and _scalar_text(v))
    if isinstance(value, dict):
        return "; ".join(f"{humanize_key(k)}: {_scalar_text(v)}" for k, v in value.items() if _scalar_text(v))
    return str(value).strip()


def choice_label(value: Any, options: Any, field_type: str = "RADIO") -> str:
    """Label of the chosen option; the value itself when it matches no option."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(options, list) or not options:
        return _scalar_text(value)
    normalized = [normalize_choice_option(o, i) for i, o in enumerate(options)]
    labels = [label for _, label in normalized]
    values = [v for v, _ in normalized]
    lowered = str(value).strip().lower()
    if isinstance(value, bool) or lowered in ("true", "false"):
        truthy = value is True or lowered == "true"
        yes = next((i for i, l in enumerate(labels) if l.strip().lower() == "yes"), None)
        no = next((i for i, l in enumerate(labels) if l.strip().lower() == "no"), None)
        if yes is not None and no is not None:
            return labels[yes if truthy else no]
        if len(options) == 2:
            return labels[1] if truthy else labels[0]
    text = str(value).strip()
    if text in values:
        return labels[values.index(text)] or text
    if text in labels:
        return text
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(labels):
            return labels[idx - 1] or text
    return text


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("[", "{"):
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def read_table_rows(raw: Any) -> list[list[str]]:
    """Rows of a TABLE input (list of lists or its JSON), blank rows dropped."""
    value = _parse_json(raw)
    if not isinstance(value, list) or not all(isinstance(r, (list, tuple)) for r in value):
        return []
    rows = [["" if c is None else _scalar_text(c) for c in row] for row in value]
    return [row for row in rows if any(c.strip() for c in row)]


def _typed_table_display(config: Any, raw: Any) -> Optional[dict]:
    columns = (config or {}).get("columns") if isinstance(config, dict) else None
    columns = [c for c in (columns or []) if isinstance(c, dict) and c.get("key")]
    value = _parse_json(raw)
    if not isinstance(value, list):
        return None
    dict_rows = [r for r in value if isinstance(r, dict)]
    if not columns:
        keys: list[str] = []
        for row in dict_rows:
            for k in row:
                if k not in keys:
                    keys.append(k)
        columns = [{"key": k, "label": humanize_key(k)} for k in keys]
    serial = not (isinstance(config, dict) and isinstance(config.get("rows"), dict) and config["rows"].get("serial_column") is False)

    def cell(column: dict, v: Any) -> str:
        if column.get("type") == "TOGGLE":
            return "Yes" if v is True or v == "true" else ("No" if v is False else "")
        if column.get("type") in ("RADIO", "COMBO") and column.get("options"):
            return choice_label(v, column.get("options"), column.get("type"))
        return _scalar_text(v)

    rows = []
    for row in dict_rows:
        cells = [cell(c, row.get(c["key"])) for c in columns]
        if not any(x.strip() for x in cells if x != "No"):
            continue
        rows.append(cells)
    if serial:
        rows = [[str(i + 1), *r] for i, r in enumerate(rows)]
    header = (["S.No."] if serial else []) + [str(c.get("label") or humanize_key(c["key"])) for c in columns]
    return {"columns": header, "rows": rows}


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, dict)) and not value)


def format_input_value(field: dict, values: dict) -> dict:
    """{"kind": "empty"} | {"kind": "text", "text"} | {"kind": "table", "columns", "rows"} for one field of one sample set."""
    ftype = str(field.get("field_type") or "").upper()
    key = field.get("field_key")
    raw = values.get(key) if isinstance(values, dict) else None

    if ftype == "TABLE":
        rows = read_table_rows(raw)
        if not rows:
            return {"kind": "empty"}
        options = field.get("options")
        columns = [normalize_choice_option(o, i)[1] for i, o in enumerate(options)] if isinstance(options, list) else []
        return {"kind": "table", "columns": [c for c in columns if c], "rows": rows}
    if ftype == "TYPED_TABLE" or (ftype not in _TABLE_TYPES and isinstance(_parse_json(raw), list)
                                   and _parse_json(raw) and all(isinstance(r, dict) for r in _parse_json(raw))):
        display = _typed_table_display(field.get("table_config"), raw)
        if display is None:
            return {"kind": "empty"} if _blank(raw) else {"kind": "text", "text": _scalar_text(raw)}
        return {"kind": "table", **display} if display["rows"] else {"kind": "empty"}
    if ftype == "PERIODIC_TABLE":
        elements = [s.strip() for s in str(values.get(f"{key}{ELEMENTS_SUFFIX}") or "").split(",") if s.strip()]
        if elements:
            return {"kind": "text", "text": ", ".join(elements)}
        return {"kind": "empty"} if _blank(raw) else {"kind": "text", "text": _scalar_text(raw)}
    if ftype == "TOGGLE":
        if raw is None or raw == "":
            return {"kind": "empty"}
        return {"kind": "text", "text": "Yes" if raw is True or str(raw).strip().lower() == "true" else "No"}
    if _blank(raw):
        return {"kind": "empty"}
    if ftype in ("RADIO", "COMBO"):
        return {"kind": "text", "text": choice_label(raw, field.get("options"), ftype)}
    if ftype == "MULTI_SELECT" and isinstance(raw, list):
        return {"kind": "text", "text": ", ".join(choice_label(v, field.get("options"), "COMBO") for v in raw)}
    parsed = _parse_json(raw)
    if isinstance(parsed, list) and parsed and all(isinstance(r, (list, tuple)) for r in parsed):
        rows = read_table_rows(parsed)
        return {"kind": "table", "columns": [], "rows": rows} if rows else {"kind": "empty"}
    text = _scalar_text(parsed)
    return {"kind": "text", "text": text} if text else {"kind": "empty"}


def formatted_as_text(value: dict, *, row_separator: str = "; ", max_rows: Optional[int] = None) -> str:
    """One-line text of a formatted value (tables as "Col: v, Col: v; …") for emails, exports and admin lists."""
    if value.get("kind") == "text":
        return value.get("text") or ""
    if value.get("kind") != "table":
        return ""
    columns = value.get("columns") or []
    rows = value.get("rows") or []
    shown = rows if max_rows is None else rows[:max_rows]
    parts = []
    for row in shown:
        if columns and len(columns) >= len(row):
            cells = [f"{columns[i]}: {c}" for i, c in enumerate(row) if str(c).strip() and columns[i] != "S.No."]
        else:
            cells = [str(c) for c in row if str(c).strip()]
        parts.append(", ".join(cells))
    text = row_separator.join(p for p in parts if p)
    if max_rows is not None and len(rows) > max_rows:
        text += f"{row_separator}… {len(rows) - max_rows} more row(s)"
    return text


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------


def field_item(f: Any) -> dict:
    """Display definition of a DynamicInputField (same keys as the booking serializer's input_fields)."""
    item = {
        "field_key": f.field_key,
        "field_label": f.field_label,
        "field_type": f.field_type or "",
    }
    if f.options:
        item["options"] = f.options
    if getattr(f, "table_config", None):
        item["table_config"] = f.table_config
    return item


def equipment_field_items(equipment_id: Any, user_type: str = "", *, cache: Optional[dict] = None) -> list[dict]:
    """Input fields the booking form shows for ``user_type`` (typed rows, else the shared rows), in field order."""
    from .models import DynamicInputField

    if not equipment_id:
        return []
    cache_key = (equipment_id, user_type or "")
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    rows = list(DynamicInputField.objects.filter(equipment_id=equipment_id).order_by("field_key"))
    typed = [f for f in rows if (f.user_type or "") == (user_type or "")] if user_type else []
    chosen = typed or [f for f in rows if not (f.user_type or "")]
    items = [field_item(f) for f in chosen]
    if cache is not None:
        cache[cache_key] = items
    return items


def all_equipment_field_items(equipment_id: Any) -> list[dict]:
    """Every field row of the equipment (all user types), shared rows last so typed labels win on lookups."""
    from .models import DynamicInputField

    if not equipment_id:
        return []
    rows = list(DynamicInputField.objects.filter(equipment_id=equipment_id).order_by("field_key"))
    rows.sort(key=lambda f: (0 if f.user_type else 1, f.field_key))
    return [field_item(f) for f in rows]


# ---------------------------------------------------------------------------
# Whole bookings
# ---------------------------------------------------------------------------


def sample_sets_of(values: Any) -> list[dict]:
    """Sample set 1 (the top-level values) followed by any non-empty extra sets."""
    if not isinstance(values, dict):
        return [{}]
    base = {k: v for k, v in values.items() if k != SAMPLE_SETS_KEY}
    extra = values.get(SAMPLE_SETS_KEY)
    sets = [base]
    if isinstance(extra, list):
        sets.extend(s for s in extra if isinstance(s, dict) and s)
    return sets


def readable_inputs(values: Any, fields: Iterable[dict]) -> dict:
    """
    Inputs per field and sample set, ready to show:
    {"sets": n, "fields": [{"key", "label", "values": [formatted per set]}], "comments": str}.
    Fields left blank in every set are omitted; keys without a definition get a readable label.
    """
    sets = sample_sets_of(values)
    defs = [f for f in fields if f.get("field_key") and f.get("field_key") != COMMENTS_KEY]
    known = {f["field_key"] for f in defs}
    extra_keys: list[str] = []
    for s in sets:
        for k in s:
            if k in known or k in extra_keys or k == COMMENTS_KEY or str(k).startswith("_"):
                continue
            if str(k).endswith(ELEMENTS_SUFFIX) and str(k)[: -len(ELEMENTS_SUFFIX)] in known:
                continue
            extra_keys.append(k)
    for k in extra_keys:
        if str(k).endswith(ELEMENTS_SUFFIX):
            base_key = str(k)[: -len(ELEMENTS_SUFFIX)]
            defs.append({"field_key": k, "field_label": f"{humanize_key(base_key)} elements", "field_type": "TEXT"})
        else:
            defs.append({"field_key": k, "field_label": humanize_key(k), "field_type": ""})
    out = []
    for f in defs:
        formatted = [format_input_value(f, s) for s in sets]
        if all(v["kind"] == "empty" for v in formatted):
            continue
        out.append({"key": f["field_key"], "label": clean_label(f.get("field_label")) or humanize_key(f["field_key"]),
                    "values": formatted})
    comments = values.get(COMMENTS_KEY) if isinstance(values, dict) else None
    return {"sets": len(sets), "fields": out, "comments": str(comments).strip() if comments else ""}


def input_summary_items(values: Any, fields: Iterable[dict], *, max_rows: Optional[int] = 20,
                        include_comments: bool = True) -> list[dict]:
    """[{"key", "label", "value"}] with one-line text values. Several sample sets read "Set 1: …; Set 2: …"."""
    data = readable_inputs(values, fields)
    items = []
    for item in data["fields"]:
        texts = [formatted_as_text(v, max_rows=max_rows) for v in item["values"]]
        if data["sets"] > 1:
            text = "; ".join(f"Set {i + 1}: {t or '—'}" for i, t in enumerate(texts))
        else:
            text = texts[0]
        if text:
            items.append({"key": item["key"], "label": item["label"], "value": text})
    if include_comments and data["comments"]:
        items.append({"key": COMMENTS_KEY, "label": "Comments", "value": data["comments"]})
    return items


def input_summary_lines(values: Any, fields: Iterable[dict], *, max_rows: Optional[int] = 20,
                        include_comments: bool = True) -> list[tuple[str, str]]:
    """[(label, text)] for emails / exports / admin."""
    return [
        (i["label"], i["value"])
        for i in input_summary_items(values, fields, max_rows=max_rows, include_comments=include_comments)
    ]


def with_fabrication_quantity_field(equipment: Any, items: list[dict]) -> list[dict]:
    """3D print / laser equipment always shows Quantity Required under A, even without that input row."""
    from .fabrication import QUANTITY_KEY, QUANTITY_LABEL
    from .models import FABRICATION_PROFILE_TYPES

    if getattr(equipment, "profile_type", None) not in FABRICATION_PROFILE_TYPES:
        return items
    if any(i.get("field_key") == QUANTITY_KEY for i in items):
        return items
    return [{"field_key": QUANTITY_KEY, "field_label": QUANTITY_LABEL, "field_type": "NUMERIC"}, *items]


def booking_display_values(booking: Any) -> dict:
    """Stored inputs as shown and reported (a fabrication booking's A is its Quantity Required, 1 if missing)."""
    from .fabrication import display_input_values

    return display_input_values(getattr(booking, "equipment", None), getattr(booking, "input_values", None) or {})


def booking_input_fields(booking: Any, *, cache: Optional[dict] = None) -> list[dict]:
    """Field definitions for a booking's user type (snapshot, charge profile, then the user's type)."""
    user_type = (
        (getattr(booking, "user_type_snapshot", None) or "").strip()
        or (getattr(getattr(booking, "charge_profile", None), "user_type", None) or "").strip()
        or (getattr(getattr(booking, "user", None), "user_type", None) or "").strip()
    )
    items = equipment_field_items(getattr(booking, "equipment_id", None), user_type, cache=cache)
    return with_fabrication_quantity_field(getattr(booking, "equipment", None), items)


def booking_input_summary_text(booking: Any, *, separator: str = " | ", max_rows: Optional[int] = 20,
                               cache: Optional[dict] = None, include_comments: bool = True) -> str:
    """"Label: value | Label: value" for a booking's inputs."""
    lines = input_summary_lines(
        booking_display_values(booking), booking_input_fields(booking, cache=cache),
        max_rows=max_rows, include_comments=include_comments,
    )
    return separator.join(f"{label}: {text}" for label, text in lines)
