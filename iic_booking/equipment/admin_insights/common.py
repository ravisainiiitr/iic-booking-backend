"""Query-parameter parsing, paging and scope shared by the dashboard insight pages."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

from django.utils import timezone

from iic_booking.equipment.admin_dashboard_summary import _local_midnight, _Scope

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 500
TRUE_VALUES = ("1", "true", "yes", "on")


def flag(params, key: str) -> bool:
    return str(params.get(key) or "").strip().lower() in TRUE_VALUES


def multi(params, key: str) -> list[str]:
    """Values of ``key`` given repeated (``?k=a&k=b``) or comma-separated (``?k=a,b``)."""
    getlist = getattr(params, "getlist", None)
    raw = getlist(key) if getlist else [params.get(key)]
    out: list[str] = []
    for value in raw:
        for part in str(value or "").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def int_values(values: Iterable[str]) -> list[int]:
    return [int(v) for v in values if str(v).strip().isdigit()]


def parse_date(raw) -> date | None:
    try:
        return datetime.strptime(str(raw or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def period(params, *, default_days: int = 30, now: datetime | None = None) -> tuple[date, date]:
    """Inclusive local dates from ``date_from`` / ``date_to``; the last ``default_days`` days by default."""
    today = timezone.localdate(now or timezone.now())
    end = parse_date(params.get("date_to")) or today
    start = parse_date(params.get("date_from")) or (end - timedelta(days=default_days - 1))
    if start > end:
        start, end = end, start
    return start, end


def bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """``[start 00:00, day after end 00:00)`` in local time."""
    return _local_midnight(start), _local_midnight(end + timedelta(days=1))


def page_params(params) -> tuple[int, int]:
    """``(offset, size)`` from ``page`` / ``page_size`` or ``offset`` / ``limit`` (exports)."""
    def number(key: str, default: int) -> int:
        raw = str(params.get(key) or "").strip()
        return int(raw) if raw.isdigit() else default

    if params.get("limit") is not None:
        size = max(1, min(number("limit", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        return max(0, number("offset", 0)), size
    size = max(1, min(number("page_size", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    page = max(1, number("page", 1))
    return (page - 1) * size, size


def page_meta(total: int, offset: int, size: int) -> dict[str, int]:
    return {
        "count": total,
        "page": offset // size + 1,
        "page_size": size,
        "total_pages": max(1, -(-total // size)),
    }


def iso(value) -> str | None:
    return value.isoformat() if value else None


def money(value) -> float:
    return round(float(value or 0), 2)


def share(part, whole) -> float | None:
    return round(float(part) / float(whole), 4) if whole else None


def scope_for(user, params) -> _Scope:
    """The viewer's scope; a Main Administrator narrows it to one department with ``?dept=<id>``."""
    return _Scope(user, params.get("dept") if params is not None else None)


def scope_payload(scope: _Scope) -> dict[str, Any]:
    from iic_booking.equipment.admin_dashboard_summary import department_choices
    from iic_booking.users.models.department import Department

    department = None
    if scope.department_id:
        department = Department.objects.filter(pk=scope.department_id).values("id", "name").first()
    return {
        "scope": "institute" if scope.is_institute else "department",
        "department": department,
        "selected_department_id": scope.department_id if scope.selected_department else None,
        "departments": department_choices(scope),
    }
