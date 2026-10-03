"""Bookings whose slot time is over but which staff have not marked Completed.

Officers in charge (including temporary OIC) and Lab in-charges (Lab Operators, respecting
operator coverage) see them in the login popup and on the dashboard, and get one digest email
a day (``equipment.send_booking_completion_reminders``).
"""

from __future__ import annotations

import html
import logging
from typing import Any, Iterable, Optional

from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

DASHBOARD_ANCHOR = "bookings-awaiting-completion"
DASHBOARD_PATH = f"/dashboard#{DASHBOARD_ANCHOR}"
EMAIL_TEMPLATE_CODE = "booking_completion_overdue_staff_email"
MAX_EMAIL_ROWS = 100


def awaiting_completion_statuses():
    from .models import BookingStatus

    # Only these can be completed (complete_booking accepts PENDING / BOOKED).
    return (BookingStatus.BOOKED, BookingStatus.PENDING)


def bookings_awaiting_completion(equipment_ids: Optional[Iterable[int]] = None, now=None):
    """Ended (latest slot end in the past), not yet completed; ``equipment_ids=None`` means all equipment."""
    from .models import Booking

    now = now or timezone.now()
    qs = Booking.objects.filter(status__in=awaiting_completion_statuses())
    if equipment_ids is not None:
        ids = list(equipment_ids)
        if not ids:
            return qs.none()
        qs = qs.filter(equipment_id__in=ids)
    return (
        qs.annotate(last_slot_end=Max("daily_slots__end_datetime"))
        .filter(last_slot_end__isnull=False, last_slot_end__lte=now)
        .select_related("equipment", "user")
        .order_by("last_slot_end", "booking_id")
    )


def awaiting_completion_equipment_ids(user) -> list[int]:
    """Equipment the user is responsible for as OIC / temporary OIC or as Lab in-charge."""
    from .reports import get_equipment_ids_managed_by_oic

    user_type = getattr(user, "user_type", None)
    if not getattr(user, "id", None) or user_type not in (UserType.MANAGER, UserType.OPERATOR):
        return []
    ids = set(get_equipment_ids_managed_by_oic(user.id))
    if user_type == UserType.OPERATOR:
        from .api_views import _get_equipment_ids_for_log_access

        ids |= set(_get_equipment_ids_for_log_access(user) or [])
    return sorted(ids)


def bookings_awaiting_completion_for_user(user, now=None):
    return bookings_awaiting_completion(awaiting_completion_equipment_ids(user), now=now)


def _booking_ref(booking) -> str:
    from iic_booking.communication.utils import booking_display_id_for_email

    return booking_display_id_for_email(booking) or str(booking.booking_id)


def _person(user) -> str:
    from iic_booking.communication.in_app import person_label

    return person_label(user)


