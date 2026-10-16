"""
Automatic handling of equipment Under Maintenance, day by day.

* Marked Under Maintenance (any path that saves the equipment): an ``EquipmentMaintenancePeriod`` starts. Today's
  remaining Available slots become Under Maintenance, today's bookings from now on are put on hold until they are
  rescheduled automatically (``maintenance_policy.put_booking_on_maintenance_hold``, ``maintenance_reschedule``),
  every user with an upcoming booking on the equipment is emailed once per booking, the results-overdue clock of the
  equipment's past bookings awaiting results is paused, and the OIC / Lab Operators get a summary.
* Every morning at 08:30 IST (``equipment.maintenance_auto_daily_check``): for each equipment still Under Maintenance,
  that day's slots and bookings are handled the same way and the affected users get a specific email.
* Operational again: only the slots this mechanism converted (or that were generated Under Maintenance during the
  period) go back to Available; slots staff set Under Maintenance by hand stay. Paused bookings resume with their
  results deadline extended by the paused time; users told about the maintenance whose bookings remain are told their
  booking stands; the OIC / Lab Operators get a summary.

Emails use ``booking_*`` templates (Booking updates: a locked category, so they are sent at once and never held for
quiet hours or the digest). ``MaintenanceNotification`` makes every email idempotent per booking (and day).
Equipment that was already Under Maintenance before this mechanism has no period and is left alone until it is
adopted on purpose (``manage.py maintenance_auto --adopt``); ``dry_run`` reports what adopting would do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape
from typing import Iterable, Optional

from django.db import transaction
from django.db.models import F, Max, Min
from django.utils import timezone

logger = logging.getLogger(__name__)

TEMPLATE_NOTICE = "booking_equipment_maintenance_hold_notice_email"
TEMPLATE_DAY = "booking_equipment_maintenance_hold_day_email"
TEMPLATE_RESOLVED = "booking_equipment_maintenance_resolved_email"
TEMPLATE_STAFF = "booking_equipment_maintenance_staff_summary_email"
TEMPLATE_RESCHEDULED = "booking_equipment_maintenance_rescheduled_email"
TEMPLATE_CODES = (TEMPLATE_NOTICE, TEMPLATE_DAY, TEMPLATE_RESOLVED, TEMPLATE_STAFF, TEMPLATE_RESCHEDULED)
SUPERSEDED_TEMPLATE_CODES = (
    "booking_equipment_maintenance_notice_email",
    "booking_equipment_maintenance_day_email",
    "booking_equipment_maintenance_staff_email",
)

PAUSE_REASON = "Paused – equipment under maintenance"
STAFF_SLOT_SOURCES = ("CHANGE_SLOT_STATUS", "DASHBOARD_CALENDAR", "ADMIN_SLOT_API", "DJANGO_ADMIN", "BOOKING_DETAILS")
MAX_LIST_ROWS = 25


def _upcoming_statuses():
    from .models import BookingStatus

    return (BookingStatus.BOOKED, BookingStatus.PENDING)


def _results_open_statuses():
    from .results_deadline import _open_statuses

    return _open_statuses()


def _fmt(dt) -> str:
    return timezone.localtime(dt).strftime("%a %d %b %Y, %I:%M %p") if dt else ""


def _fmt_window(start, end) -> str:
    if not start:
        return ""
    s = timezone.localtime(start)
    if not end:
        return _fmt(start)
    e = timezone.localtime(end)
    if s.date() == e.date():
        return f"{s.strftime('%a %d %b %Y, %I:%M %p')} – {e.strftime('%I:%M %p')}"
    return f"{_fmt(start)} – {_fmt(end)}"


def _equipment_label(equipment) -> str:
    return (getattr(equipment, "name", None) or getattr(equipment, "code", None) or "Equipment").strip()


# --- periods ------------------------------------------------------------------------------------------------------


def active_period(equipment):
    from .models import EquipmentMaintenancePeriod

    if equipment is None or not getattr(equipment, "pk", None):
        return None
    return (
        EquipmentMaintenancePeriod.objects.filter(equipment_id=equipment.pk, ended_at__isnull=True)
        .order_by("-started_at", "-id")
        .first()
    )


def _open_equipment_event(equipment):
    from .models import DisruptionEvent, DisruptionScope, DisruptionType

    return (
        DisruptionEvent.objects.filter(
            equipment=equipment,
            disruption_type=DisruptionType.UNDER_MAINTENANCE,
            scope=DisruptionScope.EQUIPMENT,
            ended_at__isnull=True,
            is_deleted=False,
        )
        .order_by("-start_at", "-id")
        .first()
    )


@dataclass
class Outcome:
    period: object = None
    slots_marked: list = field(default_factory=list)
    bookings_on_hold: list = field(default_factory=list)
    bookings_notified: list = field(default_factory=list)
    users_notified: set = field(default_factory=set)
    results_paused: list = field(default_factory=list)
    results_resumed: list = field(default_factory=list)
    slots_restored: list = field(default_factory=list)
    already_active: bool = False
    skipped: bool = False


# --- slots --------------------------------------------------------------------------------------------------------


def _convert_remaining_slots_of_day(period, now) -> list[int]:
    """Today's Available slots that have not ended yet become Under Maintenance; returns their ids."""
    from .models import DailySlot, SlotStatus

    today = timezone.localtime(now).date()
    qs = DailySlot.objects.filter(
        slot_master__equipment_id=period.equipment_id,
        date=today,
        status=SlotStatus.AVAILABLE,
        booking__isnull=True,
        end_datetime__gt=now,
    )
    ids = list(qs.values_list("id", flat=True))
    if not ids:
        return []
    DailySlot.objects.filter(pk__in=ids, status=SlotStatus.AVAILABLE).update(status=SlotStatus.UNDER_MAINTENANCE)
    known = set(period.auto_slot_ids or [])
    period.auto_slot_ids = sorted(known | set(ids))
    period.save(update_fields=["auto_slot_ids", "updated_at"])
    try:
        from .disruption_service import DisruptionInput, record_unrecorded_slots

        record_unrecorded_slots(
            period.equipment,
            DailySlot.objects.filter(pk__in=ids),
            DisruptionInput(user=period.started_by, source="EQUIPMENT_STATUS"),
            old_status=SlotStatus.AVAILABLE,
        )
    except Exception:
        logger.exception("maintenance_auto: could not record converted slots for equipment %s", period.equipment_id)
    return ids


