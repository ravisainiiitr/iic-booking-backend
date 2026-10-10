"""Academic years for TA / operator nomination calls.

IIT Roorkee's academic year runs July to June and is labelled ``YYYY-YY`` (e.g. ``2026-27``). Calls are tied
to a ``Semester`` row; when no semester exists for the chosen academic year one full-year row is created on
demand (code ``AY-2026-27``), so the picker never depends on the Main Admin having pre-created semesters.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

ACADEMIC_YEAR_START_MONTH = 7
LABEL_RE = re.compile(r"^(\d{4})-(\d{2})$")


class AcademicYearError(ValueError):
    pass


def start_year_for(day: date) -> int:
    return day.year if day.month >= ACADEMIC_YEAR_START_MONTH else day.year - 1


def label_for_start_year(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def current_label(today: date | None = None) -> str:
    return label_for_start_year(start_year_for(today or timezone.localdate()))


def bounds(label: str) -> tuple[date, date]:
    start_year = parse_label(label)
    return date(start_year, ACADEMIC_YEAR_START_MONTH, 1), date(start_year + 1, ACADEMIC_YEAR_START_MONTH, 1) - timedelta(days=1)


def parse_label(label: str) -> int:
    m = LABEL_RE.match((label or "").strip())
    if not m:
        raise AcademicYearError("Academic year must look like 2026-27.")
    start_year = int(m.group(1))
    if int(m.group(2)) != (start_year + 1) % 100:
        raise AcademicYearError("Academic year must span two consecutive years, e.g. 2026-27.")
    return start_year


def label_from_semester(semester_obj) -> str:
    """``2025-26`` from a semester's code/name (``2025-26 Odd``, ``AY-2025-26``, ``2025-2026``), else a fallback."""
    if semester_obj is None:
        return ""
    text = f"{getattr(semester_obj, 'code', '')} {getattr(semester_obj, 'name', '')}".strip()
    m_short = re.search(r"\b(\d{4}-\d{2})\b", text)
    if m_short:
        return m_short.group(1)
    m_full = re.search(r"\b(\d{4}-\d{4})\b", text)
    if m_full:
        a, b = m_full.group(1).split("-")
        return f"{a}-{b[-2:]}"
    m_year = re.search(r"\b(20\d{2})\b", text)
    if m_year:
        return m_year.group(1)
    return getattr(semester_obj, "name", "") or getattr(semester_obj, "code", "") or ""


def _semester_for_label(label: str, *, today: date, active_only: bool = True):
    from .models import Semester

    qs = Semester.objects.all()
    if active_only:
        qs = qs.filter(is_active=True)
    matches = [s for s in qs.order_by("-start_date", "-id") if label_from_semester(s) == label]
    if not matches:
        return None
    covering = [s for s in matches if s.start_date and s.end_date and s.start_date <= today <= s.end_date]
    return (covering or matches)[0]


def options(today: date | None = None) -> list[dict]:
    """Current and next academic year (always offered) plus any other year with an active semester."""
    from .models import Semester

    today = today or timezone.localdate()
    current = start_year_for(today)
    labels = [label_for_start_year(current), label_for_start_year(current + 1)]
    for sem in Semester.objects.filter(is_active=True).order_by("-start_date"):
        label = label_from_semester(sem)
        if LABEL_RE.match(label) and label not in labels:
            labels.append(label)
    out = []
    for label in labels:
        sem = _semester_for_label(label, today=today)
        closed = sem is None and _semester_for_label(label, today=today, active_only=False) is not None
        start, end = bounds(label)
        out.append(
            {
                "label": label,
                "semester_id": sem.id if sem else None,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "is_current": label == label_for_start_year(current),
                "available": not closed,
                "unavailable_reason": "Closed by the Main Admin (Admin Settings → Semesters)." if closed else "",
            }
        )
    return out


def ensure_semester(label: str, *, today: date | None = None):
    """Active semester for the academic year, creating a full-year one when none exists."""
    from .models import Semester

    today = today or timezone.localdate()
    start_year = parse_label(label)
    current = start_year_for(today)
    if not current - 1 <= start_year <= current + 1:
        raise AcademicYearError("Choose the previous, current or next academic year.")
    sem = _semester_for_label(label, today=today)
    if sem is not None:
        return sem
    if _semester_for_label(label, today=today, active_only=False) is not None:
        raise AcademicYearError(
            f"Academic year {label} is closed by the Main Admin (Admin Settings → Semesters). Reopen it or choose another year."
        )
    start, end = bounds(label)
    with transaction.atomic():
        sem, _ = Semester.objects.get_or_create(
            code=f"AY-{label}",
            defaults={"name": f"Academic Year {label}", "start_date": start, "end_date": end, "is_active": True},
        )
    return sem
