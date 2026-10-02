"""Parse NUMERIC dynamic-field limits from help_text / options."""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple


DEFAULT_NUMERIC_MIN = 0.0
DEFAULT_NUMERIC_MAX = 100.0
DEFAULT_NUMERIC_STEP = 1.0
# Numeric user inputs (counts of samples, slots, parts ...) cannot be 0 or negative.
NUMERIC_MIN_FLOOR = 1.0
MIN_BELOW_ONE_MESSAGE = (
    "Min must be at least 1: number inputs cannot be 0. For decimal values set a Step below 1; "
    "for negative values tick Allow negative."
)

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _is_truthy_option(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _to_float(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n != n:
            return None
        return n
    raw = str(value).strip().replace(",", ".")
    if not raw:
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        m = _NUMBER_RE.search(raw)
        if not m:
            return None
        try:
            n = float(m.group(0))
        except (TypeError, ValueError):
            return None
    if n != n:  # NaN
        return None
    return n


def parse_numeric_help_text(help_text: Optional[str]) -> dict[str, float]:
    """
    NUMERIC help_text convention:
      line 1 → lower limit (min)
      line 2 → upper limit (max)
      line 3 → step / resolution (e.g. 0.01)

    Also accepts a single line: "0 100 0.01" / "0,100,0.01" / "0;100;0.01".
    Blank or non-numeric lines are ignored for that slot.
    """
    if not help_text or not str(help_text).strip():
        return {}
    normalized = str(help_text).replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = normalized.split("\n")
    out: dict[str, float] = {}

    if len(lines) >= 2:
        min_v = _to_float(lines[0]) if lines[0].strip() else None
        max_v = _to_float(lines[1]) if len(lines) > 1 and lines[1].strip() else None
        step_v = _to_float(lines[2]) if len(lines) > 2 and lines[2].strip() else None
        if min_v is not None:
            out["min"] = min_v
        if max_v is not None:
            out["max"] = max_v
        if step_v is not None and step_v > 0:
            out["step"] = step_v
        return out

    parts = [p for p in re.split(r"[,;\s]+", normalized) if p]
    if len(parts) >= 3:
        min_v = _to_float(parts[0])
        max_v = _to_float(parts[1])
        step_v = _to_float(parts[2])
        if min_v is not None:
            out["min"] = min_v
        if max_v is not None:
            out["max"] = max_v
        if step_v is not None and step_v > 0:
            out["step"] = step_v
    elif len(parts) == 1:
        n = _to_float(parts[0])
        if n is not None:
            if 0 < n < 1:
                out["step"] = n
            else:
                out["min"] = n
    return out


def _options_dict(options: Any) -> dict:
    if isinstance(options, dict):
        return options
    return {}


def numeric_max_formula(options: Any) -> str:
    """options.max_formula (e.g. "B*4"), or a legacy plain-formula options value ("B*4" / ["B*4"]); "" when none."""
    if isinstance(options, dict):
        formula = options.get("max_formula")
    elif isinstance(options, str):
        formula = options
    elif isinstance(options, list) and len(options) == 1 and isinstance(options[0], str):
        formula = options[0]
    else:
        formula = None
    return formula.strip() if isinstance(formula, str) else ""


def numeric_constraints(*, options: Any = None, help_text: Optional[str] = None) -> dict[str, Any]:
    """
    The min / max / step / max_formula configured on the equipment for a NUMERIC field.

    The single place that decides where a numeric limit comes from: each of min, max and step is taken
    from ``options`` when set there, else from the help-text convention (lines 1-3), else None (no
    default applied). ``source`` records "options" / "help_text" / None per key.
    """
    opts = _options_dict(options)
    from_help = parse_numeric_help_text(help_text)
    out: dict[str, Any] = {"max_formula": numeric_max_formula(options), "source": {}}
    for key in ("min", "max", "step"):
        value = _to_float(opts.get(key))
        if key == "step" and value is not None and value <= 0:
            value = None
        source = "options" if value is not None else None
        if value is None and key in from_help:
            value, source = from_help[key], "help_text"
        out[key] = value
        out["source"][key] = source
    return out


_PLAIN_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$")


def is_numeric_help_text_convention(help_text: Optional[str]) -> bool:
    """True when the help text is only the min / max / step numbers (nothing a user should read)."""
    if not help_text or not str(help_text).strip():
        return False
    normalized = str(help_text).replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = normalized.split("\n")
    if len(lines) >= 2:
        tokens = [line.strip() for line in lines]
        if len(tokens) > 3:
            return False
        tokens = [t for t in tokens if t]
    else:
        tokens = [p for p in re.split(r"[,;\s]+", normalized) if p]
        if len(tokens) not in (1, 3):
            return False
    return bool(tokens) and all(_PLAIN_NUMBER_RE.match(t) for t in tokens) and bool(parse_numeric_help_text(help_text))


def numeric_help_text_for_display(field_type: Any, help_text: Optional[str]) -> str:
    """Help text to show users: empty for a NUMERIC field whose help text is only the limit numbers."""
    text = (help_text or "").strip()
    if str(field_type or "").upper() == "NUMERIC" and is_numeric_help_text_convention(text):
        return ""
    return text


_FORMULA_TOKEN_RE = re.compile(r"\bSLOT_DURATION_MINUTES\b|\b[A-Z]\b")
_FORMULA_EXPR_RE = re.compile(r"[0-9.+\-*/()\s]+")
_NUMERIC_OPTION_LABELS = {"min": "Min", "max": "Max", "step": "Step"}


def _strict_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        try:
            n = float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def _json_number(value: float):
    return int(value) if float(value).is_integer() else value


def max_formula_error(formula: str) -> Optional[str]:
    """Why ``formula`` is not a usable max formula (field keys A-Z, SLOT_DURATION_MINUTES, numbers, + - * / ( )), else None."""
    expr = _FORMULA_TOKEN_RE.sub("1", formula)
    if not _FORMULA_EXPR_RE.fullmatch(expr):
        return "Max formula may only use field keys A–Z (capitals), SLOT_DURATION_MINUTES, numbers and + - * / ( )."
    try:
        eval(compile(expr, "<max_formula>", "eval"), {"__builtins__": {}}, {})  # noqa: S307 - digits/operators only
    except ZeroDivisionError:
        return None
    except Exception:  # noqa: BLE001
        return "Max formula is not a valid expression (e.g. B*4)."
    return None


def normalize_numeric_field_config(options: Any, help_text: Optional[str]) -> Tuple[Any, str]:
    """
    Validate the Min / Max / Step / Max formula of a NUMERIC field and keep them in ``options``.

    Returns (options, help_text). Help text that is only the min / max / step convention is folded into
    ``options`` (keys already in options win, as they do when resolving) and cleared, so the limits a
    booking sees do not change. Any other help text is kept as is. Legacy string / list formula options
    are returned untouched. Raises ValueError with a message for the admin when a value is invalid.
    """
    help_text = help_text or ""
    if options in (None, "", []):
        opts: dict[str, Any] = {}
    elif isinstance(options, dict):
        opts = dict(options)
    else:
        return options, help_text

    for key, label in _NUMERIC_OPTION_LABELS.items():
        raw = opts.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            opts.pop(key, None)
            continue
        value = _strict_number(raw)
        if value is None:
            raise ValueError(f"{label} must be a number.")
        opts[key] = _json_number(value)

    formula = opts.get("max_formula")
    if formula is None or (isinstance(formula, str) and not formula.strip()):
        opts.pop("max_formula", None)
    else:
        formula = str(formula).strip()
        error = max_formula_error(formula)
        if error:
            raise ValueError(error)
        opts["max_formula"] = formula

    if is_numeric_help_text_convention(help_text):
        for key, value in parse_numeric_help_text(help_text).items():
            opts.setdefault(key, _json_number(value))
        help_text = ""

    min_v, max_v, step_v = opts.get("min"), opts.get("max"), opts.get("step")
    if step_v is not None and step_v <= 0:
        raise ValueError("Step must be greater than 0.")
    if min_v is not None and max_v is not None and min_v > max_v:
        raise ValueError(f"Min ({min_v}) cannot be greater than Max ({max_v}).")
    if step_v is not None and float(step_v).is_integer():
        for key in ("min", "max"):
            if opts.get(key) is not None and not float(opts[key]).is_integer():
                raise ValueError(
                    f"{_NUMERIC_OPTION_LABELS[key]} must be a whole number when Step is a whole number."
                )
    allows_negative = _is_truthy_option(opts.get("allow_negative")) or _is_truthy_option(opts.get("allowNegative"))
    if (
        min_v is not None
        and 0 <= min_v < NUMERIC_MIN_FLOOR
        and not allows_negative
        and not (step_v is not None and step_v < 1)
    ):
        raise ValueError(MIN_BELOW_ONE_MESSAGE)
    return (opts if opts else []), help_text


def numeric_field_allows_below_one(
    *, options: Any = None, help_text: Optional[str] = None, default_value: Any = None
) -> bool:
    """
    True when the equipment set the field up for decimal or negative values, so the minimum of 1 does
    not apply: Allow negative, a negative or fractional Min (e.g. -7 eV, 0.1 s/step), a Step below 1, or a
    negative / fractional default (e.g. a 0.02 degree step size). A Min of 0 alone is not such a signal.
    """
    opts = _options_dict(options)
    if _is_truthy_option(opts.get("allow_negative")) or _is_truthy_option(opts.get("allowNegative")):
        return True
    configured = numeric_constraints(options=options, help_text=help_text)
    if configured["min"] is not None and configured["min"] < NUMERIC_MIN_FLOOR and configured["min"] != 0:
        return True
    if configured["step"] is not None and configured["step"] < 1:
        return True
    default = None
    if default_value is not None and not (isinstance(default_value, str) and not default_value.strip()):
        default = _strict_number(default_value)
    return default is not None and default < NUMERIC_MIN_FLOOR and default != 0


def _step_from_default(default_value: Any) -> Optional[float]:
    """Resolution of a fractional default (0.02 -> 0.01), so an unset Step does not round it away."""
    if default_value is None or isinstance(default_value, bool):
        return None
    n = _strict_number(default_value)
    if n is None or n.is_integer():
        return None
    text = f"{abs(n):.10f}".rstrip("0")
    places = len(text.split(".", 1)[1]) if "." in text else 0
    return 10.0 ** -places if places else None


def resolve_numeric_field_bounds(
    *,
    options: Any = None,
    help_text: Optional[str] = None,
    formula_max: Optional[float] = None,
    default_value: Any = None,
    apply_min_floor: bool = True,
) -> Tuple[float, float, float]:
    """
    Resolve (min, max, step) for a NUMERIC dynamic field.

    Priority (see ``numeric_constraints``):
      min: options → help_text → default 0
      step: options → help_text → resolution of a fractional default_value → default 1
      max: formula_max (if provided) → options.max → help_text → default 100

    The min is at least 1 (``NUMERIC_MIN_FLOOR``) unless the field is set up for decimal or negative
    values (``numeric_field_allows_below_one``) or ``apply_min_floor`` is False.
    """
    opts = _options_dict(options)
    configured = numeric_constraints(options=options, help_text=help_text)

    min_v = configured["min"] if configured["min"] is not None else DEFAULT_NUMERIC_MIN
    if configured["step"] is not None:
        step_v = configured["step"]
    else:
        step_v = _step_from_default(default_value) or DEFAULT_NUMERIC_STEP
    if apply_min_floor and min_v < NUMERIC_MIN_FLOOR and not numeric_field_allows_below_one(
        options=options, help_text=help_text, default_value=default_value
    ):
        min_v = NUMERIC_MIN_FLOOR

    if formula_max is not None:
        max_v = float(formula_max)
    else:
        max_v = configured["max"] if configured["max"] is not None else DEFAULT_NUMERIC_MAX

    allow_negative = _is_truthy_option(opts.get("allow_negative")) or _is_truthy_option(
        opts.get("allowNegative")
    )
    if allow_negative and min_v >= 0:
        min_v = -abs(max_v if max_v != 0 else DEFAULT_NUMERIC_MAX)

    if max_v < min_v:
        max_v = min_v
    if step_v <= 0:
        step_v = DEFAULT_NUMERIC_STEP
    return float(min_v), float(max_v), float(step_v)


def initial_numeric_value(*, options: Any = None, help_text: Optional[str] = None, default_value: Any = None,
                          is_required: bool = False) -> str:
    """
    Value the booking form starts a NUMERIC field with (mirrors the frontend ``initialNumericFieldValue``):
    the default clamped to the field's limits; "" for an optional field whose default is below its
    minimum (e.g. a legacy default of 0) or that has no default; else the minimum.
    """
    min_v, max_v, _step = resolve_numeric_field_bounds(options=options, help_text=help_text, default_value=default_value)
    raw = None if default_value is None or (isinstance(default_value, str) and not default_value.strip()) else default_value
    parsed = _strict_number(raw) if raw is not None else None
    if parsed is not None:
        if parsed < min_v and not is_required:
            return ""
        return str(_json_number(min(max_v, max(min_v, parsed))))
    return str(_json_number(min_v)) if is_required else ""