def _manually_marked_slot_ids(equipment_id) -> set[int]:
    """Slots whose latest staff status change set them Under Maintenance."""
    from .models import SlotStatus, SlotStatusChangeLog

    latest: dict[int, str] = {}
    for new_status, slot_ids in (
        SlotStatusChangeLog.objects.filter(equipment_id=equipment_id, source__in=STAFF_SLOT_SOURCES)
        .order_by("changed_at", "id")
        .values_list("new_status", "slot_ids")
    ):
        for sid in slot_ids or []:
            try:
                latest[int(sid)] = new_status
            except (TypeError, ValueError):
                continue
    return {sid for sid, st in latest.items() if st == SlotStatus.UNDER_MAINTENANCE}


def restorable_slot_ids(period, now) -> list[int]:
    """Unbooked Under Maintenance slots from now on that this period put there: converted by the mechanism, or
    generated while the equipment was Under Maintenance. Slots staff marked by hand are kept.

    Adopted periods were marked before the mechanism existed, when marking converted every future slot at once,
    so all of them are restored (except the hand-marked ones), as the old Operational path did."""
    from django.db.models import Q

    from .models import DailySlot, SlotStatus

    qs = DailySlot.objects.filter(
        slot_master__equipment_id=period.equipment_id,
        status=SlotStatus.UNDER_MAINTENANCE,
        booking__isnull=True,
        end_datetime__gt=now,
    )
    if not period.adopted:
        qs = qs.filter(Q(pk__in=list(period.auto_slot_ids or [])) | Q(created_at__gte=period.started_at))
    manual = _manually_marked_slot_ids(period.equipment_id)
    return [sid for sid in qs.values_list("id", flat=True) if sid not in manual]


# --- bookings -----------------------------------------------------------------------------------------------------


def _bookings_on_day_from_now(equipment_id, now) -> list:
    """BOOKED / PENDING bookings with a slot today that is in progress or still to come."""
    from .models import Booking, DailySlot

    today = timezone.localtime(now).date()
    ids = set(
        DailySlot.objects.filter(
            slot_master__equipment_id=equipment_id,
            date=today,
            booking__isnull=False,
            booking__status__in=_upcoming_statuses(),
            end_datetime__gt=now,
        ).values_list("booking_id", flat=True)
    )
    return list(
        Booking.objects.filter(pk__in=ids)
        .select_related("equipment", "user")
        .prefetch_related("daily_slots")
        .order_by("booking_id")
    )


def upcoming_bookings(equipment_id, now):
    """BOOKED / PENDING bookings of the equipment with a slot that has not ended yet."""
    from .models import Booking

    return (
        Booking.objects.filter(equipment_id=equipment_id, status__in=_upcoming_statuses())
        .annotate(first_start=Min("daily_slots__start_datetime"), last_end=Max("daily_slots__end_datetime"))
        .filter(last_end__gt=now)
        .select_related("equipment", "user")
        .order_by("first_start", "booking_id")
    )


