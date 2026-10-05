"""Indian financial year helpers (1 April – 31 March). Labels look like ``2026-27``."""

from __future__ import annotations

import re
from datetime import date

from django.utils import timezone

FY_START_MONTH = 4
_LABEL_RE = re.compile(r"^(\d{4})-(\d{2})$")


def fy_start_year(d: date) -> int:
    return d.year if d.month >= FY_START_MONTH else d.year - 1


def fy_label(d: date | None = None) -> str:
    d = d or timezone.localdate()
    start = fy_start_year(d)
    return f"{start}-{str(start + 1)[-2:]}"


def fy_bounds(label: str) -> tuple[date, date]:
    """Inclusive first and last day of the financial year ``label``."""
    start = parse_fy_label(label)
    return date(start, FY_START_MONTH, 1), date(start + 1, FY_START_MONTH - 1, 31)


def parse_fy_label(label: str) -> int:
    m = _LABEL_RE.match((label or "").strip())
    if not m:
        raise ValueError("Financial year must look like 2026-27.")
    start = int(m.group(1))
    if f"{start + 1}"[-2:] != m.group(2):
        raise ValueError("Financial year must cover two consecutive years, e.g. 2026-27.")
    return start


def is_valid_fy_label(label: str) -> bool:
    try:
        parse_fy_label(label)
    except ValueError:
        return False
    return True
