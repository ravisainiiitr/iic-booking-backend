"""Parse NUMERIC dynamic-field limits from help_text / options."""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple


DEFAULT_NUMERIC_MIN = 0.0
DEFAULT_NUMERIC_MAX = 100.0
DEFAULT_NUMERIC_STEP = 1.0

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