def results_pause_candidates(equipment_id, now):
    """Open bookings whose slots are over (awaiting results) and whose results clock is not paused yet."""
    from .models import Booking

    return (
        Booking.objects.filter(
            equipment_id=equipment_id,
            status__in=_results_open_statuses(),
            results_paused_at__isnull=True,
        )
        .annotate(last_slot_end=Max("daily_slots__end_datetime"))
        .filter(last_slot_end__isnull=False, last_slot_end__lte=now)
        .order_by("booking_id")
    )


def _booking_window(booking):
    first = getattr(booking, "first_start", None)
    last = getattr(booking, "last_end", None)
    if first is None or last is None:
        slots = [(s.start_datetime, s.end_datetime) for s in booking.daily_slots.all() if s.start_datetime]
        if slots:
            first = min(s for s, _e in slots)
            last = max(e for _s, e in slots if e) if any(e for _s, e in slots) else None
    return first, last


def _booking_ref(booking) -> str:
    from iic_booking.communication.utils import booking_display_id_for_email

    return booking_display_id_for_email(booking) or str(booking.booking_id)


def _bookings_list(bookings: Iterable, *, with_user: bool = False) -> tuple[str, str]:
    """(html rows for a details card, plain text lines)."""
    from iic_booking.communication.email_branding import detail_row_html

    rows, lines = [], []
    items = list(bookings)
    for b in items[:MAX_LIST_ROWS]:
        first, last = _booking_window(b)
        when = _fmt_window(first, last) or "—"
        status = b.get_status_display() if hasattr(b, "get_status_display") else str(b.status)
        who = ""
        if with_user and getattr(b, "user", None) is not None:
            from iic_booking.communication.in_app import person_label

            who = f" · {person_label(b.user)}"
        rows.append(detail_row_html(_booking_ref(b), escape(f"{when} · {status}{who}")))
        lines.append(f"- {_booking_ref(b)}: {when} ({status}){who}")
    if len(items) > MAX_LIST_ROWS:
        more = len(items) - MAX_LIST_ROWS
        rows.append(detail_row_html("More", escape(f"{more} more booking{'s' if more != 1 else ''}")))
        lines.append(f"- … and {more} more")
    return "\n".join(rows), "\n".join(lines)


# --- notifications ------------------------------------------------------------------------------------------------


def _claim(period, kind, *, booking=None, user=None, for_date=None) -> bool:
    """Record that this email goes out; False when it was already sent (idempotent per period)."""
    from .models import MaintenanceNotification

    key = ":".join(
        [
            str(kind),
            str(getattr(booking, "pk", None) or "-"),
            str(getattr(user, "pk", None) or "-"),
            for_date.isoformat() if for_date else "-",
        ]
    )
    _obj, created = MaintenanceNotification.objects.get_or_create(
        period=period,
        dedupe_key=key,
        defaults={"kind": kind, "booking": booking, "user": user, "for_date": for_date},
    )
    return created


def ensure_template(code: str):
    """The DB template, created from the catalog when missing (never overwritten)."""
    from iic_booking.communication.default_email_templates import get_default_email_template
    from iic_booking.communication.models import CommunicationTemplate

    existing = CommunicationTemplate.objects.filter(
        code=code, communication_type=CommunicationTemplate.CommunicationType.EMAIL
    ).first()
    if existing is not None:
        return existing
    spec = get_default_email_template(code)
    if spec is None:
        return None
    fields = {k: spec[k] for k in ("name", "subject", "body_text", "body_html", "description", "variable_help")}
    template, _ = CommunicationTemplate.objects.get_or_create(
        code=code,
        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
        defaults={**fields, "is_active": True},
    )
    return template


def _deliver(user, code: str, context: dict, *, title: str, message: str, path: str, event: str, meta: dict):
    """Email (template ``code``) plus an in-app / push notice, after the surrounding transaction commits."""
    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.users.display import get_user_display_name

    if user is None or getattr(user, "is_active", True) is False:
        return

    def _email():
        try:
            template = ensure_template(code)
            if template is None or not getattr(user, "email", None):
                return
            CommunicationService.send_email(
                recipient=user,
                template=template,
                template_context={
                    **context,
                    "user_name": get_user_display_name(user),
                    "link": get_frontend_absolute_url(path) or path,
                },
                metadata={"module": "maintenance_auto", "event": event, **meta},
            )
        except Exception:
            logger.exception("maintenance_auto: email %s to user %s failed", code, getattr(user, "pk", None))

    try:
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(_email)
        else:
            _email()
    except Exception:
        logger.exception("maintenance_auto: email dispatch failed code=%s", code)
    try:
        notify_in_app(
            [user],
            title=title,
            message=message,
            link=path,
            notification_type="booking",
            event=event,
            extra={"notification_category": "bookings", **meta},
        )
    except Exception:
        logger.exception("maintenance_auto: in-app notice failed user=%s", getattr(user, "pk", None))


