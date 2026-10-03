"""
Compact "Today" summary for Officers In Charge and Lab Operators (IIC Booking app home screen).

Everything is scoped to the equipment the user manages (OIC, incl. active temporary OIC delegations)
or operates (Lab Operator, honouring operator coverage windows) - the same scope as the Lab Operator
dashboard. Cached per user for a short time; ``?refresh=1`` bypasses the cache.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import OuterRef, Prefetch, Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.display import get_user_display_name
from iic_booking.users.models.user_type import UserType

from .models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingSampleTrace,
    BookingStatus,
    DailySlot,
    Equipment,
    EquipmentStatus,
    SampleTraceStatus,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    WaitlistEntry,
)

CACHE_SECONDS = 30
MAX_ROWS_PER_DAY = 100
MESSAGE_LOOKBACK_DAYS = 30
STAFF_TYPES = (UserType.OPERATOR, UserType.MANAGER)

HIDDEN_STATUSES = [BookingStatus.CANCELLED, BookingStatus.REFUNDED, BookingStatus.WAITLISTED]
AWAITING_RECEIPT_STAGES = [
    SampleTraceStatus.SAMPLE_SENT,
    SampleTraceStatus.HELD_AT_OFFICE,
    SampleTraceStatus.FORWARDED_TO_LAB,
]


def cache_key(user_id: int, day) -> str:
    return f"staff_app_today:v1:{user_id}:{day.isoformat()}"


def _latest_stage_subquery():
    return Subquery(
        BookingSampleTrace.objects.filter(booking_id=OuterRef("pk")).order_by("-created_at", "-id").values("status")[:1]
    )


def _booking_ref(b) -> str:
    return (b.virtual_booking_id or "").strip() or f"{b.equipment.code}-#{b.booking_id}"


def _day_rows(base, day, sample_index) -> list[dict]:
    from .booking_sample_summary import booking_sample_summary

    stage_labels = dict(SampleTraceStatus.choices)
    qs = (
        base.exclude(status__in=HIDDEN_STATUSES)
        .filter(daily_slots__date=day)
        .distinct()
        .annotate(_stage=_latest_stage_subquery())
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch(
                "daily_slots",
                queryset=DailySlot.objects.filter(date=day).order_by("start_datetime"),
                to_attr="_day_slots",
            )
        )
    )
    rows = []
    for b in qs[:MAX_ROWS_PER_DAY]:
        slots = getattr(b, "_day_slots", [])
        first = slots[0] if slots else None
        last = slots[-1] if slots else None
        stage = b._stage or ""
        rows.append(
            {
                "booking_id": b.booking_id,
                "booking_ref": _booking_ref(b),
                "equipment_id": b.equipment_id,
                "equipment_code": b.equipment.code,
                "equipment_name": b.equipment.name or "",
                "user_name": get_user_display_name(b.user, fallback_to_email=False),
                "is_test": bool(getattr(b.user, "is_test_account", False)),
                "status": b.status,
                "status_display": b.get_status_display(),
                "start_time": first.start_datetime.isoformat() if first and first.start_datetime else None,
                "end_time": last.end_datetime.isoformat() if last and last.end_datetime else None,
                "sample_stage": stage,
                "sample_stage_display": str(stage_labels.get(stage, "")) if stage else "",
                "sample_summary": booking_sample_summary(b, sample_index),
            }
        )
    rows.sort(key=lambda r: (r["start_time"] or "", r["booking_id"]))
    return rows


def _awaiting_reply_booking_ids(equipment_ids, now) -> list[int]:
    """Bookings whose latest Message-the-lab entry (last 30 days) is from the user, i.e. not yet answered."""
    from .booking_lab_messages import KIND_USER, LAB_MESSAGE_KEY

    events = (
        BookingEvent.objects.filter(
            booking__equipment_id__in=equipment_ids,
            event_type=BookingEventType.COMMENT,
            created_at__gte=now - timedelta(days=MESSAGE_LOOKBACK_DAYS),
            metadata__has_key=LAB_MESSAGE_KEY,
        )
        .order_by("booking_id", "created_at", "event_id")
        .values_list("booking_id", "metadata")
    )
    latest: dict[int, str] = {}
    for booking_id, md in events:
        latest[booking_id] = (md or {}).get(LAB_MESSAGE_KEY) or ""
    return sorted(bid for bid, kind in latest.items() if kind == KIND_USER)


def build_staff_today(user) -> dict:
    from .api_views import _get_equipment_ids_for_log_access
    from .booking_sample_summary import SampleCountFieldIndex

    now = timezone.now()
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    is_oic = user.user_type == UserType.MANAGER
    equipment_ids = sorted(int(x) for x in (_get_equipment_ids_for_log_access(user) or []))

    status_labels = dict(EquipmentStatus.choices)
    equipment = [
        {
            "equipment_id": int(r["equipment_id"]),
            "code": r["code"] or "",
            "name": r["name"] or "",
            "status": r["status"] or "",
            "status_display": str(status_labels.get(r["status"], r["status"] or "")),
        }
        for r in Equipment.objects.filter(equipment_id__in=equipment_ids)
        .values("equipment_id", "code", "name", "status")
        .order_by("code")
    ]

    from iic_booking.support.models import Ticket

    tickets_open = Ticket.objects.filter(
        assigned_to=user, status__in=[Ticket.TicketStatus.OPEN, Ticket.TicketStatus.IN_PROGRESS]
    ).count()

    payload = {
        "role": user.user_type,
        "today": today.isoformat(),
        "tomorrow": tomorrow.isoformat(),
        "generated_at": now.isoformat(),
        "equipment": equipment,
        "days": [],
        "counts": {
            "samples_awaiting_receipt": 0,
            "user_messages_awaiting_reply": 0,
            "urgent_requests_pending": 0 if is_oic else None,
            "waitlist_active": 0 if is_oic else None,
            "tickets_assigned_open": tickets_open,
            "results_overdue": 0,
        },
        "message_booking_ids": [],
        "results_overdue_booking_ids": [],
    }
    if not equipment_ids:
        payload["days"] = [
            {"date": today.isoformat(), "label": "Today", "bookings": []},
            {"date": tomorrow.isoformat(), "label": "Tomorrow", "bookings": []},
        ]
        return payload

    base = Booking.objects.filter(equipment_id__in=equipment_ids)
    sample_index = SampleCountFieldIndex()
    sample_index.preload(equipment_ids)
    payload["days"] = [
        {"date": today.isoformat(), "label": "Today", "bookings": _day_rows(base, today, sample_index)},
        {"date": tomorrow.isoformat(), "label": "Tomorrow", "bookings": _day_rows(base, tomorrow, sample_index)},
    ]

    counts = payload["counts"]
    counts["samples_awaiting_receipt"] = (
        base.filter(status=BookingStatus.BOOKED)
        .annotate(_stage=_latest_stage_subquery())
        .filter(_stage__in=AWAITING_RECEIPT_STAGES)
        .count()
    )
    message_ids = _awaiting_reply_booking_ids(equipment_ids, now)
    counts["user_messages_awaiting_reply"] = len(message_ids)
    payload["message_booking_ids"] = message_ids[:50]
    from .results_deadline import overdue_booking_ids

    overdue_ids = overdue_booking_ids(base, now)
    counts["results_overdue"] = len(overdue_ids)
    payload["results_overdue_booking_ids"] = overdue_ids[:50]
    if is_oic:
        counts["urgent_requests_pending"] = UrgentBookingRequest.objects.filter(
            equipment_id__in=equipment_ids, status=UrgentBookingRequestStatus.PENDING
        ).count()
        counts["waitlist_active"] = WaitlistEntry.objects.filter(
            equipment_id__in=equipment_ids, status="ACTIVE"
        ).count()
    return payload


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def staff_app_today(request):
    """Today and tomorrow on my equipment plus pending counts (Officer In Charge / Lab Operator only)."""
    user = request.user
    if getattr(user, "user_type", None) not in STAFF_TYPES:
        return Response(
            {"error": "Only Officers In Charge and Lab Operators have a Today summary."},
            status=status.HTTP_403_FORBIDDEN,
        )
    key = cache_key(user.pk, timezone.localdate())
    refresh = str(request.query_params.get("refresh") or "") in {"1", "true", "yes"}
    data = None if refresh else cache.get(key)
    if data is None:
        data = build_staff_today(user)
        cache.set(key, data, CACHE_SECONDS)
    response = Response(data)
    response["Cache-Control"] = "private, no-store"
    return response
