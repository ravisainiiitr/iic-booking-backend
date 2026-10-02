"""Home-page overview for the Main Administrator (institute) and Department Administrators (own department).

Booking figures use the Reports & Statistics scope and charge definitions
(``booking_report_metrics``), so the overview always agrees with the Reports page. The payload is
shared by everyone with the same scope and cached for ``CACHE_SECONDS``.

Items that already appear in ``/api/notifications/pending-actions/`` for the signed-in user are not
repeated in ``attention``; the dashboard merges both lists.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Callable

from django.core.cache import cache
from django.db.models import Avg, Count, Q, QuerySet, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
CACHE_PREFIX = "admin_dashboard_summary:v1"
CHART_DAYS = 30
TOP_EQUIPMENT = 8
RECENT_BOOKINGS = 8
ATTEMPT_DAYS = 7
RATING_DAYS = 90

SCOPE_INSTITUTE = "institute"
SCOPE_DEPARTMENT = "department"

OPERATIONAL_STATUSES = ("ACTIVE",)
MAINTENANCE_STATUSES = ("MAINTENANCE", "REPAIR", "INACTIVE")


class _Scope:
    """Institute (Main Administrator) or one internal department (Department Administrator)."""

    def __init__(self, user):
        self.user = user
        self.is_institute = getattr(user, "user_type", None) == UserType.ADMIN
        self.department_id = None if self.is_institute else getattr(user, "department_id", None)

    @property
    def cache_key(self) -> str:
        return f"{CACHE_PREFIX}:{SCOPE_INSTITUTE}" if self.is_institute else f"{CACHE_PREFIX}:dept:{self.department_id or 0}"

    def by_department(self, qs: QuerySet, field: str) -> QuerySet:
        """Restrict ``qs`` to the department through ``field`` (an internal-department id lookup)."""
        if self.is_institute:
            return qs
        if not self.department_id:
            return qs.none()
        return qs.filter(**{field: self.department_id})


def can_view_admin_dashboard(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "user_type", None) in (UserType.ADMIN, UserType.DEPT_ADMIN)
    )


def _money(value) -> float:
    return round(float(value or 0), 2)


def _local_midnight(day) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def _safely(payload: dict[str, Any], key: str, default: Any, fn: Callable[[], Any]) -> None:
    try:
        payload[key] = fn()
    except Exception:
        logger.exception("admin dashboard summary: %s failed", key)
        payload[key] = default


def _booking_figures(bookings: QuerySet, now: datetime) -> dict[str, Any]:
    from .booking_report_metrics import CHARGED_STATUSES, REFUNDED_STATUSES

    today = timezone.localdate(now)
    today_start = _local_midnight(today)
    week_start = _local_midnight(today - timedelta(days=today.weekday()))
    month_start = _local_midnight(today.replace(day=1))
    last_month_start = _local_midnight((today.replace(day=1) - timedelta(days=1)).replace(day=1))
    charged = Q(status__in=CHARGED_STATUSES)
    this_month = Q(created_at__gte=month_start)
    last_month = Q(created_at__gte=last_month_start, created_at__lt=month_start)

    row = (
        bookings.order_by()
        .filter(created_at__gte=min(last_month_start, week_start))
        .aggregate(
            created_today=Count("pk", filter=Q(created_at__gte=today_start)),
            created_this_week=Count("pk", filter=Q(created_at__gte=week_start)),
            created_last_7_days=Count("pk", filter=Q(created_at__gte=now - timedelta(days=7))),
            charged_this_month=Sum("total_charge", filter=this_month & charged),
            charged_bookings_this_month=Count("pk", filter=this_month & charged),
            refunded_this_month=Sum("total_charge", filter=this_month & Q(status__in=REFUNDED_STATUSES)),
            charged_last_month=Sum("total_charge", filter=last_month & charged),
            disrupted=Count("pk", filter=Q(status="DISRUPTION_PENDING")),
        )
    )
    return {
        "created_today": row["created_today"] or 0,
        "created_this_week": row["created_this_week"] or 0,
        "created_last_7_days": row["created_last_7_days"] or 0,
        "revenue": {
            "month": today.strftime("%Y-%m"),
            "charged_this_month": _money(row["charged_this_month"]),
            "charged_bookings_this_month": row["charged_bookings_this_month"] or 0,
            "refunded_this_month": _money(row["refunded_this_month"]),
            "charged_last_month": _money(row["charged_last_month"]),
        },
    }


def _sessions(bookings: QuerySet, now: datetime) -> dict[str, int]:
    """Charged bookings with at least one slot today / in the next 7 days (today included)."""
    from .booking_report_metrics import CHARGED_STATUSES
    from .models import DailySlot

    today = timezone.localdate(now)
    active = bookings.order_by().filter(status__in=CHARGED_STATUSES).values("pk")
    slots = DailySlot.objects.filter(booking_id__in=active)
    return {
        "sessions_today": slots.filter(date=today).values("booking_id").distinct().count(),
        "sessions_next_7_days": slots.filter(date__gte=today, date__lt=today + timedelta(days=7))
        .values("booking_id")
        .distinct()
        .count(),
    }


def _bookings_per_day(bookings: QuerySet, now: datetime) -> list[dict[str, Any]]:
    from .booking_report_metrics import CHARGED_STATUSES

    today = timezone.localdate(now)
    first = today - timedelta(days=CHART_DAYS - 1)
    rows = (
        bookings.order_by()
        .filter(created_at__gte=_local_midnight(first))
        .annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values("day")
        .annotate(n=Count("pk"), charged=Sum("total_charge", filter=Q(status__in=CHARGED_STATUSES)))
    )
    by_day = {r["day"]: r for r in rows}
    series = []
    for i in range(CHART_DAYS):
        day = first + timedelta(days=i)
        r = by_day.get(day) or {}
        series.append({"date": day.isoformat(), "count": r.get("n") or 0, "charged": _money(r.get("charged"))})
    return series


def _top_equipment(bookings: QuerySet, now: datetime) -> list[dict[str, Any]]:
    """Equipment with the most booked hours on slots in the last ``CHART_DAYS`` days."""
    from .booking_report_metrics import CHARGED_STATUSES
    from .models import DailySlot

    today = timezone.localdate(now)
    slot_bookings = DailySlot.objects.filter(
        date__gte=today - timedelta(days=CHART_DAYS - 1), date__lte=today, booking_id__isnull=False
    ).values("booking_id")
    rows = (
        bookings.order_by()
        .filter(status__in=CHARGED_STATUSES, pk__in=slot_bookings)
        .values("equipment_id", "equipment__name", "equipment__code")
        .annotate(n=Count("pk"), minutes=Sum("total_time_minutes"), charged=Sum("total_charge"))
        .order_by("-minutes", "-n", "equipment__name")[:TOP_EQUIPMENT]
    )
    return [
        {
            "equipment_id": r["equipment_id"],
            "name": r["equipment__name"],
            "code": r["equipment__code"],
            "bookings": r["n"] or 0,
            "hours": round(float(r["minutes"] or 0) / 60.0, 1),
            "charged": _money(r["charged"]),
        }
        for r in rows
    ]


def _recent_bookings(bookings: QuerySet) -> list[dict[str, Any]]:
    from iic_booking.communication.in_app import person_label

    rows = bookings.select_related("equipment", "user").order_by("-created_at")[:RECENT_BOOKINGS]
    return [
        {
            "booking_id": b.booking_id,
            "reference": b.virtual_booking_id or str(b.booking_id),
            "equipment_name": b.equipment.name,
            "user_name": person_label(b.user),
            "status": b.status,
            "status_display": b.get_status_display(),
            "total_charge": _money(b.total_charge),
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in rows
    ]


def _equipment_status(scope: _Scope) -> dict[str, int]:
    from .models import Equipment

    counts = {"total": 0, "operational": 0, "under_maintenance": 0, "disposed": 0, "other": 0}
    rows = scope.by_department(Equipment.objects.all(), "internal_department_id").order_by().values("status").annotate(
        n=Count("pk")
    )
    for r in rows:
        code = (r["status"] or "").upper()
        n = r["n"] or 0
        if code in OPERATIONAL_STATUSES:
            counts["operational"] += n
        elif code in MAINTENANCE_STATUSES:
            counts["under_maintenance"] += n
        elif code == "DISPOSED":
            counts["disposed"] += n
        else:
            counts["other"] += n
        if code != "DISPOSED":
            counts["total"] += n
    return counts


def _users(scope: _Scope, now: datetime) -> dict[str, int]:
    from django.contrib.auth import get_user_model

    qs = get_user_model().objects.filter(is_test_account=False)
    qs = scope.by_department(qs, "department_id")
    row = qs.order_by().aggregate(
        active=Count("pk", filter=Q(is_active=True)),
        new_last_7_days=Count("pk", filter=Q(date_joined__gte=now - timedelta(days=7))),
        new_last_30_days=Count("pk", filter=Q(date_joined__gte=now - timedelta(days=30))),
    )
    return {k: v or 0 for k, v in row.items()}


def _waitlist(scope: _Scope) -> dict[str, int]:
    from .models import WaitlistEntry

    qs = scope.by_department(WaitlistEntry.objects.filter(status="ACTIVE"), "equipment__internal_department_id")
    return {"active": qs.count()}


def _booking_attempts(scope: _Scope, now: datetime) -> dict[str, Any]:
    from .models import BookingAttemptLog, BookingAttemptOutcome

    qs = scope.by_department(
        BookingAttemptLog.objects.filter(requested_at__gte=now - timedelta(days=ATTEMPT_DAYS)),
        "equipment__internal_department_id",
    ).order_by()
    row = qs.aggregate(total=Count("pk"), failed=Count("pk", filter=Q(outcome=BookingAttemptOutcome.FAILED)))
    reasons = (
        qs.filter(outcome=BookingAttemptOutcome.FAILED)
        .exclude(failure_reason="")
        .values("failure_reason")
        .annotate(n=Count("pk"))
        .order_by("-n")[:3]
    )
    return {
        "days": ATTEMPT_DAYS,
        "total": row["total"] or 0,
        "failed": row["failed"] or 0,
        "top_failure_reasons": [{"reason": r["failure_reason"][:160], "count": r["n"]} for r in reasons],
    }


def _ratings(scope: _Scope, bookings: QuerySet, now: datetime) -> dict[str, Any]:
    rated = (
        bookings.order_by()
        .filter(rating__isnull=False, rating_removed=False, rated_at__gte=now - timedelta(days=RATING_DAYS))
        .aggregate(avg=Avg("rating"), n=Count("pk"))
    )
    payload: dict[str, Any] = {
        "days": RATING_DAYS,
        "booking_average": round(float(rated["avg"]), 2) if rated["avg"] is not None else None,
        "booking_count": rated["n"] or 0,
        "portal_average": None,
        "portal_count": 0,
    }
    if scope.is_institute:
        from iic_booking.support.models import PortalFeedback

        portal = PortalFeedback.objects.order_by().aggregate(avg=Avg("overall_rating"), n=Count("pk"))
        payload["portal_average"] = round(float(portal["avg"]), 2) if portal["avg"] is not None else None
        payload["portal_count"] = portal["n"] or 0
    return payload


def _attention(scope: _Scope, disrupted: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, count: int, link: str, description: str) -> None:
        if count:
            items.append({"key": key, "label": label, "count": count, "link": link, "description": description})

    if scope.is_institute:
        from iic_booking.support.models import Ticket

        add(
            "open_support_tickets",
            "Open support tickets",
            Ticket.objects.filter(status__in=(Ticket.TicketStatus.OPEN, Ticket.TicketStatus.IN_PROGRESS)).count(),
            "/admin-settings/support",
            "Support tickets that are open or in progress.",
        )
    else:
        from iic_booking.users.models.wallet import WalletRechargeRequest, WalletRechargeRequestStatus

        add(
            "wallet_recharge_requests",
            "Wallet recharge requests",
            scope.by_department(
                WalletRechargeRequest.objects.filter(status=WalletRechargeRequestStatus.PENDING, user_otp_verified=True),
                "department_id",
            ).count(),
            "/admin-settings/wallet-recharge-requests",
            "Verified recharge requests for your department's sub-wallets are awaiting approval.",
        )
    add(
        "bookings_disrupted",
        "Disrupted bookings",
        disrupted,
        "/booking-management",
        "Bookings disrupted by the lab that are waiting for the user to reschedule or cancel.",
    )
    return items


def _department(scope: _Scope) -> dict[str, Any] | None:
    if scope.is_institute or not scope.department_id:
        return None
    from iic_booking.users.models.department import Department

    dept = Department.objects.filter(pk=scope.department_id).values("id", "name", "code").first()
    return dept


def build_admin_dashboard_summary(user) -> dict[str, Any]:
    from iic_booking.platform_compat.manifest import build_version_payload

    from .booking_report_metrics import report_bookings_scope

    scope = _Scope(user)
    now = timezone.now()
    bookings, _label = report_bookings_scope(user)

    payload: dict[str, Any] = {
        "scope": SCOPE_INSTITUTE if scope.is_institute else SCOPE_DEPARTMENT,
        "generated_at": now.isoformat(),
        "cache_seconds": CACHE_SECONDS,
    }
    _safely(payload, "department", None, lambda: _department(scope))

    figures: dict[str, Any] = {}
    _safely(figures, "figures", {}, lambda: _booking_figures(bookings, now))
    booking_figures = figures["figures"] or {}
    sessions: dict[str, Any] = {}
    _safely(sessions, "sessions", {}, lambda: _sessions(bookings, now))
    payload["bookings"] = {
        "created_today": booking_figures.get("created_today", 0),
        "created_this_week": booking_figures.get("created_this_week", 0),
        "created_last_7_days": booking_figures.get("created_last_7_days", 0),
        "sessions_today": (sessions["sessions"] or {}).get("sessions_today", 0),
        "sessions_next_7_days": (sessions["sessions"] or {}).get("sessions_next_7_days", 0),
    }
    payload["revenue"] = booking_figures.get("revenue")

    _safely(payload, "equipment", None, lambda: _equipment_status(scope))
    _safely(payload, "users", None, lambda: _users(scope, now))
    _safely(payload, "waitlist", None, lambda: _waitlist(scope))
    _safely(payload, "booking_attempts", None, lambda: _booking_attempts(scope, now))
    _safely(payload, "ratings", None, lambda: _ratings(scope, bookings, now))
    disrupted = 0
    try:
        disrupted = bookings.order_by().filter(status="DISRUPTION_PENDING").count()
    except Exception:
        logger.exception("admin dashboard summary: disrupted count failed")
    _safely(payload, "attention", [], lambda: _attention(scope, disrupted))
    _safely(payload, "bookings_per_day", [], lambda: _bookings_per_day(bookings, now))
    _safely(payload, "top_equipment", [], lambda: _top_equipment(bookings, now))
    _safely(payload, "recent_bookings", [], lambda: _recent_bookings(bookings))

    def system():
        version = build_version_payload()
        return {
            "backend_version": version.get("backend_version") or version.get("portal_version") or "",
            "build_date": version.get("build_date") or "",
            "server_time": now.isoformat(),
        }

    _safely(payload, "system", None, system)
    return payload


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_dashboard_summary(request):
    """GET /api/admin/dashboard-summary/ — Main Administrator (institute) or Department Administrator (own department).

    ``?refresh=1`` skips the shared cache (still re-cached for everyone with the same scope).
    """
    user = request.user
    if not can_view_admin_dashboard(user):
        return Response(
            {"error": "Only the Main Administrator or a Department Administrator can view this overview."},
            status=status.HTTP_403_FORBIDDEN,
        )
    scope = _Scope(user)
    refresh = str(request.query_params.get("refresh") or "").strip().lower() in ("1", "true", "yes")
    payload = None if refresh else cache.get(scope.cache_key)
    if payload is None:
        payload = build_admin_dashboard_summary(user)
        cache.set(scope.cache_key, payload, CACHE_SECONDS)
    return Response(payload, status=status.HTTP_200_OK)