def _period_context(period) -> dict:
    eq = period.equipment
    event = period.disruption_event
    expected = getattr(event, "expected_recovery_at", None) if event else None
    return {
        "equipment_name": _equipment_label(eq),
        "equipment_code": getattr(eq, "code", "") or "",
        "maintenance_since": _fmt(period.started_at),
        "expected_recovery": _fmt(expected) if expected else "",
    }


def _send_day_email(period, booking, now) -> bool:
    from .booking_links import booking_detail_path

    today = timezone.localtime(now).date()
    if not _claim(period, "DAY", booking=booking, user=booking.user, for_date=today):
        return False
    first, last = _booking_window(booking)
    ref = _booking_ref(booking)
    eq = _equipment_label(period.equipment)
    _deliver(
        booking.user,
        TEMPLATE_DAY,
        {
            **_period_context(period),
            "booking_id": ref,
            "start_time": _fmt(first),
            "end_time": _fmt(last),
            "maintenance_day": timezone.localtime(now).strftime("%a %d %b %Y"),
        },
        title=f"On hold: {eq} under maintenance",
        message=(
            f"Booking {ref} is on hold because {eq} is under maintenance. It will be moved automatically to the "
            "earliest free slot once the equipment is back; you can cancel for a full refund until then."
        ),
        path=booking_detail_path(display_id=ref, staff=False) or "/my-bookings",
        event="maintenance.day",
        meta={"booking_id": booking.booking_id, "maintenance_period_id": period.pk},
    )
    return True


def _notify_upcoming(period, now, *, on_hold_ids: set, outcome: Outcome) -> None:
    """Everyone with an upcoming booking on the equipment: one email per user listing the bookings not told yet."""
    by_user: dict[int, list] = {}
    users: dict[int, object] = {}
    for b in upcoming_bookings(period.equipment_id, now):
        if b.booking_id in on_hold_ids or b.user_id is None:
            continue
        by_user.setdefault(b.user_id, []).append(b)
        users[b.user_id] = b.user
    held_users = set(outcome.users_notified)
    eq = _equipment_label(period.equipment)
    for uid, bookings in by_user.items():
        fresh = [b for b in bookings if _claim(period, "MARKED", booking=b, user=users[uid])]
        if not fresh:
            continue
        html, text = _bookings_list(fresh)
        _deliver(
            users[uid],
            TEMPLATE_NOTICE,
            {
                **_period_context(period),
                "bookings_html": html,
                "bookings_text": text,
                "today_handled": "yes" if uid in held_users else "",
            },
            title=f"{eq} is under maintenance",
            message=(
                f"{eq} is under maintenance. Your upcoming booking{'s' if len(fresh) != 1 else ''} "
                f"({', '.join(_booking_ref(b) for b in fresh[:5])}) may be affected. If it is still under "
                "maintenance on the day, the booking goes on hold and is moved automatically to the earliest free "
                "slot once the equipment is back."
            ),
            path="/my-bookings",
            event="maintenance.marked",
            meta={"maintenance_period_id": period.pk, "booking_ids": [b.booking_id for b in fresh]},
        )
        outcome.bookings_notified.extend(b.booking_id for b in fresh)
        outcome.users_notified.add(uid)


def _staff_summary(
    period, kind: str, *, subject: str, intro: str, bookings: list, for_date=None, extra=None, claim_booking=None
):
    from .completion_reminders import _candidate_recipients

    html, text = _bookings_list(bookings, with_user=True)
    context = {
        **_period_context(period),
        "summary_subject": subject,
        "summary_intro": intro,
        "bookings_html": html,
        "bookings_text": text,
        **{k: (str(v) if v not in (None, "") else "") for k, v in (extra or {}).items()},
    }
    for user in _candidate_recipients([period.equipment_id]):
        if not _claim(period, kind, booking=claim_booking, user=user, for_date=for_date):
            continue
        _deliver(
            user,
            TEMPLATE_STAFF,
            context,
            title=subject,
            message=intro,
            path="/dashboard",
            event=f"maintenance.staff.{kind.lower()}",
            meta={"maintenance_period_id": period.pk, "staff_recipient": True, "notification_category": "bookings"},
        )


# --- results overdue pause / resume -------------------------------------------------------------------------------


def _record_pause_event(booking, comment: str, metadata: dict, actor=None) -> None:
    from .booking_events import create_booking_event
    from .models import BookingEventType

    try:
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_by=actor,
            system_actor=actor is None,
            comment=comment,
            metadata=metadata,
            send_notification=False,
        )
    except Exception:
        logger.exception("maintenance_auto: could not record results pause event for booking %s", booking.booking_id)


