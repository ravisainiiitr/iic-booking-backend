"""Slot Exchange: two internal users swap booked slots of the same charge, with both agreeing.

Rules, checked when a request is made and again, under row locks, when it is accepted:

* The Main Admin has enabled Slot Exchange on the equipment (``Equipment.slot_exchange_enabled``).
* Both users, and both bookings, are internal (IITR student, individual student, faculty, IITR startup).
* Both bookings are Booked; none of their slots has started; the lab has not accepted or completed either sample.
* The same number of slots is exchanged on each side, slot for slot of the same length and the same charge
  (the booking's charge divided by its slots). A partial exchange is allowed: a user with two ₹100 slots may
  give either one for another user's single ₹100 slot. No money moves: each booking keeps its charge.
* Same equipment, or another equipment of the same group when whole bookings are exchanged and the group's
  cross-equipment rules accept each booking on the other equipment (same charge, compatible inputs).
* Until 5 minutes before the earliest slot in the exchange starts.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import Booking, BookingEventType, BookingStatus, DailySlot
from .slot_exchange_models import SlotExchangeRequest, SlotExchangeStatus

logger = logging.getLogger(__name__)

CUTOFF = timedelta(minutes=5)
EMAIL_TEMPLATE_CODE = "slot_exchange_email"
MAX_MESSAGE_LENGTH = 500


class ExchangeError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def is_internal_user(user) -> bool:
    return bool(user) and str(getattr(user, "user_type", "") or "").lower() in UserType.get_internal_user_codes()


def booking_is_internal(booking) -> bool:
    snapshot = str(getattr(booking, "user_type_snapshot", "") or "").lower()
    if snapshot:
        return snapshot in UserType.get_internal_user_codes()
    return is_internal_user(booking.user)


def per_slot_charge(booking, slot_count: Optional[int] = None) -> Decimal:
    count = slot_count if slot_count is not None else booking.daily_slots.count()
    total = Decimal(str(booking.total_charge or 0))
    if not count:
        return total
    return (total / count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _minutes(slot) -> int:
    return int((slot.end_datetime - slot.start_datetime).total_seconds() // 60)


def slot_summary(slot) -> dict:
    start = timezone.localtime(slot.start_datetime)
    end = timezone.localtime(slot.end_datetime)
    return {
        "id": slot.pk,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": f"{start:%a %d %b %Y, %H:%M}–{end:%H:%M}",
    }


def slots_label(summaries: Iterable[dict]) -> str:
    return "; ".join(s["label"] for s in summaries)


def deadline_for(slots: Iterable) -> Optional[timezone.datetime]:
    starts = [s.start_datetime for s in slots]
    return min(starts) - CUTOFF if starts else None


@dataclass
class ExchangePlan:
    """A checked exchange: what moves where, and whether the bookings change equipment."""

    requester_booking: Booking
    responder_booking: Booking
    offered: list = field(default_factory=list)
    requested: list = field(default_factory=list)
    cross_equipment: bool = False
    requester_move: Optional[dict] = None  # reschedule_target_info of requester's booking on the other equipment
    responder_move: Optional[dict] = None

    @property
    def expires_at(self):
        return deadline_for(self.offered + self.requested)


def _sample_locked(booking) -> bool:
    from .reschedule_lock import sample_accepted_by_lab

    return sample_accepted_by_lab(booking)


def _booking_problem(booking, now, *, whose: str) -> Optional[ExchangeError]:
    if booking.status != BookingStatus.BOOKED:
        return ExchangeError(f"{whose} booking is not in Booked status.", "BOOKING_NOT_BOOKED")
    if not booking_is_internal(booking):
        return ExchangeError("Slot Exchange is only for internal (IITR) bookings.", "NOT_INTERNAL")
    if not getattr(booking.equipment, "slot_exchange_enabled", False):
        return ExchangeError(f"Slot Exchange is not enabled for {booking.equipment.name}.", "EXCHANGE_DISABLED")
    if booking.daily_slots.filter(start_datetime__lte=now).exists():
        return ExchangeError(f"{whose} booking has already started.", "ALREADY_STARTED")
    if _sample_locked(booking):
        return ExchangeError(f"The lab has already accepted the sample of {whose.lower()} booking.", "SAMPLE_ACCEPTED")
    return None


def plan_exchange(requester, offered_slot_ids, requested_slot_ids, *, now=None, lock=False) -> ExchangePlan:
    """Check every rule for ``requester`` giving ``offered_slot_ids`` for ``requested_slot_ids``.

    Raises ``ExchangeError`` with a plain-language message on the first rule that fails.
    With ``lock`` (inside a transaction) the bookings and slots are locked first.
    """
    now = now or timezone.now()
    offered_ids = sorted({int(i) for i in offered_slot_ids or []})
    requested_ids = sorted({int(i) for i in requested_slot_ids or []})
    if not offered_ids or not requested_ids:
        raise ExchangeError("Choose the slots to exchange on both sides.", "NO_SLOTS")
    if set(offered_ids) & set(requested_ids):
        raise ExchangeError("A slot cannot be on both sides of an exchange.", "SAME_SLOT")
    if not is_internal_user(requester):
        raise ExchangeError("Slot Exchange is only available to internal (IITR) users.", "NOT_INTERNAL")

    slot_qs = DailySlot.objects.select_related("slot_master")
    offered = list(slot_qs.filter(pk__in=offered_ids).order_by("start_datetime"))
    requested = list(slot_qs.filter(pk__in=requested_ids).order_by("start_datetime"))
    if len(offered) != len(offered_ids) or len(requested) != len(requested_ids):
        raise ExchangeError("One of the slots no longer exists. Refresh and try again.", "SLOT_MISSING")

    offered_bookings = {s.booking_id for s in offered}
    requested_bookings = {s.booking_id for s in requested}
    if None in offered_bookings or len(offered_bookings) != 1:
        raise ExchangeError("The slots you give must all belong to one of your bookings.", "OFFER_NOT_ONE_BOOKING")
    if None in requested_bookings or len(requested_bookings) != 1:
        raise ExchangeError("The slots you ask for must all belong to one booking.", "REQUEST_NOT_ONE_BOOKING")

    booking_qs = Booking.objects.select_related("equipment", "equipment__equipment_group", "user")
    ids = sorted(offered_bookings | requested_bookings)
    if lock:
        # Same order everywhere (by primary key) so two exchanges never wait on each other.
        list(Booking.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk", flat=True))
        list(
            DailySlot.objects.select_for_update()
            .filter(pk__in=offered_ids + requested_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        offered = list(slot_qs.filter(pk__in=offered_ids).order_by("start_datetime"))
        requested = list(slot_qs.filter(pk__in=requested_ids).order_by("start_datetime"))
    mine = booking_qs.get(pk=next(iter(offered_bookings)))
    theirs = booking_qs.get(pk=next(iter(requested_bookings)))
    if any(s.booking_id != mine.pk for s in offered) or any(s.booking_id != theirs.pk for s in requested):
        raise ExchangeError("These slots changed hands meanwhile. Refresh and try again.", "SLOTS_CHANGED")
    if mine.user_id != requester.pk:
        raise ExchangeError("You can only give slots from your own booking.", "NOT_YOUR_BOOKING")
    if theirs.user_id == requester.pk:
        raise ExchangeError("Both slots are already yours.", "SAME_USER")
    if not is_internal_user(theirs.user):
        raise ExchangeError("The other booking is not by an internal (IITR) user.", "NOT_INTERNAL")

    for booking, whose in ((mine, "Your"), (theirs, "The other")):
        problem = _booking_problem(booking, now, whose=whose)
        if problem:
            raise problem

    if len(offered) != len(requested):
        raise ExchangeError(
            f"Exchange the same number of slots on each side (you chose {len(offered)} for {len(requested)}).",
            "COUNT_MISMATCH",
        )
    if sorted(_minutes(s) for s in offered) != sorted(_minutes(s) for s in requested):
        raise ExchangeError("The slots on each side must be of the same length.", "LENGTH_MISMATCH")
    my_count = mine.daily_slots.count()
    their_count = theirs.daily_slots.count()
    if per_slot_charge(mine, my_count) != per_slot_charge(theirs, their_count):
        raise ExchangeError(
            f"The charges differ (₹{per_slot_charge(mine, my_count)} per slot for yours, "
            f"₹{per_slot_charge(theirs, their_count)} for theirs). Only slots of the same charge can be exchanged.",
            "CHARGE_MISMATCH",
        )

    plan = ExchangePlan(requester_booking=mine, responder_booking=theirs, offered=offered, requested=requested)
    if mine.equipment_id != theirs.equipment_id:
        _plan_cross_equipment(plan, requester, my_count, their_count)

    deadline = plan.expires_at
    if deadline is None or now >= deadline:
        raise ExchangeError(
            "Too late: an exchange must be agreed at least 5 minutes before the slots start.", "TOO_LATE"
        )
    return plan


def _plan_cross_equipment(plan: ExchangePlan, actor, my_count: int, their_count: int) -> None:
    from .equipment_group_service import reschedule_target_info

    mine, theirs = plan.requester_booking, plan.responder_booking
    group_id = mine.equipment.equipment_group_id
    if not group_id or group_id != theirs.equipment.equipment_group_id:
        raise ExchangeError("Slots can only be exchanged within the same equipment group.", "DIFFERENT_GROUP")
    if len(plan.offered) != my_count or len(plan.requested) != their_count:
        raise ExchangeError(
            "Between two equipment of a group only whole bookings can be exchanged. "
            "Choose all slots of both bookings.",
            "PARTIAL_CROSS_EQUIPMENT",
        )
    for booking, target, needed in ((mine, theirs.equipment, their_count), (theirs, mine.equipment, my_count)):
        info = reschedule_target_info(booking, target, actor=actor)
        if not info.get("ok"):
            raise ExchangeError(info.get("reason") or "This equipment cannot take that booking.", info.get("code") or "TARGET")
        if info.get("charge_differs"):
            raise ExchangeError("The two equipment charge these bookings differently.", "CHARGE_MISMATCH")
        if int(info.get("required_slots") or 0) != needed:
            raise ExchangeError(
                f"{target.name} needs {info.get('required_slots')} slot(s) for one of these bookings, "
                f"not {needed}.",
                "SLOT_COUNT_MISMATCH",
            )
        if info.get("dropped_fields"):
            raise ExchangeError(
                f"Some booking details would be lost on {target.name}; these bookings cannot be exchanged.",
                "INPUTS_INCOMPATIBLE",
            )
    plan.cross_equipment = True
    plan.requester_move = reschedule_target_info(mine, theirs.equipment, actor=actor)
    plan.responder_move = reschedule_target_info(theirs, mine.equipment, actor=actor)


# --- lifecycle -----------------------------------------------------------------------------------------------


def expire_stale(queryset=None, now=None) -> int:
    """Mark pending requests whose time has run out as Expired."""
    now = now or timezone.now()
    qs = queryset if queryset is not None else SlotExchangeRequest.objects.all()
    return qs.filter(status=SlotExchangeStatus.PENDING, expires_at__lte=now).update(
        status=SlotExchangeStatus.EXPIRED, responded_at=now
    )


def create_request(requester, offered_slot_ids, requested_slot_ids, message: str = "", now=None):
    now = now or timezone.now()
    message = (message or "").strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ExchangeError(f"Keep the message under {MAX_MESSAGE_LENGTH} characters.", "MESSAGE_TOO_LONG")
    plan = plan_exchange(requester, offered_slot_ids, requested_slot_ids, now=now)
    expire_stale(now=now)
    requested_ids = [s.pk for s in plan.requested]
    duplicate = SlotExchangeRequest.objects.filter(
        requester=requester, status=SlotExchangeStatus.PENDING, requested_slots__in=requested_ids
    ).exists()
    if duplicate:
        raise ExchangeError("You already have a pending request for these slots.", "DUPLICATE")
    with transaction.atomic():
        req = SlotExchangeRequest.objects.create(
            requester=requester,
            responder=plan.responder_booking.user,
            requester_booking=plan.requester_booking,
            responder_booking=plan.responder_booking,
            offered_summary=[slot_summary(s) for s in plan.offered],
            requested_summary=[slot_summary(s) for s in plan.requested],
            message=message,
            expires_at=plan.expires_at,
        )
        req.offered_slots.set(plan.offered)
        req.requested_slots.set(plan.requested)
        transaction.on_commit(lambda: notify(req, "requested"))
    return req


def _pending_for(req_id, *, user, role: str) -> SlotExchangeRequest:
    req = SlotExchangeRequest.objects.select_related(
        "requester", "responder", "requester_booking", "responder_booking"
    ).filter(pk=req_id).first()
    if req is None or getattr(req, f"{role}_id") != user.pk:
        raise ExchangeError("Exchange request not found.", "NOT_FOUND")
    expire_stale(SlotExchangeRequest.objects.filter(pk=req.pk))
    req.refresh_from_db()
    if req.status != SlotExchangeStatus.PENDING:
        raise ExchangeError(f"This request is already {req.get_status_display().lower()}.", "NOT_PENDING")
    return req


def accept_request(req_id, responder, now=None) -> SlotExchangeRequest:
    now = now or timezone.now()
    req = _pending_for(req_id, user=responder, role="responder")
    offered_ids = list(req.offered_slots.values_list("pk", flat=True))
    requested_ids = list(req.requested_slots.values_list("pk", flat=True))
    try:
        with transaction.atomic():
            locked = SlotExchangeRequest.objects.select_for_update().get(pk=req.pk)
            if locked.status != SlotExchangeStatus.PENDING:
                raise ExchangeError("This request was answered meanwhile.", "NOT_PENDING")
            plan = plan_exchange(locked.requester, offered_ids, requested_ids, now=now, lock=True)
            if plan.requester_booking.pk != locked.requester_booking_id or plan.responder_booking.pk != locked.responder_booking_id:
                raise ExchangeError("These slots changed hands meanwhile.", "SLOTS_CHANGED")
            _swap(plan, locked, responder)
            locked.status = SlotExchangeStatus.ACCEPTED
            locked.responded_at = now
            locked.save(update_fields=["status", "responded_at"])
            _close_overlapping(locked, now)
            transaction.on_commit(lambda: notify(locked, "accepted"))
            return locked
    except ExchangeError as exc:
        # The swap is no longer possible: close the request so it stops asking for an answer.
        SlotExchangeRequest.objects.filter(pk=req.pk, status=SlotExchangeStatus.PENDING).update(
            status=SlotExchangeStatus.FAILED, failure_reason=exc.message, responded_at=now
        )
        req.refresh_from_db()
        transaction.on_commit(lambda: notify(req, "failed"))
        raise


def decline_request(req_id, responder, note: str = "", now=None) -> SlotExchangeRequest:
    req = _pending_for(req_id, user=responder, role="responder")
    req.status = SlotExchangeStatus.DECLINED
    req.response_note = (note or "").strip()[:MAX_MESSAGE_LENGTH]
    req.responded_at = now or timezone.now()
    req.save(update_fields=["status", "response_note", "responded_at"])
    notify(req, "declined")
    return req


def cancel_request(req_id, requester, now=None) -> SlotExchangeRequest:
    req = _pending_for(req_id, user=requester, role="requester")
    req.status = SlotExchangeStatus.CANCELLED
    req.responded_at = now or timezone.now()
    req.save(update_fields=["status", "responded_at"])
    notify(req, "cancelled")
    return req


def _swap(plan: ExchangePlan, req: SlotExchangeRequest, actor) -> None:
    from .booking_events import create_booking_event

    mine, theirs = plan.requester_booking, plan.responder_booking
    offered_ids = [s.pk for s in plan.offered]
    requested_ids = [s.pk for s in plan.requested]
    DailySlot.objects.filter(pk__in=offered_ids).update(booking=theirs)
    DailySlot.objects.filter(pk__in=requested_ids).update(booking=mine)
    if plan.cross_equipment:
        my_equipment, their_equipment = mine.equipment, theirs.equipment
        for booking, target, move in (
            (mine, their_equipment, plan.requester_move),
            (theirs, my_equipment, plan.responder_move),
        ):
            booking.equipment = target
            if move.get("charge_profile_id"):
                booking.charge_profile_id = move["charge_profile_id"]
            booking.input_values = move.get("input_values") or booking.input_values
            booking.save(update_fields=["equipment", "charge_profile", "input_values"])

    gave = slots_label(req.offered_summary)
    got = slots_label(req.requested_summary)
    common = {"slot_exchange_id": req.pk, "cross_equipment": plan.cross_equipment}
    create_booking_event(
        booking=mine,
        event_type=BookingEventType.RESCHEDULED,
        created_by=actor,
        comment=f"Slot exchange #{req.pk} with {_name(req.responder)}: gave {gave}; received {got}.",
        previous_status=mine.status,
        new_status=mine.status,
        metadata={**common, "gave": req.offered_summary, "received": req.requested_summary},
        send_notification=False,
    )
    create_booking_event(
        booking=theirs,
        event_type=BookingEventType.RESCHEDULED,
        created_by=actor,
        comment=f"Slot exchange #{req.pk} with {_name(req.requester)}: gave {got}; received {gave}.",
        previous_status=theirs.status,
        new_status=theirs.status,
        metadata={**common, "gave": req.requested_summary, "received": req.offered_summary},
        send_notification=False,
    )


def _close_overlapping(done: SlotExchangeRequest, now) -> None:
    """Other pending requests that named any slot which just changed hands can no longer happen."""
    moved = list(done.offered_slots.values_list("pk", flat=True)) + list(
        done.requested_slots.values_list("pk", flat=True)
    )
    stale = (
        SlotExchangeRequest.objects.filter(status=SlotExchangeStatus.PENDING)
        .exclude(pk=done.pk)
        .filter(offered_slots__in=moved)
        .union(
            SlotExchangeRequest.objects.filter(status=SlotExchangeStatus.PENDING)
            .exclude(pk=done.pk)
            .filter(requested_slots__in=moved)
        )
        .values_list("pk", flat=True)
    )
    SlotExchangeRequest.objects.filter(pk__in=list(stale)).update(
        status=SlotExchangeStatus.FAILED,
        failure_reason="One of these slots was exchanged with someone else.",
        responded_at=now,
    )


# --- presentation --------------------------------------------------------------------------------------------


def _name(user) -> str:
    from iic_booking.communication.email_branding import user_display_name

    return user_display_name(user, fallback=getattr(user, "email", "") or "a user")


def booker_details(user) -> dict:
    """What another internal user may see about an internal booker."""
    dept = getattr(user, "department", None)
    return {
        "name": _name(user),
        "email": (getattr(user, "email", "") or "").strip() or None,
        "phone": (getattr(user, "phone_number", "") or "").strip() or None,
        "user_type": str(getattr(user, "user_type", "") or "") or None,
        "user_type_display": dict(UserType.get_choices()).get(getattr(user, "user_type", None)),
        "department": (getattr(dept, "name", "") or "").strip() or None,
        "department_code": (getattr(dept, "code", "") or "").strip() or None,
    }


def serialize_request(req: SlotExchangeRequest, viewer) -> dict:
    incoming = req.responder_id == viewer.pk
    other = req.requester if incoming else req.responder
    pending = req.status == SlotExchangeStatus.PENDING
    from iic_booking.communication.utils import booking_display_id_for_email

    return {
        "id": req.pk,
        "status": req.status,
        "status_display": req.get_status_display(),
        "direction": "incoming" if incoming else "outgoing",
        "equipment_name": req.responder_booking.equipment.name,
        "equipment_code": req.responder_booking.equipment.code,
        "other_user": booker_details(other),
        "requester_booking_id": req.requester_booking_id,
        "requester_booking_ref": booking_display_id_for_email(req.requester_booking),
        "responder_booking_id": req.responder_booking_id,
        "responder_booking_ref": booking_display_id_for_email(req.responder_booking),
        # From the viewer's side: what they give and what they get.
        "you_give": req.requested_summary if incoming else req.offered_summary,
        "you_get": req.offered_summary if incoming else req.requested_summary,
        "message": req.message,
        "response_note": req.response_note,
        "failure_reason": req.failure_reason,
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "responded_at": req.responded_at.isoformat() if req.responded_at else None,
        "can_respond": incoming and pending,
        "can_cancel": (not incoming) and pending,
    }


# --- notifications -------------------------------------------------------------------------------------------

_HEADINGS = {
    "requested": "Slot exchange request",
    "accepted": "Slot exchange accepted",
    "declined": "Slot exchange declined",
    "cancelled": "Slot exchange request withdrawn",
    "failed": "Slot exchange could not be completed",
}


def notify(req: SlotExchangeRequest, kind: str) -> None:
    """In-app + email to the person who has to know. Failures are logged, never raised."""
    try:
        _notify(req, kind)
    except Exception:
        logger.exception("Slot exchange notification failed request=%s kind=%s", req.pk, kind)


def _notify(req: SlotExchangeRequest, kind: str) -> None:
    from iic_booking.communication.email_branding import absolute_http_url, format_email_datetime
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url

    from .booking_events import booking_party_context
    from .booking_lab_messages import _booking_time_rows

    to_responder = kind in ("requested", "cancelled")
    recipient = req.responder if to_responder else req.requester
    sender = req.requester if to_responder else req.responder
    booking = req.responder_booking if to_responder else req.requester_booking
    # The recipient's side of the exchange.
    gives = req.requested_summary if to_responder else req.offered_summary
    gets = req.offered_summary if to_responder else req.requested_summary
    sender_name = _name(sender)
    equipment = booking.equipment
    heading = _HEADINGS[kind]
    intro = {
        "requested": f"{sender_name} would like to exchange slots with you on {equipment.name}. "
        "Nothing changes unless you accept.",
        "accepted": f"{sender_name} accepted your slot exchange on {equipment.name}. Your booking now has the new slots.",
        "declined": f"{sender_name} declined your slot exchange request on {equipment.name}.",
        "cancelled": f"{sender_name} withdrew the slot exchange request on {equipment.name}.",
        "failed": f"Your slot exchange with {sender_name} on {equipment.name} could not be completed: "
        f"{req.failure_reason or 'the slots are no longer available.'}",
    }[kind]
    path = "/slot-exchanges"
    link = absolute_http_url(get_frontend_absolute_url(path) or path)
    note = req.message if kind == "requested" else req.response_note
    display_ref = booking_display_id_for_email(booking)
    meta = {
        "booking_id": display_ref,
        "real_booking_id": booking.booking_id,
        "slot_exchange_id": req.pk,
        "slot_exchange_event": kind,
        "link": link,
    }
    if (recipient.email or "").strip():
        try:
            _ensure_email_template()
            CommunicationService.send_email(
                recipient=recipient,
                template=EMAIL_TEMPLATE_CODE,
                template_context={
                    "user_name": _name(recipient),
                    "user_email": recipient.email or "",
                    "booking_id": display_ref,
                    "virtual_booking_id": display_ref,
                    "equipment_name": equipment.name,
                    "equipment_code": equipment.code,
                    "exchange_heading": heading,
                    "exchange_intro": intro,
                    "exchange_status": req.get_status_display(),
                    "slots_given": slots_label(gives),
                    "slots_received": slots_label(gets),
                    "respond_by": format_email_datetime(req.expires_at) if kind == "requested" else "",
                    "counterparty_display": f"{sender_name} ({sender.email})" if sender.email else sender_name,
                    "exchange_message": note,
                    "exchange_message_html": html.escape(note or ""),
                    "link": link,
                    **_booking_time_rows(booking),
                    **booking_party_context(booking.user, equipment),
                },
                metadata=meta,
                created_by=sender,
            )
        except Exception:
            logger.exception("Slot exchange email failed request=%s kind=%s", req.pk, kind)
    CommunicationService.send_push_notification(
        recipient=recipient,
        title=f"{heading} — {equipment.name}",
        message=f"{intro} You give: {slots_label(gives)}. You get: {slots_label(gets)}.",
        metadata={**meta, "notification_type": "info", "event": f"slot_exchange.{kind}"},
        created_by=sender,
    )


def _ensure_email_template() -> None:
    from .booking_lab_messages import ensure_email_template

    ensure_email_template(EMAIL_TEMPLATE_CODE)
