"""Bookings whose slot time is over and whose sample the lab has received, but which staff have not marked Completed.

A booking past its slot whose sample was never received is not awaiting completion: it follows the
Booking Not Utilized / Operator Unavailable rules instead. Walk-in equipment never records receipt, so
its bookings count as received at the slot.

Lab Operators see every such booking on the dashboard: "Results due by <time>" until the equipment's results
overdue time (``results_overdue``), then "Overdue by" counted from that time. The login popup item and the
daily 9:00 AM digest (``equipment.send_booking_completion_reminders``) to Officers in charge (including
temporary OIC) and Lab in-charges (respecting operator coverage) list only the overdue ones. "Results
deadline" is the equipment's separate results deadline (``results_deadline``).
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
    """Ended (latest slot end in the past), sample received, not yet completed; ``equipment_ids=None`` means all equipment."""
    from django.db.models.functions import Coalesce, Greatest

    from .models import Booking
    from .results_deadline import annotate_sample_receipt, sample_received_q

    now = now or timezone.now()
    qs = Booking.objects.filter(status__in=awaiting_completion_statuses())
    if equipment_ids is not None:
        ids = list(equipment_ids)
        if not ids:
            return qs.none()
        qs = qs.filter(equipment_id__in=ids)
    return (
        annotate_sample_receipt(qs)
        .annotate(last_slot_end=Max("daily_slots__end_datetime"))
        .filter(last_slot_end__isnull=False, last_slot_end__lte=now)
        .filter(sample_received_q())
        .annotate(completion_anchor=Greatest("last_slot_end", Coalesce("_sample_received_at", "last_slot_end")))
        .select_related("equipment", "user")
        .order_by("completion_anchor", "booking_id")
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


def with_results_due(bookings) -> list[tuple]:
    """[(booking, ResultsDue or None)] in results-due order (earliest first), loaded in batch."""
    from .results_overdue import booking_results_due, preload

    rows = list(bookings)
    preload(rows)
    pairs = [(b, booking_results_due(b)) for b in rows]
    pairs.sort(key=lambda p: (p[1] is None, p[1].due_at.timestamp() if p[1] else 0, p[0].booking_id))
    return pairs


def overdue_awaiting_completion(equipment_ids: Optional[Iterable[int]] = None, now=None) -> list[tuple]:
    """[(booking, ResultsDue)] awaiting completion whose results overdue time has been reached."""
    from .results_overdue import is_results_overdue

    now = now or timezone.now()
    return [
        (b, due)
        for b, due in with_results_due(bookings_awaiting_completion(equipment_ids, now=now))
        if is_results_overdue(b, due, now)
    ]


def overdue_awaiting_completion_for_user(user, now=None) -> list[tuple]:
    return overdue_awaiting_completion(awaiting_completion_equipment_ids(user), now=now)


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
    from iic_booking.communication.email_branding import strftime_slot_end

    return strftime_slot_end(timezone.localtime(last_slot_end), "%d %b %Y, %I:%M %p")


def completion_anchor(booking):
    """(anchor, receipt): the later of the slot end and the sample receipt plus the booked time (``results_overdue``)."""
    from .results_deadline import booking_sample_receipt
    from .results_overdue import booking_booked_duration, results_anchor

    receipt = booking_sample_receipt(booking)
    return results_anchor(booking.last_slot_end, receipt.received_at, booking_booked_duration(booking)), receipt


def received_display(receipt) -> str:
    from .results_deadline import RECEIPT_SAMPLE_ACCEPTED, RECEIPT_WALK_IN

    if receipt.source == RECEIPT_SAMPLE_ACCEPTED and receipt.received_at:
        return _ended_display(receipt.received_at)
    if receipt.source == RECEIPT_WALK_IN:
        return "At the slot"
    return "Recorded (time not available)"


def booking_management_path(booking) -> str:
    return f"/booking-management?expand={booking.booking_id}"


def results_due(booking, now=None, calendar=None) -> tuple[str, bool]:
    """("Mon 06 Oct 2026" or "", passed?) from the equipment's results deadline (not the results overdue time)."""
    from .results_deadline import booking_results_deadline, due_display, is_results_overdue

    try:
        deadline = booking_results_deadline(booking, calendar)
        if deadline is None:
            return "", False
        return due_display(deadline), is_results_overdue(booking, deadline, now)
    except Exception:
        logger.exception("results deadline failed booking_id=%s", getattr(booking, "booking_id", None))
        return "", False