def pause_results(period, now, *, actor=None) -> list[int]:
    from .models import Booking

    ids = list(results_pause_candidates(period.equipment_id, now).values_list("booking_id", flat=True))
    if not ids:
        return []
    Booking.objects.filter(pk__in=ids, results_paused_at__isnull=True).update(
        results_paused_at=now, results_pause_period=period
    )
    eq = _equipment_label(period.equipment)
    for booking in Booking.objects.filter(pk__in=ids).select_related("equipment", "user"):
        _record_pause_event(
            booking,
            f"Results overdue paused: {eq} was marked Under Maintenance on {_fmt(now)}. The results clock resumes, "
            "with the deadline extended by the paused time, when the equipment is Operational again.",
            {"results_pause": "paused", "maintenance_period_id": period.pk, "paused_at": now.isoformat()},
            actor,
        )
    return ids


def _resume_reference(booking):
    """The time the results clock pointed at before the pause (results overdue due time, else the slot-end timing)."""
    from .results_deadline import booking_last_slot_end, unreceived_safeguard_due_at
    from .results_overdue import results_due_at

    due = results_due_at(booking)
    if due is not None:
        return due
    slot_end = booking_last_slot_end(booking)
    return unreceived_safeguard_due_at(booking, slot_end) or slot_end


def resume_results(period, now, *, actor=None) -> list[int]:
    from .models import Booking

    resumed: list[int] = []
    eq = _equipment_label(period.equipment)
    qs = (
        Booking.objects.filter(results_pause_period=period, results_paused_at__isnull=False)
        .select_related("equipment", "user")
        .prefetch_related("daily_slots")
    )
    for booking in qs:
        paused_at = booking.results_paused_at
        paused_for = max(now - paused_at, timedelta(0))
        booking.results_paused_at = None
        fields = ["results_paused_at", "results_pause_period"]
        new_due = None
        if booking.status in _results_open_statuses():
            ref = _resume_reference(booking)
            if ref is not None:
                new_due = ref + paused_for
                hold = booking.operator_absent_hold_until
                if hold is None or new_due > hold:
                    booking.operator_absent_hold_until = new_due
                    fields.append("operator_absent_hold_until")
        booking.results_pause_period = None
        booking.save(update_fields=fields)
        hours = paused_for.total_seconds() / 3600
        paused_text = f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f} hours"
        _record_pause_event(
            booking,
            f"Results overdue resumed: {eq} is Operational again. Paused for {paused_text}"
            + (f"; results now due by {_fmt(new_due)} (extended by the paused time)." if new_due else "."),
            {
                "results_pause": "resumed",
                "maintenance_period_id": period.pk,
                "paused_at": paused_at.isoformat(),
                "resumed_at": now.isoformat(),
                "paused_seconds": int(paused_for.total_seconds()),
                **({"extended_to": new_due.isoformat()} if new_due else {}),
            },
            actor,
        )
        resumed.append(booking.booking_id)
    return resumed


def booking_results_pause_payload(booking) -> Optional[dict]:
    paused_at = getattr(booking, "results_paused_at", None)
    if not paused_at:
        return None
    return {
        "paused": True,
        "paused_at": paused_at.isoformat(),
        "paused_display": _fmt(paused_at),
        "reason": PAUSE_REASON,
    }


# --- the day's work -----------------------------------------------------------------------------------------------


def _handle_day(period, now, outcome: Outcome, *, reason: str) -> None:
    outcome.slots_marked.extend(_convert_remaining_slots_of_day(period, now))
    from .maintenance_policy import put_booking_on_maintenance_hold

    for booking in _bookings_on_day_from_now(period.equipment_id, now):
        try:
            if not put_booking_on_maintenance_hold(booking, now, reason=reason, send_email=False):
                continue
        except Exception:
            logger.exception("maintenance_auto: could not put booking %s on hold", booking.booking_id)
            continue
        outcome.bookings_on_hold.append(booking.booking_id)
        if _send_day_email(period, booking, now):
            outcome.users_notified.add(booking.user_id)


def start_period(equipment, *, user=None, now=None, adopted: bool = False) -> Outcome:
    """Equipment just marked Under Maintenance (or adopted). Idempotent: an active period is returned as is."""
    from .models import EquipmentMaintenancePeriod

    now = now or timezone.now()
    existing = active_period(equipment)
    if existing is not None:
        return Outcome(period=existing, already_active=True)
    event = _open_equipment_event(equipment)
    period = EquipmentMaintenancePeriod.objects.create(
        equipment=equipment,
        disruption_event=event,
        started_at=event.start_at if (adopted and event is not None) else now,
        started_by=user if getattr(user, "pk", None) else None,
        last_checked_on=timezone.localtime(now).date(),
        adopted=adopted,
    )
    outcome = Outcome(period=period)
    _handle_day(period, now, outcome, reason="Equipment status changed to Under Maintenance")
    _notify_upcoming(period, now, on_hold_ids=set(outcome.bookings_on_hold), outcome=outcome)
    outcome.results_paused = pause_results(period, now, actor=user if getattr(user, "pk", None) else None)
    if outcome.bookings_on_hold and event is not None:
        type(event).objects.filter(pk=event.pk).update(
            bookings_affected=F("bookings_affected") + len(outcome.bookings_on_hold)
        )
    _summarise_start(period, now, outcome)
    logger.info(
        "maintenance_auto: equipment %s period %s started slots=%s on_hold=%s notified_users=%s paused=%s",
        equipment.pk, period.pk, len(outcome.slots_marked), len(outcome.bookings_on_hold),
        len(outcome.users_notified), len(outcome.results_paused),
    )
    return outcome


