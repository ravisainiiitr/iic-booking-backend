"""Advanced table (TYPED_TABLE) dynamic input fields.

The schema lives in ``DynamicInputField.table_config``::

    {
      "version": 1,
      "columns": [
        {"key": "sample_code", "label": "Sample code", "type": "TEXT", "required": true,
         "help_text": "", "default": null, "max_length": 100},
        {"key": "temp", "label": "Max temperature", "type": "NUMERIC", "min": 0, "max": 1000,
         "step": 0.1, "integer": false},
        {"key": "phase", "label": "Phase", "type": "RADIO", "options": ["Solid", "Liquid"]},
      ],
      "rows": {"mode": "USER" | "LINKED", "link_field_key": "A", "min_rows": 0, "max_rows": 50,
               "initial_rows": 1, "serial_column": true, "allow_duplicate": true}
    }

A value is a list of row dicts keyed by column key (the S.No. column is display only). NUMERIC cells are
numbers, TEXT / RADIO / COMBO strings, MULTI_SELECT and PERIODIC_TABLE lists of strings, TOGGLE booleans.
In LINKED mode the row count must equal the linked NUMERIC field's value in the same sample set (capped at
max_rows). In charge / time formulas the table's field key stands for its number of filled rows.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Iterator, Optional

from .periodic_elements import PERIODIC_SYMBOL_BY_TOKEN

TYPED_TABLE = "TYPED_TABLE"
COLUMN_TYPES = ("NUMERIC", "TEXT", "RADIO", "COMBO", "MULTI_SELECT", "TOGGLE", "PERIODIC_TABLE")
CHOICE_COLUMN_TYPES = ("RADIO", "COMBO", "MULTI_SELECT")
ROW_MODES = ("USER", "LINKED")

MAX_COLUMNS = 20
MAX_ROWS_CAP = 200
DEFAULT_MAX_ROWS = 50
MAX_OPTIONS = 50
TEXT_MAX_LENGTH_CAP = 500
MAX_LABEL_LENGTH = 100
MAX_HELP_LENGTH = 300

_COLUMN_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FIELD_KEY_RE = re.compile(r"^[A-Z]$")
_ELEMENT_SYMBOLS = frozenset(PERIODIC_SYMBOL_BY_TOKEN.values())


class TableConfigError(ValueError):
    """Raised when an advanced table schema is invalid (message is shown to the admin)."""


def _slugify_key(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(label or "").lower()).strip("_")
    if not slug:
        slug = "col"
    if not slug[0].isalpha():
        slug = f"c_{slug}"
    return slug[:40]


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _to_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _pretty_number(value: float):
    return int(value) if float(value).is_integer() else round(float(value), 6)


def _to_int(value: Any, *, name: str, default: Optional[int], low: int, high: int) -> Optional[int]:
    if value is None or value == "":
        return default
    number = _to_number(value)
    if number is None or not float(number).is_integer():
        raise TableConfigError(f"{name} must be a whole number.")
    number = int(number)
    if number < low or number > high:
        raise TableConfigError(f"{name} must be between {low} and {high}.")
    return number


def _normalize_options(raw: Any, label: str) -> list:
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines()]
    if not isinstance(raw, list):
        raw = []
    options = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("value", item.get("label", ""))
        text = str(item if item is not None else "").strip()
        if not text:
            continue
        if text in options:
            raise TableConfigError(f'Column "{label}": option "{text}" is listed twice.')
        if len(text) > MAX_LABEL_LENGTH:
            raise TableConfigError(f'Column "{label}": options can be at most {MAX_LABEL_LENGTH} characters.')
        options.append(text)
    if not options:
        raise TableConfigError(f'Column "{label}" needs at least one option.')
    if len(options) > MAX_OPTIONS:
        raise TableConfigError(f'Column "{label}" can have at most {MAX_OPTIONS} options.')
    return options


def _normalize_column(raw: Any, index: int, used_keys: set) -> dict:
    if not isinstance(raw, dict):
        raise TableConfigError(f"Column {index} is invalid.")
    label = str(raw.get("label") or "").strip()
    if not label:
        raise TableConfigError(f"Column {index} needs a label.")
    if len(label) > MAX_LABEL_LENGTH:
        raise TableConfigError(f'Column "{label[:30]}…" label can be at most {MAX_LABEL_LENGTH} characters.')
    col_type = str(raw.get("type") or "TEXT").strip().upper()
    if col_type not in COLUMN_TYPES:
        raise TableConfigError(f'Column "{label}": unknown type "{col_type}".')

    raw_key = str(raw.get("key") or "").strip()
    if raw_key:
        key = raw_key.lower()
        if not _COLUMN_KEY_RE.match(key):
            raise TableConfigError(
                f'Column "{label}": key "{raw_key}" must start with a letter and use only letters, digits and _.'
            )
        if key in used_keys:
            raise TableConfigError(f'Column key "{key}" is used by more than one column.')
    else:
        base = _slugify_key(label)
        key, n = base, 2
        while key in used_keys:
            suffix = f"_{n}"
            key = f"{base[:40 - len(suffix)]}{suffix}"
            n += 1
    used_keys.add(key)

    help_text = str(raw.get("help_text") or "").strip()
    if len(help_text) > MAX_HELP_LENGTH:
        raise TableConfigError(f'Column "{label}": help text can be at most {MAX_HELP_LENGTH} characters.')

    column: dict = {
        "key": key,
        "label": label,
        "type": col_type,
        "required": _to_bool(raw.get("required")),
        "help_text": help_text,
    }

    if col_type in CHOICE_COLUMN_TYPES:
        column["options"] = _normalize_options(raw.get("options"), label)

    if col_type == "NUMERIC":
        low = _to_number(raw.get("min"))
        high = _to_number(raw.get("max"))
        step = _to_number(raw.get("step"))
        if raw.get("min") not in (None, "") and low is None:
            raise TableConfigError(f'Column "{label}": lower limit must be a number.')
        if raw.get("max") not in (None, "") and high is None:
            raise TableConfigError(f'Column "{label}": upper limit must be a number.')
        if raw.get("step") not in (None, "") and step is None:
            raise TableConfigError(f'Column "{label}": step must be a number.')
        if low is not None and high is not None and low > high:
            raise TableConfigError(f'Column "{label}": lower limit cannot be greater than the upper limit.')
        if step is not None and step <= 0:
            raise TableConfigError(f'Column "{label}": step must be greater than 0.')
        integer = _to_bool(raw.get("integer"))
        if integer and step is not None and not float(step).is_integer():
            raise TableConfigError(f'Column "{label}": whole-number columns need a whole-number step.')
        column.update({
            "min": _pretty_number(low) if low is not None else None,
            "max": _pretty_number(high) if high is not None else None,
            "step": _pretty_number(step) if step is not None else None,
            "integer": integer,
        })

    if col_type == "TEXT":
        column["max_length"] = _to_int(
            raw.get("max_length"), name=f'Column "{label}": maximum length', default=None,
            low=1, high=TEXT_MAX_LENGTH_CAP,
        )

    raw_default = raw.get("default")
    column["default"] = None
    if raw_default not in (None, "", []) and col_type != "PERIODIC_TABLE":
        cleaned, problem = _clean_cell(column, raw_default)
        if problem:
            raise TableConfigError(f'Column "{label}": default value — {problem["message"]}')
        if col_type == "TOGGLE":
            cleaned = bool(cleaned)
        column["default"] = cleaned
    return column


def normalize_table_config(raw: Any) -> dict:
    """Validated, normalised schema. Raises TableConfigError with an admin-facing message."""
    if raw in (None, "", {}):
        raise TableConfigError("Add at least one column to the advanced table.")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise TableConfigError("Advanced table configuration is not valid JSON.") from exc
    if not isinstance(raw, dict):
        raise TableConfigError("Advanced table configuration must be an object.")
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise TableConfigError("Add at least one column to the advanced table.")
    if len(raw_columns) > MAX_COLUMNS:
        raise TableConfigError(f"An advanced table can have at most {MAX_COLUMNS} columns.")
    used: set = set()
    columns = [_normalize_column(c, i, used) for i, c in enumerate(raw_columns, start=1)]

    raw_rows = raw.get("rows") if isinstance(raw.get("rows"), dict) else {}
    mode = str(raw_rows.get("mode") or "USER").strip().upper()
    if mode not in ROW_MODES:
        raise TableConfigError('Row mode must be "USER" (user adds rows) or "LINKED" (rows follow a field).')
    max_rows = _to_int(raw_rows.get("max_rows"), name="Maximum rows", default=DEFAULT_MAX_ROWS, low=1, high=MAX_ROWS_CAP)
    link_key = str(raw_rows.get("link_field_key") or "").strip().upper()
    if mode == "LINKED":
        if not _FIELD_KEY_RE.match(link_key):
            raise TableConfigError("Choose the field key (A–Z) that sets the number of rows.")
        min_rows, initial_rows = 0, 0
    else:
        link_key = ""
        min_rows = _to_int(raw_rows.get("min_rows"), name="Minimum rows", default=0, low=0, high=MAX_ROWS_CAP)
        if min_rows > max_rows:
            raise TableConfigError("Minimum rows cannot be greater than maximum rows.")
        initial_rows = _to_int(
            raw_rows.get("initial_rows"), name="Initial rows", default=max(1, min_rows), low=0, high=MAX_ROWS_CAP
        )
        initial_rows = min(max(initial_rows, min_rows), max_rows)
    return {
        "version": 1,
        "columns": columns,
        "rows": {
            "mode": mode,
            "link_field_key": link_key or None,
            "min_rows": min_rows,
            "max_rows": max_rows,
            "initial_rows": initial_rows,
            "serial_column": _to_bool(raw_rows.get("serial_column"), True),
            "allow_duplicate": _to_bool(raw_rows.get("allow_duplicate"), True),
        },
    }


def table_link_key(config: Any) -> Optional[str]:
    rows = (config or {}).get("rows") if isinstance(config, dict) else None
    if isinstance(rows, dict) and str(rows.get("mode") or "").upper() == "LINKED":
        key = str(rows.get("link_field_key") or "").strip().upper()
        return key or None
    return None


def _attr(field: Any, name: str, default=None):
    if isinstance(field, dict):
        return field.get(name, default)
    return getattr(field, name, default)


def validate_table_links(fields: Iterable[Any]) -> Optional[str]:
    """Cross-field check for one user-type group of dynamic fields (dicts or model rows).

    A linked advanced table must point at a NUMERIC field of the same group, never at itself. Because the
    target must be NUMERIC (never a table), link chains and cycles cannot form.
    """
    fields = list(fields)
    by_key = {}
    for f in fields:
        key = str(_attr(f, "field_key") or "").strip().upper()
        if key:
            by_key[key] = f
    for f in fields:
        if str(_attr(f, "field_type") or "") != TYPED_TABLE:
            continue
        key = str(_attr(f, "field_key") or "").strip().upper()
        link = table_link_key(_attr(f, "table_config"))
        if not link:
            continue
        label = _attr(f, "field_label") or key
        if link == key:
            return f'Field {key} ("{label}"): rows cannot be linked to the table itself.'
        target = by_key.get(link)
        if target is None:
            return f'Field {key} ("{label}"): linked field {link} does not exist for this user type.'
        if str(_attr(target, "field_type") or "") != "NUMERIC":
            return f'Field {key} ("{label}"): linked field {link} must be a Numeric field.'
    return None


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def parse_table_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except ValueError:
            return value
    return value


def _is_blank_cell(column: dict, value: Any) -> bool:
    if value is None:
        return True
    if column["type"] == "TOGGLE":
        return value is False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False


def is_row_blank(config: dict, row: Any) -> bool:
    if not isinstance(row, dict):
        return True
    return all(_is_blank_cell(c, row.get(c["key"])) for c in config.get("columns") or [])


def filled_row_count(value: Any) -> int:
    """Rows with at least one filled cell (what the table's field key stands for in formulas)."""
    value = parse_table_value(value)
    if not isinstance(value, list):
        return 0
    count = 0
    for row in value:
        if not isinstance(row, dict):
            continue
        if any(v not in (None, "", [], False) for v in row.values()):
            count += 1
    return count


def _as_string_list(value: Any) -> Optional[list]:
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        out = []
        for item in value:
            if item is None or isinstance(item, (dict, list)):
                return None
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    return None


def _clean_cell(column: dict, value: Any):
    """(clean value, problem or None) for one cell; blank cells come back as None (False for toggles)."""
    col_type = column["type"]
    label = column["label"]
    if col_type == "TOGGLE":
        if value in (None, ""):
            return False, None
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on", "y"):
            return True, None
        if text in ("0", "false", "no", "off", "n"):
            return False, None
        return None, {"kind": "invalid", "message": f"{label} must be Yes or No."}
    if value is None or value == [] or (isinstance(value, str) and not value.strip()):
        return None, None
    if col_type == "NUMERIC":
        number = _to_number(value)
        if number is None:
            return None, {"kind": "invalid", "message": f"{label} must be a number."}
        if column.get("integer") and not float(number).is_integer():
            return None, {"kind": "integer", "message": f"{label} must be a whole number."}
        low, high = column.get("min"), column.get("max")
        if low is not None and number < float(low):
            return None, {"kind": "min", "limit": low, "value": _pretty_number(number),
                          "message": f"{label} cannot be less than {low}."}
        if high is not None and number > float(high):
            return None, {"kind": "max", "limit": high, "value": _pretty_number(number),
                          "message": f"{label} cannot be greater than {high}."}
        return _pretty_number(number), None
    if col_type == "TEXT":
        if isinstance(value, (dict, list)):
            return None, {"kind": "invalid", "message": f"{label} must be text."}
        text = str(value).strip()
        limit = column.get("max_length") or TEXT_MAX_LENGTH_CAP
        if len(text) > limit:
            return None, {"kind": "max_length", "limit": limit,
                          "message": f"{label} can be at most {limit} characters."}
        return text, None
    if col_type in ("RADIO", "COMBO"):
        if isinstance(value, (dict, list)):
            return None, {"kind": "invalid", "message": f"{label}: choose one option."}
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        text = str(value).strip()
        if text not in (column.get("options") or []):
            return None, {"kind": "option", "message": f'{label}: "{text}" is not one of the options.'}
        return text, None
    if col_type == "MULTI_SELECT":
        items = _as_string_list(value)
        if items is None:
            return None, {"kind": "invalid", "message": f"{label}: choose from the options."}
        allowed = column.get("options") or []
        out = []
        for item in items:
            if item not in allowed:
                return None, {"kind": "option", "message": f'{label}: "{item}" is not one of the options.'}
            if item not in out:
                out.append(item)
        return (out or None), None
    if col_type == "PERIODIC_TABLE":
        items = _as_string_list(value)
        if items is None:
            return None, {"kind": "invalid", "message": f"{label}: choose elements from the periodic table."}
        out = []
        for item in items:
            symbol = PERIODIC_SYMBOL_BY_TOKEN.get(item.lower())
            if not symbol or symbol not in _ELEMENT_SYMBOLS:
                return None, {"kind": "invalid", "message": f'{label}: "{item}" is not an element symbol.'}
            if symbol not in out:
                out.append(symbol)
        return (out or None), None
    return None, None


def linked_row_target(config: dict, group: dict) -> Optional[int]:
    """Rows a LINKED table needs for ``group`` (one sample set): the linked value capped at max_rows."""
    link = table_link_key(config)
    if not link:
        return None
    number = _to_number((group or {}).get(link))
    count = int(number) if number is not None and number > 0 else 0
    max_rows = int(((config.get("rows") or {}).get("max_rows")) or DEFAULT_MAX_ROWS)
    return min(count, max_rows)


def clean_table_rows(
    field: Any, value: Any, group: dict, *, check_required: bool = True
) -> tuple[list, list]:
    """Clean one table value against ``field.table_config``. Returns (rows, problems).

    USER mode drops fully blank rows; LINKED mode keeps rows positional so the count matches the linked
    field. ``check_required`` (off for templates) enforces required columns, minimum rows and the linked
    row count; maximum rows and every cell limit are always checked.
    """
    config = _attr(field, "table_config") or {}
    columns = config.get("columns") or []
    rows_cfg = config.get("rows") or {}
    key = _attr(field, "field_key")
    label = _attr(field, "field_label") or key
    problems: list = []

    def problem(kind, message, *, row=None, column=None, limit=None, value_=None):
        item = {"key": key, "label": label, "kind": kind, "message": message, "row": row,
                "column": column, "limit": limit}
        if value_ is not None:
            item["value"] = value_
        problems.append(item)

    parsed = parse_table_value(value)
    if parsed in (None, ""):
        parsed = []
    if not isinstance(parsed, list):
        problem("invalid", f"{label} must be a list of rows.")
        return [], problems

    linked = table_link_key(config) is not None
    cleaned_rows: list = []
    for index, raw_row in enumerate(parsed, start=1):
        if raw_row is None:
            raw_row = {}
        if not isinstance(raw_row, dict):
            problem("invalid", f"{label}, row {index} is invalid.", row=index)
            continue
        row: dict = {}
        for column in columns:
            cell, cell_problem = _clean_cell(column, raw_row.get(column["key"]))
            if cell_problem:
                problem(
                    cell_problem["kind"], f"{label}, row {index}: {cell_problem['message']}", row=index,
                    column=column["key"], limit=cell_problem.get("limit"), value_=cell_problem.get("value"),
                )
                continue
            if cell is not None:
                row[column["key"]] = cell
        if not linked and is_row_blank(config, row):
            continue
        cleaned_rows.append(row)

    max_rows = int(rows_cfg.get("max_rows") or DEFAULT_MAX_ROWS)
    count = len(cleaned_rows)
    if linked:
        target = linked_row_target(config, group)
        if check_required and count != target:
            link = table_link_key(config)
            problem(
                "row_count",
                f"{label} must have {target} row{'s' if target != 1 else ''} "
                f"(set by field {link}); it has {count}.",
                limit=target,
            )
        elif count > max_rows:
            problem("max_rows", f"{label}: at most {max_rows} rows are allowed.", limit=max_rows)
    else:
        if count > max_rows:
            problem("max_rows", f"{label}: at most {max_rows} rows are allowed.", limit=max_rows)
        min_rows = int(rows_cfg.get("min_rows") or 0)
        needed = max(min_rows, 1 if _attr(field, "is_required") else 0)
        if check_required and count < needed:
            problem(
                "min_rows",
                f"{label}: add at least {needed} row{'s' if needed != 1 else ''}.",
                limit=needed,
            )

    if check_required:
        for index, row in enumerate(cleaned_rows, start=1):
            for column in columns:
                if column.get("required") and column["type"] != "TOGGLE" and _is_blank_cell(
                    column, row.get(column["key"])
                ):
                    if any(p["row"] == index and p["column"] == column["key"] for p in problems):
                        continue
                    problem(
                        "required", f"{label}, row {index}: {column['label']} is required.",
                        row=index, column=column["key"],
                    )
    problems.sort(key=lambda p: (p["row"] or 0, p["column"] or ""))
    return cleaned_rows, problems


# ---------------------------------------------------------------------------
# Equipment-level helpers
# ---------------------------------------------------------------------------


def shown_typed_table_fields(equipment, user_type: str) -> list:
    """TYPED_TABLE fields the booking form shows for ``user_type`` (typed rows, else shared rows)."""
    from .equipment_group_service import _effective_input_fields
    from .models import Equipment

    if not isinstance(equipment, Equipment) or equipment.pk is None:
        return []
    return [
        f for f in _effective_input_fields(equipment, str(user_type or ""))
        if f.field_type == TYPED_TABLE and isinstance(f.table_config, dict) and f.table_config.get("columns")
    ]


def typed_table_keys(equipment) -> frozenset:
    """Field keys that are an advanced table for any user type (cached on the instance)."""
    from .models import DynamicInputField, Equipment

    if not isinstance(equipment, Equipment) or equipment.pk is None:
        return frozenset()
    cached = getattr(equipment, "_typed_table_keys_cache", None)
    if isinstance(cached, frozenset):
        return cached

    keys = frozenset(
        DynamicInputField.objects.filter(equipment=equipment, field_type=TYPED_TABLE)
        .values_list("field_key", flat=True)
    )
    try:
        equipment._typed_table_keys_cache = keys
    except AttributeError:
        pass
    return keys


def _iter_groups(values: dict, baseline: Optional[dict]):
    from .calculators import SAMPLE_SETS_KEY, split_sample_sets

    has_baseline = isinstance(baseline, dict)
    base, sets = split_sample_sets(values if isinstance(values, dict) else {})
    old_base, old_sets = split_sample_sets(baseline if has_baseline else {})
    yield 1, base, (old_base if has_baseline else None)
    raw_sets = values.get(SAMPLE_SETS_KEY) if isinstance(values, dict) else None
    if isinstance(raw_sets, list):
        for i, s in enumerate(sets):
            yield i + 2, s, (old_sets[i] if has_baseline and i < len(old_sets) else None)


def _group_unchanged(field, group: dict, old: Optional[dict]) -> bool:
    if old is None:
        return False
    key = field.field_key
    if group.get(key) != old.get(key):
        return False
    link = table_link_key(field.table_config)
    return not link or group.get(link) == old.get(link)


def iter_typed_table_problems(
    equipment, values: dict, *, user_type: str = "", baseline: Optional[dict] = None,
    check_required: bool = True, fields: Optional[list] = None,
) -> Iterator[dict]:
    """Every advanced-table problem in sample set 1 and the extra sets, each with ``set`` (1-based)."""
    fields = shown_typed_table_fields(equipment, user_type) if fields is None else fields
    if not fields:
        return
    for set_no, group, old in _iter_groups(values, baseline):
        for field in fields:
            if _group_unchanged(field, group, old):
                continue
            raw = group.get(field.field_key)
            _rows, problems = clean_table_rows(field, raw, group, check_required=check_required)
            for p in problems:
                p["set"] = set_no
                if set_no > 1:
                    p["message"] = f"Sample set {set_no}: {p['message']}"
                yield p


def clean_typed_tables(
    equipment, values: dict, *, user_type: str = "", baseline: Optional[dict] = None,
    check_required: bool = True,
) -> tuple[dict, Optional[dict]]:
    """Clean every advanced table in ``values`` (base and ``_sample_sets``).

    Returns (values, first problem or None). Values left exactly as stored in ``baseline`` (with the same
    linked count) are kept untouched, so bookings saved under an older schema stay editable.
    """
    from .calculators import SAMPLE_SETS_KEY

    if not isinstance(values, dict):
        return values, None
    fields = shown_typed_table_fields(equipment, user_type)
    if not fields:
        return values, None
    first = next(
        iter_typed_table_problems(
            equipment, values, user_type=user_type, baseline=baseline, check_required=check_required,
            fields=fields,
        ),
        None,
    )
    if first:
        return values, first

    def clean_group(group: dict, old: Optional[dict]) -> dict:
        out = dict(group)
        for field in fields:
            if _group_unchanged(field, group, old):
                continue
            rows, _problems = clean_table_rows(field, group.get(field.field_key), group, check_required=False)
            if rows:
                out[field.field_key] = rows
            else:
                out.pop(field.field_key, None)
        return out

    groups = list(_iter_groups(values, baseline))
    out = clean_group(groups[0][1], groups[0][2])
    if isinstance(values.get(SAMPLE_SETS_KEY), list):
        out[SAMPLE_SETS_KEY] = [clean_group(g, old) for _n, g, old in groups[1:]]
    return out, None


def trim_linked_typed_tables(equipment, values: dict, *, user_type: str = "") -> dict:
    """Drop rows beyond each linked table's count (e.g. after a partial cancellation lowers the samples)."""
    from .calculators import SAMPLE_SETS_KEY

    if not isinstance(values, dict):
        return values
    fields = [f for f in shown_typed_table_fields(equipment, user_type) if table_link_key(f.table_config)]
    if not fields:
        return values

    def trim(group: dict) -> dict:
        out = dict(group)
        for field in fields:
            rows = parse_table_value(out.get(field.field_key))
            if not isinstance(rows, list):
                continue
            target = linked_row_target(field.table_config, out) or 0
            if len(rows) > target:
                if target:
                    out[field.field_key] = rows[:target]
                else:
                    out.pop(field.field_key, None)
        return out

    out = trim({k: v for k, v in values.items() if k != SAMPLE_SETS_KEY})
    if isinstance(values.get(SAMPLE_SETS_KEY), list):
        out[SAMPLE_SETS_KEY] = [trim(s) if isinstance(s, dict) else s for s in values[SAMPLE_SETS_KEY]]
    return out


def restore_typed_tables(equipment, original: Any, reduced: Any, *, user_type: str = "") -> Any:
    """Put the advanced-table rows of ``original`` back into charge-safe ``reduced`` values.

    Charge-safe values hold only each table's row count; a waitlist auto-booking stores them as the
    booking's inputs, so the rows are copied back (per sample set) and linked tables trimmed to fit.
    """
    from .calculators import SAMPLE_SETS_KEY, split_sample_sets

    if not isinstance(original, dict) or not isinstance(reduced, dict):
        return reduced
    keys = typed_table_keys(equipment)
    if not keys:
        return reduced
    old_base, old_sets = split_sample_sets(original)
    out = dict(reduced)

    def copy_into(target: dict, source: dict) -> dict:
        target = dict(target)
        for key in keys:
            if isinstance(source.get(key), list):
                target[key] = source[key]
        return target

    out = copy_into(out, old_base)
    if isinstance(out.get(SAMPLE_SETS_KEY), list):
        out[SAMPLE_SETS_KEY] = [
            copy_into(s, old_sets[i]) if isinstance(s, dict) and i < len(old_sets) else s
            for i, s in enumerate(out[SAMPLE_SETS_KEY])
        ]
    return trim_linked_typed_tables(equipment, out, user_type=user_type)


def format_cell(column: dict, value: Any) -> str:
    if column.get("type") == "TOGGLE":
        return "Yes" if value is True else ("No" if value is False else "")
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def format_typed_table_text(config: Any, value: Any, *, max_rows: Optional[int] = None) -> str:
    """Plain-text rendering, one row per line: "1. Sample code: S1; Temp: 40"."""
    rows = parse_table_value(value)
    if not isinstance(rows, list) or not rows:
        return ""
    columns = (config or {}).get("columns") or [] if isinstance(config, dict) else []
    lines = []
    shown = rows if max_rows is None else rows[:max_rows]
    for index, row in enumerate(shown, start=1):
        if not isinstance(row, dict):
            continue
        if columns:
            parts = [
                f"{c['label']}: {format_cell(c, row.get(c['key']))}"
                for c in columns
                if format_cell(c, row.get(c["key"])) != ""
            ]
        else:
            parts = [f"{k}: {v}" for k, v in row.items() if v not in (None, "", [])]
        lines.append(f"{index}. " + "; ".join(parts))
    if max_rows is not None and len(rows) > max_rows:
        lines.append(f"… {len(rows) - max_rows} more row(s)")
    return "\n".join(lines)


def summarize_typed_table(value: Any) -> str:
    rows = parse_table_value(value)
    count = len(rows) if isinstance(rows, list) else 0
    return f"{count} row{'s' if count != 1 else ''}"