def overdue_label(last_slot_end, now=None) -> str:
    delta = (now or timezone.now()) - last_slot_end
    hours = max(int(delta.total_seconds() // 3600), 0)
    days, rem = divmod(hours, 24)
    if days and rem:
        return f"{days} day{'s' if days != 1 else ''} {rem} h"
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    if hours:
        return f"{hours} h"
    return "less than 1 h"


def _ended_display(last_slot_end) -> str:
    return timezone.localtime(last_slot_end).strftime("%d %b %Y, %I:%M %p")


def booking_management_path(booking) -> str:
    return f"/booking-management?expand={booking.booking_id}"


def results_due(booking, now=None, calendar=None) -> tuple[str, bool]:
    """("Mon 06 Oct 2026" or "", overdue?) from the equipment's results deadline."""
    from .results_deadline import booking_results_deadline, due_display, is_results_overdue

    try:
        deadline = booking_results_deadline(booking, calendar)
        if deadline is None:
            return "", False
        return due_display(deadline), is_results_overdue(booking, deadline, now)
    except Exception:
        logger.exception("results deadline failed booking_id=%s", getattr(booking, "booking_id", None))
        return "", False


def serialize_awaiting_booking(booking, now=None, calendar=None) -> dict[str, Any]:
    equipment = booking.equipment
    due, results_overdue = results_due(booking, now, calendar)
    return {
        "results_due_display": due,
        "results_overdue": results_overdue,
        "booking_id": booking.booking_id,
        "booking_ref": _booking_ref(booking),
        "equipment_id": equipment.equipment_id,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "user_name": _person(booking.user),
        "status": booking.status,
        "ended_at": booking.last_slot_end.isoformat(),
        "ended_display": _ended_display(booking.last_slot_end),
        "overdue": overdue_label(booking.last_slot_end, now),
        "link": booking_management_path(booking),
    }


def pending_action_detail(booking) -> str:
    return (
        f"{_booking_ref(booking)} — {booking.equipment.name} — {_person(booking.user)} — "
        f"ended {_ended_display(booking.last_slot_end)}"
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bookings_awaiting_completion_view(request):
    from .results_deadline import WorkingCalendar

    now = timezone.now()
    calendar = WorkingCalendar()
    rows = [
        serialize_awaiting_booking(b, now, calendar)
        for b in bookings_awaiting_completion_for_user(request.user, now)
    ]
    return Response({"count": len(rows), "bookings": rows}, status=status.HTTP_200_OK)


def _candidate_recipients(equipment_ids: list[int]) -> list:
    """OIC, active temporary OIC, Lab Operators and acting coverage operators of these equipment."""
    from django.contrib.auth import get_user_model

    from .models import EquipmentManager, EquipmentOperator, EquipmentOperatorCoverage, EquipmentTemporaryOIC

    now = timezone.now()
    user_ids = set(EquipmentManager.objects.filter(equipment_id__in=equipment_ids).values_list("manager_id", flat=True))
    user_ids |= set(
        EquipmentTemporaryOIC.objects.filter(equipment_id__in=equipment_ids, resume_at__gt=now).values_list(
            "temporary_oic_id", flat=True
        )
    )
    user_ids |= set(
        EquipmentOperator.objects.filter(equipment_id__in=equipment_ids).values_list("operator_id", flat=True)
    )
    user_ids |= set(
        EquipmentOperatorCoverage.objects.filter(
            equipment_id__in=equipment_ids, starts_at__lte=now, ends_at__gte=now
        )
        .filter(Q(ended_early_at__isnull=True) | Q(ended_early_at__gt=now))
        .values_list("acting_operator_id", flat=True)
    )
    user_ids.discard(None)
    return list(get_user_model().objects.filter(id__in=user_ids, is_active=True).order_by("id"))


def _digest_context(user, bookings: list, now) -> dict[str, Any]:
    from iic_booking.communication.email_branding import absolute_http_url
    from iic_booking.communication.utils import get_frontend_absolute_url

    from .results_deadline import WorkingCalendar

    calendar = WorkingCalendar()
    shown = bookings[:MAX_EMAIL_ROWS]
    more = len(bookings) - len(shown)
    cell = "padding:6px 8px;border-bottom:1px solid #e2e8f0;font-size:13px;text-align:left;vertical-align:top;"
    head = "".join(
        f"<th style=\"{cell}background:#f1f5f9;font-weight:700;\">{label}</th>"
        for label in ("Booking ID", "Equipment", "User", "Booking ended", "Overdue by", "Results due")
    )
    rows_html = []
    rows_text = []
    for b in shown:
        due, results_overdue = results_due(b, now, calendar)
        values = (
            _booking_ref(b),
            b.equipment.name,
            _person(b.user),
            _ended_display(b.last_slot_end),
            overdue_label(b.last_slot_end, now),
            (f"{due} (results overdue)" if results_overdue else due) or "—",
        )
        link = absolute_http_url(get_frontend_absolute_url(booking_management_path(b)))
        first = f"<a href=\"{html.escape(link, quote=True)}\">{html.escape(values[0])}</a>" if link else html.escape(values[0])
        rows_html.append(
            "<tr>"
            + f"<td style=\"{cell}\">{first}</td>"
            + "".join(f"<td style=\"{cell}\">{html.escape(v)}</td>" for v in values[1:])
            + "</tr>"
        )
        rows_text.append(
            f"- {values[0]} | {values[1]} | {values[2]} | ended {values[3]} | overdue by {values[4]} | results due {values[5]}"
        )
    if more > 0:
        rows_text.append(f"... and {more} more (see your dashboard)")
    bookings_html = (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;margin:8px 0 4px 0;font-family:Arial,Helvetica,sans-serif;">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )
    if more > 0:
        bookings_html += (
            '<p style="margin:4px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#475569;">'
            f"and {more} more — see your dashboard.</p>"
        )
    return {
        "user_name": _person(user),
        "user_email": getattr(user, "email", "") or "",
        "booking_count": str(len(bookings)),
        "bookings_html": bookings_html,
        "bookings_text": "\n".join(rows_text),
        "link": absolute_http_url(get_frontend_absolute_url(DASHBOARD_PATH)),
    }


def send_booking_completion_reminders(now=None) -> int:
    """One digest per OIC / Lab in-charge listing every ended, not-completed booking they are responsible for."""
    from iic_booking.communication.service import CommunicationService

    now = now or timezone.now()
    equipment_ids = sorted(set(bookings_awaiting_completion(now=now).values_list("equipment_id", flat=True)))
    if not equipment_ids:
        return 0
    sent = 0
    for user in _candidate_recipients(equipment_ids):
        if not (getattr(user, "email", "") or "").strip():
            continue
        try:
            bookings = list(bookings_awaiting_completion_for_user(user, now))
            if not bookings:
                continue
            CommunicationService.send_email(
                recipient=user,
                template=EMAIL_TEMPLATE_CODE,
                template_context=_digest_context(user, bookings, now),
                metadata={
                    "kind": "booking_completion_overdue_digest",
                    "booking_ids": [b.booking_id for b in bookings[:MAX_EMAIL_ROWS]],
                    "booking_count": len(bookings),
                },
            )
            sent += 1
        except Exception:
            logger.exception("booking completion reminder failed user_id=%s", user.id)
    return sent