def _summarise_start(period, now, outcome: Outcome) -> None:
    from .models import Booking

    eq = _equipment_label(period.equipment)
    on_hold = list(Booking.objects.filter(pk__in=outcome.bookings_on_hold).select_related("user", "equipment"))
    at_risk = list(upcoming_bookings(period.equipment_id, now))
    n_hold, n_risk = len(on_hold), len(at_risk)
    _staff_summary(
        period,
        "STAFF_MARKED",
        subject=f"Under maintenance: {eq} – {n_hold + n_risk} booking{'s' if n_hold + n_risk != 1 else ''} affected",
        intro=(
            f"{eq} was marked Under Maintenance on {_fmt(period.started_at)}. Today's bookings from now on are on hold "
            "and will be moved automatically, in booking order, to the earliest free slots once the equipment is "
            "Operational (users may cancel for a full refund until then); users with later bookings were told, and "
            "each day at 8:30 AM the bookings of that day are handled the same way while the equipment stays under "
            "maintenance."
        ),
        bookings=on_hold + at_risk,
        extra={
            "bookings_on_hold": n_hold,
            "bookings_at_risk": n_risk,
            "users_notified": len(outcome.users_notified),
            "results_paused": len(outcome.results_paused),
        },
    )


def run_daily_for_period(period, now) -> Outcome:
    """08:30 check for one active period. Claims the day first, so running twice the same day does nothing."""
    from .maintenance_policy import is_equipment_under_maintenance_status
    from .models import Booking, EquipmentMaintenancePeriod

    today = timezone.localtime(now).date()
    outcome = Outcome(period=period)
    if not is_equipment_under_maintenance_status(getattr(period.equipment, "status", None)):
        end_period(period.equipment, now=now)
        outcome.skipped = True
        return outcome
    from django.db.models import Q

    claimed = (
        EquipmentMaintenancePeriod.objects.filter(pk=period.pk, ended_at__isnull=True)
        .filter(Q(last_checked_on__isnull=True) | Q(last_checked_on__lt=today))
        .update(last_checked_on=today)
    )
    if not claimed:
        outcome.skipped = True
        return outcome
    period.refresh_from_db()
    _handle_day(period, now, outcome, reason="Equipment still Under Maintenance at the 08:30 check")
    outcome.results_paused = pause_results(period, now)
    if outcome.bookings_on_hold:
        eq = _equipment_label(period.equipment)
        on_hold = list(Booking.objects.filter(pk__in=outcome.bookings_on_hold).select_related("user", "equipment"))
        _staff_summary(
            period,
            "STAFF_DAY",
            subject=f"Still under maintenance: {eq} – {len(on_hold)} booking{'s' if len(on_hold) != 1 else ''} on hold today",
            intro=(
                f"{eq} is still Under Maintenance at the 8:30 AM check on {timezone.localtime(now):%a %d %b %Y}. "
                "Today's bookings are on hold until they are rescheduled automatically once the equipment is "
                "Operational, and the users were emailed."
            ),
            bookings=on_hold,
            for_date=today,
            extra={
                "bookings_on_hold": len(on_hold),
                "users_notified": len(outcome.users_notified),
                "results_paused": len(outcome.results_paused) or "",
            },
        )
        if period.disruption_event_id:
            from .models import DisruptionEvent

            DisruptionEvent.objects.filter(pk=period.disruption_event_id).update(
                bookings_affected=F("bookings_affected") + len(on_hold)
            )
    return outcome


