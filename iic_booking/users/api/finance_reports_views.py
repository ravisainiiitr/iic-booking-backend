"""Finance / Accounts In-charge executive report dashboard API."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.finance_reports import build_finance_report
from iic_booking.users.models.user_type import UserType


def _is_finance_or_admin(user) -> bool:
    return getattr(user, "user_type", None) in (UserType.FINANCE, UserType.ADMIN)


def _parse_ymd(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _financial_year_start(d: date) -> date:
    """India financial year runs 1 April – 31 March."""
    if d.month >= 4:
        return date(d.year, 4, 1)
    return date(d.year - 1, 4, 1)


def _quarter_start(d: date) -> date:
    q_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, q_month, 1)


def resolve_finance_report_date_range(
    preset: Optional[str],
    date_from_raw: Optional[str],
    date_to_raw: Optional[str],
) -> tuple[date, date]:
    """
    Resolve (date_from, date_to) from a preset code, falling back to explicit
    date_from/date_to (custom range), and finally to "this month" when nothing is given.
    """
    today = timezone.localdate()
    code = (preset or "").strip().lower()

    if code == "today":
        return today, today
    if code == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if code == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if code == "this_month":
        return today.replace(day=1), today
    if code == "this_quarter":
        return _quarter_start(today), today
    if code == "this_financial_year":
        return _financial_year_start(today), today

    # "custom" or unrecognised: use explicit dates, defaulting missing ends to sane values.
    start = _parse_ymd(date_from_raw)
    end = _parse_ymd(date_to_raw)
    if start is None and end is None:
        start = today.replace(day=1)
        end = today
    elif start is None:
        start = end.replace(day=1)
    elif end is None:
        end = today
    if end < start:
        start, end = end, start
    return start, end


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def finance_report_dashboard(request):
    """
    GET /api/finance/reports/dashboard/?date_from=&date_to=&preset=

    Executive finance dashboard for the Account In-charge (finance) role, scoped to
    their own internal department. Accessible to Finance and Admin user types only.

    Query params:
      - preset: today | yesterday | this_week | this_month | this_quarter |
                this_financial_year | custom
      - date_from, date_to: YYYY-MM-DD (used when preset is "custom" or omitted)
    """
    if not _is_finance_or_admin(request.user):
        return Response(
            {"error": "Only the Account In-charge (Finance) or Admin can access this report."},
            status=status.HTTP_403_FORBIDDEN,
        )

    preset = request.query_params.get("preset")
    date_from_raw = request.query_params.get("date_from")
    date_to_raw = request.query_params.get("date_to")

    date_from, date_to = resolve_finance_report_date_range(preset, date_from_raw, date_to_raw)

    data = build_finance_report(user=request.user, date_from=date_from, date_to=date_to)
    return Response(data, status=status.HTTP_200_OK)
