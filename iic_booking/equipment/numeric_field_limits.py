"""Parse NUMERIC dynamic-field limits from help_text / options."""

from __future__ import annotations

from typing import Any, Optional, Tuple


DEFAULT_NUMERIC_MIN = 0.0
DEFAULT_NUMERIC_MAX = 100.0
DEFAULT_NUMERIC_STEP = 1.0


def _to_float(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
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

    Blank or non-numeric lines are ignored for that slot.
    """
    if not help_text or not str(help_text).strip():
        return {}
    lines = str(help_text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: dict[str, float] = {}
    if len(lines) >= 1 and lines[0].strip() != "":
        n = _to_float(lines[0].strip())
        if n is not None:
            out["min"] = n
    if len(lines) >= 2 and lines[1].strip() != "":
        n = _to_float(lines[1].strip())
        if n is not None:
            out["max"] = n
    if len(lines) >= 3 and lines[2].strip() != "":
        n = _to_float(lines[2].strip())
        if n is not None and n > 0:
            out["step"] = n
    return out


def _options_dict(options: Any) -> dict:
    if isinstance(options, dict):
        return options
    return {}


def resolve_numeric_field_bounds(
    *,
    options: Any = None,
    help_text: Optional[str] = None,
    formula_max: Optional[float] = None,
) -> Tuple[float, float, float]:
    """
    Resolve (min, max, step) for a NUMERIC dynamic field.

    Priority:
      min/step: options → help_text → defaults (0 / 1)
      max: formula_max (if provided) → options.max → help_text → default 100
    """
    opts = _options_dict(options)
    from_help = parse_numeric_help_text(help_text)

    min_v = _to_float(opts.get("min"))
    if min_v is None:
        min_v = from_help.get("min", DEFAULT_NUMERIC_MIN)

    step_v = _to_float(opts.get("step"))
    if step_v is None or step_v <= 0:
        step_v = from_help.get("step", DEFAULT_NUMERIC_STEP)

    if formula_max is not None:
        max_v = float(formula_max)
    else:
        max_v = _to_float(opts.get("max"))
        if max_v is None:
            max_v = from_help.get("max", DEFAULT_NUMERIC_MAX)

    if max_v < min_v:
        max_v = min_v
    if step_v <= 0:
        step_v = DEFAULT_NUMERIC_STEP
    return float(min_v), float(max_v), float(step_v)