def daily_check(now=None) -> dict:
    """Every active period: still Under Maintenance -> handle today; Operational (status changed without the save
    signal) -> close the period."""
    from .models import EquipmentMaintenancePeriod

    now = now or timezone.now()
    stats = {"periods": 0, "handled": 0, "ended": 0, "skipped": 0, "slots_marked": 0, "bookings_on_hold": 0,
             "users_notified": 0, "results_paused": 0}
    for period in EquipmentMaintenancePeriod.objects.filter(ended_at__isnull=True).select_related(
        "equipment", "disruption_event", "started_by"
    ):
        stats["periods"] += 1
        try:
            out = run_daily_for_period(period, now)
        except Exception:
            logger.exception("maintenance_auto: daily check failed for period %s", period.pk)
            continue
        if out.skipped:
            period.refresh_from_db()
            stats["ended" if period.ended_at else "skipped"] += 1
            continue
        stats["handled"] += 1
        stats["slots_marked"] += len(out.slots_marked)
        stats["bookings_on_hold"] += len(out.bookings_on_hold)
        stats["users_notified"] += len(out.users_notified)
        stats["results_paused"] += len(out.results_paused)
    from .maintenance_reschedule import allocate_pending_everywhere

    try:
        placed = allocate_pending_everywhere(now)
    except Exception:
        logger.exception("maintenance_auto: automatic reschedule failed at the daily check")
        placed = {}
    stats["rescheduled"] = placed.get("placed", 0)
    stats["still_on_hold"] = placed.get("unplaced", 0)
    return stats


def end_period(equipment, *, user=None, now=None) -> Optional[Outcome]:
    """Equipment Operational again. Returns None when it had no active period (legacy handling applies)."""
    from .models import Booking, DailySlot, MaintenanceNotification, SlotStatus

    now = now or timezone.now()
    period = active_period(equipment)
    if period is None:
        return None
    actor = user if getattr(user, "pk", None) else None
    period.ended_at = now
    period.ended_by = actor
    period.save(update_fields=["ended_at", "ended_by", "updated_at"])
    outcome = Outcome(period=period)

    restore = restorable_slot_ids(period, now)
    if restore:
        DailySlot.objects.filter(pk__in=restore, status=SlotStatus.UNDER_MAINTENANCE, booking__isnull=True).update(
            status=SlotStatus.AVAILABLE
        )
    outcome.slots_restored = restore
    outcome.results_resumed = resume_results(period, now, actor=actor)

    told = set(
        MaintenanceNotification.objects.filter(period=period, kind="MARKED", booking__isnull=False).values_list(
            "booking_id", flat=True
        )
    )
    eq = _equipment_label(period.equipment)
    by_user: dict[int, list] = {}
    users: dict[int, object] = {}
    for b in upcoming_bookings(period.equipment_id, now):
        if b.booking_id in told and b.user_id:
            by_user.setdefault(b.user_id, []).append(b)
            users[b.user_id] = b.user
    for uid, bookings in by_user.items():
        fresh = [b for b in bookings if _claim(period, "RESOLVED", booking=b, user=users[uid])]
        if not fresh:
            continue
        html, text = _bookings_list(fresh)
        _deliver(
            users[uid],
            TEMPLATE_RESOLVED,
            {**_period_context(period), "operational_since": _fmt(now), "bookings_html": html, "bookings_text": text},
            title=f"{eq} is operational – your booking stands",
            message=f"{eq} is back in working condition. Your upcoming booking{'s' if len(fresh) != 1 else ''} stand as booked.",
            path="/my-bookings",
            event="maintenance.resolved",
            meta={"maintenance_period_id": period.pk, "booking_ids": [b.booking_id for b in fresh]},
        )
        outcome.users_notified.add(uid)
        outcome.bookings_notified.extend(b.booking_id for b in fresh)

    remaining = list(Booking.objects.filter(pk__in=outcome.bookings_notified).select_related("user", "equipment"))
    _staff_summary(
        period,
        "STAFF_RESOLVED",
        subject=f"Operational again: {eq}",
        intro=(
            f"{eq} is Operational again ({_fmt(now)}). Slots set Under Maintenance by the daily check are bookable "
            "again, paused results deadlines were extended by the paused time, and users with remaining bookings "
            "were told their booking stands."
        ),
        bookings=remaining,
        extra={
            "operational_since": _fmt(now),
            "users_notified": len(outcome.users_notified),
            "results_resumed": len(outcome.results_resumed),
        },
    )
    logger.info(
        "maintenance_auto: equipment %s period %s ended restored_slots=%s resumed=%s notified_users=%s",
        equipment.pk, period.pk, len(restore), len(outcome.results_resumed), len(outcome.users_notified),
    )
    return outcome


# --- staff visibility ---------------------------------------------------------------------------------------------


def period_summary(period) -> dict:
    from .models import Booking, MaintenanceNotification

    notes = MaintenanceNotification.objects.filter(period=period)
    affected = set(
        notes.filter(kind__in=("MARKED", "DAY"), booking__isnull=False).values_list("booking_id", flat=True)
    )
    users = set(
        notes.filter(kind__in=("MARKED", "DAY", "RESOLVED"), user__isnull=False).values_list("user_id", flat=True)
    )
    on_hold = set(notes.filter(kind="DAY", booking__isnull=False).values_list("booking_id", flat=True))
    paused = Booking.objects.filter(results_pause_period=period, results_paused_at__isnull=False).count()
    eq = period.equipment
    return {
        "period_id": period.pk,
        "equipment_id": eq.equipment_id,
        "equipment_code": eq.code,
        "equipment_name": eq.name,
        "since": period.started_at.isoformat(),
        "since_display": _fmt(period.started_at),
        "last_checked_on": period.last_checked_on.isoformat() if period.last_checked_on else None,
        "bookings_affected": len(affected),
        "bookings_on_hold": len(on_hold),
        "users_notified": len(users),
        "results_paused": paused,
        "adopted": period.adopted,
    }