def serialize_awaiting_booking(booking, now=None, calendar=None, results=None) -> dict[str, Any]:
    """``results`` is the booking's ``results_overdue.ResultsDue`` (computed when not given)."""
    from .booking_list_status import compute_list_status
    from .results_overdue import booking_results_due, due_display, is_results_overdue, waiting_for_user

    now = now or timezone.now()
    equipment = booking.equipment
    due, deadline_passed = results_due(booking, now, calendar)
    anchor, receipt = completion_anchor(booking)
    results = results or booking_results_due(booking)
    overdue = is_results_overdue(booking, results, now)
    return {
        "sample_received_at": receipt.received_at.isoformat() if receipt.received_at else None,
        "sample_received_display": received_display(receipt),
        "receipt_source": receipt.source,
        "anchor_at": anchor.isoformat(),
        "results_due_at": results.due_at.isoformat() if results else None,
        "results_due_at_display": due_display(results.due_at) if results else "",
        "overdue_after_hours": results.hours if results else None,
        "is_overdue": overdue,
        "waiting_for_user": waiting_for_user(booking),
        "results_due_display": due,
        "results_overdue": deadline_passed,
        "booking_id": booking.booking_id,
        "booking_ref": _booking_ref(booking),
        "equipment_id": equipment.equipment_id,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "user_name": _person(booking.user),
        "status": booking.status,
        "list_status": compute_list_status(booking, staff_view=True, now=now),
        "ended_at": booking.last_slot_end.isoformat(),
        "ended_display": _ended_display(booking.last_slot_end),
        "overdue": overdue_label(results.due_at, now) if overdue else "",
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
        serialize_awaiting_booking(b, now, calendar, due)
        for b, due in with_results_due(bookings_awaiting_completion_for_user(request.user, now))
    ]
    overdue = sum(1 for r in rows if r["is_overdue"])
    return Response({"count": len(rows), "overdue_count": overdue, "bookings": rows}, status=status.HTTP_200_OK)


def _candidate_recipients(equipment_ids: list[int]) -> list:
    """OIC, active temporary OIC, Lab Operators and acting coverage operators of these equipment."""
    from django.contrib.auth import get_user_model

    from .models import EquipmentManager, EquipmentOperator, EquipmentOperatorCoverage, EquipmentTemporaryOIC

    now = timezone.now()
    user_ids = set(EquipmentManager.objects.filter(equipment_id__in=equipment_ids).values_list("manager_id", flat=True))
    user_ids |= set(
        EquipmentTemporaryOIC.objects.active(now)
        .filter(equipment_id__in=equipment_ids)
        .values_list("temporary_oic_id", flat=True)
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


def _digest_context(user, overdue: list, now) -> dict[str, Any]:
    """``overdue``: [(booking, ResultsDue)] past their results overdue time."""
    from iic_booking.communication.email_branding import absolute_http_url
    from iic_booking.communication.utils import get_frontend_absolute_url

    from .results_deadline import WorkingCalendar
    from .results_overdue import due_display

    calendar = WorkingCalendar()
    shown = overdue[:MAX_EMAIL_ROWS]
    more = len(overdue) - len(shown)
    cell = "padding:6px 8px;border-bottom:1px solid #e2e8f0;font-size:13px;text-align:left;vertical-align:top;"
    head = "".join(
        f"<th style=\"{cell}background:#f1f5f9;font-weight:700;\">{label}</th>"
        for label in (
            "Booking ID", "Equipment", "User", "Booking ended", "Sample received", "Results due by", "Overdue by",
            "Results deadline",
        )
    )
    rows_html = []
    rows_text = []
    for b, results in shown:
        deadline, deadline_passed = results_due(b, now, calendar)
        _anchor, receipt = completion_anchor(b)
        values = (
            _booking_ref(b),
            b.equipment.name,
            _person(b.user),
            _ended_display(b.last_slot_end),
            received_display(receipt),
            due_display(results.due_at),
            overdue_label(results.due_at, now),
            (f"{deadline} (passed)" if deadline_passed else deadline) or "—",
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
            f"- {values[0]} | {values[1]} | {values[2]} | ended {values[3]} | sample received {values[4]} "
            f"| results due by {values[5]} | overdue by {values[6]} | results deadline {values[7]}"
        )
    if more > 0:
        rows_text.append(f"... and {more} more (see your dashboard)")
    note = (
        "Listed once the results are overdue: the hours set for the equipment (24 by default) after the booking "
        "end, or after the sample receipt plus the booked time if that is later."
    )
    rows_text.insert(0, note)
    bookings_html = (
        f'<p style="margin:0 0 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#475569;">{note}</p>'
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
        "booking_count": str(len(overdue)),
        "bookings_html": bookings_html,
        "bookings_text": "\n".join(rows_text),
        "link": absolute_http_url(get_frontend_absolute_url(DASHBOARD_PATH)),
    }


def send_booking_completion_reminders(now=None) -> int:
    """
    One digest per OIC / Lab in-charge listing their awaiting-completion bookings whose results overdue time has
    been reached, so a booking's first reminder is the first daily run at or after that time.
    """
    from iic_booking.communication.service import CommunicationService

    now = now or timezone.now()
    overdue = overdue_awaiting_completion(now=now)
    equipment_ids = sorted({b.equipment_id for b, _due in overdue})
    if not equipment_ids:
        return 0
    sent = 0
    for user in _candidate_recipients(equipment_ids):
        if not (getattr(user, "email", "") or "").strip():
            continue
        try:
            scope = set(awaiting_completion_equipment_ids(user))
            rows = [(b, due) for b, due in overdue if b.equipment_id in scope]
            if not rows:
                continue
            CommunicationService.send_email(
                recipient=user,
                template=EMAIL_TEMPLATE_CODE,
                template_context=_digest_context(user, rows, now),
                metadata={
                    "kind": "booking_completion_overdue_digest",
                    "booking_ids": [b.booking_id for b, _due in rows[:MAX_EMAIL_ROWS]],
                    "booking_count": len(rows),
                },
            )
            sent += 1
        except Exception:
            logger.exception("booking completion reminder failed user_id=%s", user.id)
    return sent