def active_summaries_for_user(user) -> list[dict]:
    from iic_booking.users.models.user_type import UserType

    from .completion_reminders import awaiting_completion_equipment_ids
    from .models import EquipmentMaintenancePeriod

    qs = EquipmentMaintenancePeriod.objects.filter(ended_at__isnull=True).select_related("equipment")
    if not (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN):
        ids = awaiting_completion_equipment_ids(user)
        if not ids:
            return []
        qs = qs.filter(equipment_id__in=ids)
    return [period_summary(p) for p in qs.order_by("started_at")]


# --- dry run for equipment already under maintenance --------------------------------------------------------------


def dry_run(now=None) -> list[dict]:
    """Read-only: what adopting each Under Maintenance equipment without a period would do right now."""
    from .maintenance_policy import is_equipment_under_maintenance_status
    from .models import Equipment, EquipmentStatus

    now = now or timezone.now()
    statuses = (EquipmentStatus.REPAIR, EquipmentStatus.INACTIVE, EquipmentStatus.MAINTENANCE)
    rows = []
    for eq in Equipment.objects.filter(status__in=statuses).order_by("code"):
        period = active_period(eq)
        event = _open_equipment_event(eq)
        today_hold = _bookings_on_day_from_now(eq.pk, now)
        hold_ids = {b.booking_id for b in today_hold}
        upcoming = [b for b in upcoming_bookings(eq.pk, now) if b.booking_id not in hold_ids]
        pause_ids = list(results_pause_candidates(eq.pk, now).values_list("booking_id", flat=True))
        from .results_overdue import overdue_booking_ids
        from .models import Booking

        overdue_now = set(overdue_booking_ids(Booking.objects.filter(equipment=eq), now))
        rows.append(
            {
                "equipment_id": eq.pk,
                "code": eq.code,
                "status": eq.status,
                "handled_by_mechanism": is_equipment_under_maintenance_status(eq.status),
                "has_period": period is not None,
                "since": event.start_at.isoformat() if event else None,
                "today_bookings_to_put_on_hold": sorted(hold_ids),
                "upcoming_bookings_to_notify": [b.booking_id for b in upcoming],
                "users_to_notify": len({b.user_id for b in upcoming} | {b.user_id for b in today_hold}),
                "results_bookings_to_pause": pause_ids,
                "of_which_currently_overdue": sorted(set(pause_ids) & overdue_now),
                "slots_to_mark_today": len(
                    _available_today_from_now(eq.pk, now)
                ),
            }
        )
    return rows


def _available_today_from_now(equipment_id, now) -> list[int]:
    from .models import DailySlot, SlotStatus

    return list(
        DailySlot.objects.filter(
            slot_master__equipment_id=equipment_id,
            date=timezone.localtime(now).date(),
            status=SlotStatus.AVAILABLE,
            booking__isnull=True,
            end_datetime__gt=now,
        ).values_list("id", flat=True)
    )


def dry_run_report_text(now=None) -> str:
    rows = dry_run(now)
    lines = [f"equipment under maintenance={len(rows)}"]
    users = bookings = paused = 0
    for r in rows:
        if r["has_period"]:
            lines.append(f"  {r['code']} (id={r['equipment_id']}): already handled by the mechanism (period active)")
            continue
        lines.append(
            f"  {r['code']} (id={r['equipment_id']}) status={r['status']} since={r['since']}"
            f" handled_status={r['handled_by_mechanism']}"
            f" today_on_hold={r['today_bookings_to_put_on_hold']}"
            f" upcoming_to_notify={r['upcoming_bookings_to_notify']}"
            f" users_to_notify={r['users_to_notify']}"
            f" results_to_pause={len(r['results_bookings_to_pause'])} ids={r['results_bookings_to_pause'][:40]}"
            f" of_which_overdue_now={r['of_which_currently_overdue']}"
            f" slots_to_mark_today={r['slots_to_mark_today']}"
        )
        users += r["users_to_notify"]
        bookings += len(r["today_bookings_to_put_on_hold"]) + len(r["upcoming_bookings_to_notify"])
        paused += len(r["results_bookings_to_pause"])
    lines.append(f"TOTAL (if adopted now): bookings={bookings} users_to_notify={users} results_to_pause={paused}")
    return "\n".join(lines)
