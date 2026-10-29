"""Officer in Charge / Main Administrator decisions on external special requests.

Who may decide: the same staff who may decide urgent requests (``check_operator_permission``), limited to equipment
in their charge (``_get_equipment_ids_for_log_access``; the Main Administrator sees all), and never their own request.

Allocation (one transaction, rows locked): the chosen slots must be free, not in the past and not overlapping
another mode of the instrument (the urgent-allocation validator, which also flags weekends, holidays and
maintenance for the OIC to confirm); the amount is re-quoted and must match what the OIC saw; the external weekly
quota is checked without bypass; then a PENDING_PAYMENT booking is created on those slots with the payment
deadline. No wallet is debited.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import notifications
from .models import AllocationState, SpecialAllocation, SpecialRequestAction
from .special_payment import payment_deadline
from .special_requests import (
    OPEN_STATUSES,
    SpecialRequestError,
    _text,
    is_short_notice,
    quote,
    quote_payload,
    record,
    serialize,
    special_qs,
)


def _scope_ids(user):
    from iic_booking.equipment import api_views

    if not api_views.check_operator_permission(user):
        raise SpecialRequestError(
            "Only the Officer in Charge or the Main Administrator can review special requests.", status=403,
            code="forbidden",
        )
    return api_views._get_equipment_ids_for_log_access(user)


def _load(user, request_id, *, lock=False):
    allowed = _scope_ids(user)
    qs = special_qs().select_related("user", "equipment", "external_special")
    if lock:
        qs = qs.select_for_update(of=("self",))
    try:
        urg = qs.get(pk=int(request_id))
    except Exception:  # noqa: BLE001 - not found or not a number
        raise SpecialRequestError("Special request not found.", status=404, code="not_found")
    if allowed is not None and urg.equipment_id not in set(allowed):
        raise SpecialRequestError("Special request not found.", status=404, code="not_found")
    if urg.user_id == user.pk:
        raise SpecialRequestError("You cannot decide your own request.", status=403, code="own_request")
    return urg


def staff_list(user, *, status_filter: str = "") -> list[dict]:
    allowed = _scope_ids(user)
    qs = special_qs().select_related("user", "equipment", "external_special").order_by("-requested_at")
    if allowed is not None:
        qs = qs.filter(equipment_id__in=list(allowed))
    if status_filter:
        qs = qs.filter(status__in=[s.strip().upper() for s in status_filter.split(",") if s.strip()])
    return [serialize(u) | {"user_name": u.user.name if hasattr(u.user, "name") else u.user.email}
            for u in qs[:200]]


def staff_detail(user, request_id) -> dict:
    return serialize(_load(user, request_id), staff=True)


def clarify(user, request_id, message) -> dict:
    from iic_booking.equipment.models import UrgentBookingRequestStatus

    _load(user, request_id)  # authorise before validating the caller's input
    text = _text(message, "message", required=True)
    with transaction.atomic():
        urg = _load(user, request_id, lock=True)
        if urg.status not in OPEN_STATUSES:
            raise SpecialRequestError("Only an open request can be queried.", code="invalid_state")
        detail = urg.external_special
        detail.clarification_message = text
        detail.clarification_requested_at = timezone.now()
        detail.clarification_response, detail.clarification_responded_at = "", None
        detail.save(update_fields=["clarification_message", "clarification_requested_at", "clarification_response",
                                   "clarification_responded_at", "updated_at"])
        urg.status = UrgentBookingRequestStatus.CLARIFICATION
        urg.save(update_fields=["status"])
        record(urg, SpecialRequestAction.CLARIFICATION_REQUESTED, user, reason=text)
        notifications.notify(
            f"special:{urg.pk}:clarify:{detail.clarification_requested_at:%Y%m%d%H%M%S}", [urg.user],
            title=f"Question about your special request #{urg.pk}",
            message=f"The Officer in Charge needs more information about your request for {urg.equipment.name}: "
                    f"{text}",
            link=f"/external-special-requests?request={urg.pk}",
        )
    return serialize(urg, staff=True)


def reject(user, request_id, reason) -> dict:
    from iic_booking.equipment.models import UrgentBookingRequestStatus

    _load(user, request_id)  # authorise before validating the caller's input
    text = _text(reason, "reason", required=True)
    with transaction.atomic():
        urg = _load(user, request_id, lock=True)
        if urg.status not in OPEN_STATUSES:
            raise SpecialRequestError("Only an open request can be rejected.", code="invalid_state")
        urg.status = UrgentBookingRequestStatus.REJECTED
        urg.decided_at, urg.decided_by, urg.admin_notes = timezone.now(), user, text
        urg.save(update_fields=["status", "decided_at", "decided_by", "admin_notes"])
        record(urg, SpecialRequestAction.REJECTED, user, reason=text)
        notifications.notify(
            f"special:{urg.pk}:rejected", [urg.user],
            title=f"Special request #{urg.pk} not approved",
            message=f"Your special booking request for {urg.equipment.name} could not be approved. Reason: {text}",
            link=f"/external-special-requests?request={urg.pk}",
        )
    return serialize(urg, staff=True)


def candidate_slots(user, request_id, start: date, days: int = 7) -> dict:
    """Unbooked slots of the equipment from ``start`` for ``days`` days, with notes for weekends, holidays and
    non-available statuses (the OIC decides; the validator refuses booked or past slots)."""
    from iic_booking.equipment.models import DailySlot
    from iic_booking.equipment.slot_utils import SlotGenerator
    from iic_booking.equipment.urgent_allocation import holidays_for, slot_notes

    urg = _load(user, request_id)
    days = max(1, min(int(days or 7), 14))
    end = start + timedelta(days=days - 1)
    SlotGenerator.generate_slots_for_week(urg.equipment, start, end, allow_holiday=True)
    slots = list(
        DailySlot.objects.filter(slot_master__equipment=urg.equipment, date__gte=start, date__lte=end)
        .order_by("start_datetime")
    )
    holidays = holidays_for({s.date for s in slots if s.date})
    now = timezone.now()
    rows = []
    for s in slots:
        rows.append({
            "id": s.pk,
            "date": s.date.isoformat() if s.date else None,
            "start": timezone.localtime(s.start_datetime).isoformat() if s.start_datetime else None,
            "end": timezone.localtime(s.end_datetime).isoformat() if s.end_datetime else None,
            "status": s.status,
            "taken": bool(s.booking_id) or s.status == "BOOKED",
            "past": bool(s.end_datetime and s.end_datetime <= now),
            "notes": slot_notes(s, holidays),
        })
    return {"request": serialize(urg), "slots": rows, "required_slots": urg.slots_requested}


def _expected_total(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise SpecialRequestError("expected_total must be an amount.", field="expected_total")


def allocate(user, request_id, slot_ids, *, expected_total=None, note: str = "") -> dict:
    from iic_booking.equipment.booking_events import create_booking_event
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService
    from iic_booking.equipment.models import (
        Booking,
        BookingEventType,
        BookingStatus,
        DailySlot,
        SlotStatus,
        UrgentBookingRequestStatus,
    )
    from iic_booking.equipment.urgent_allocation import (
        UrgentAllocationError,
        _log_overridden_slot_statuses,
        _validate_slots,
    )

    _load(user, request_id)  # authorise before validating the caller's input
    expected = _expected_total(expected_total)
    note = _text(note, "note")
    with transaction.atomic():
        urg = _load(user, request_id, lock=True)
        if urg.status != UrgentBookingRequestStatus.PENDING:
            raise SpecialRequestError(
                "Only a pending request can be approved (answer any clarification first).", code="invalid_state"
            )
        if SpecialAllocation.objects.filter(request=urg).exists():
            raise SpecialRequestError("This request already has an allocation.", code="already_allocated")
        equipment = urg.equipment
        from iic_booking.training.authorisation import check_booking as authorisation_gate

        gate = authorisation_gate(urg.user, equipment, actor=user, context="special allocation")
        if gate is not None:
            raise SpecialRequestError(gate["error"], status=403, code=gate.get("code") or "authorisation_required")
        try:
            slots, warnings = _validate_slots(equipment, slot_ids)
        except UrgentAllocationError as exc:
            raise SpecialRequestError(exc.message, code=exc.code or "invalid_slots")
        if len(slots) < int(urg.slots_requested or 1):
            raise SpecialRequestError(
                f"The request needs {urg.slots_requested} slot(s); {len(slots)} chosen.", code="slots_too_few"
            )
        q = quote(equipment, urg.user, urg.input_values or {})
        if expected is not None and expected != q["total_charge"]:
            raise SpecialRequestError(
                f"The amount is now ₹{q['total_charge']:.2f} (you saw ₹{expected:.2f}). Check it and approve again.",
                code="amount_changed", extra={"quote": quote_payload(q)},
            )
        ids = [s.pk for s in slots]
        locked = list(
            DailySlot.objects.select_for_update()
            .filter(id__in=ids, slot_master__equipment=equipment, booking__isnull=True)
            .exclude(status=SlotStatus.BOOKED)
            .order_by("start_datetime")
        )
        if len(locked) != len(ids):
            raise SpecialRequestError("One or more slots were just booked. Choose other slots.", code="slots_taken")
        ext_quota = ExternalSlotQuotaService.validate_external_booking(
            urg.user, equipment, slot_dates=[s.date for s in locked if s.date], slots_requested=len(locked),
            bypass=False,
        )
        if not ext_quota.allowed:
            raise SpecialRequestError(
                ext_quota.message or "The external weekly quota for that week is used up.", code="external_quota",
                extra=ext_quota.as_error_payload(),
            )
        now = timezone.now()
        first_start = locked[0].start_datetime
        deadline = payment_deadline(now, first_start)
        if deadline <= now:
            raise SpecialRequestError("The first chosen slot starts now; choose a later slot.", code="slot_too_soon")
        total = q["total_charge"]
        booking = Booking.objects.create(
            user=urg.user,
            equipment=equipment,
            charge_profile=q["charge_profile"],
            user_type_snapshot=q["user_type"],
            total_time_minutes=q["required_minutes"],
            total_charge=total,
            amount_due=total,
            input_values=urg.input_values or {},
            selected_parameters=None,
            charge_breakdown=q["charge_breakdown"],
            status=BookingStatus.PENDING_PAYMENT,
            notes=f"Special request #{urg.pk}: slots allocated by staff. Payment due by "
                  f"{timezone.localtime(deadline):%d %b %Y %H:%M} IST.",
            settlement_department=getattr(equipment, "internal_department", None),
            created_by=user,
        )
        previous = {str(s.pk): s.status for s in locked}
        overridden = [s for s in locked if s.status != SlotStatus.AVAILABLE]
        DailySlot.objects.filter(pk__in=ids).update(booking=booking, status=SlotStatus.BOOKED)
        if overridden:
            _log_overridden_slot_statuses(equipment, overridden, user, urg.pk)
        short = is_short_notice(first_start, at=now)
        allocation = SpecialAllocation.objects.create(
            request=urg, booking=booking, approved_by=user, approved_at=now, slot_start=first_start,
            payment_deadline_at=deadline, previous_slot_statuses=previous, short_notice=short,
            state=AllocationState.AWAITING_PAYMENT,
        )
        detail = getattr(urg, "external_special", None)
        if short and detail is not None and detail.short_notice_ack_at and detail.short_notice_policy_id:
            # The user already accepted the current conditions with the request; carry it over.
            allocation.short_notice_ack_at = detail.short_notice_ack_at
            allocation.short_notice_policy_id = detail.short_notice_policy_id
            allocation.save(update_fields=["short_notice_ack_at", "short_notice_policy", "updated_at"])
        urg.status = UrgentBookingRequestStatus.APPROVED
        urg.decided_at, urg.decided_by = now, user
        urg.admin_notes = note
        urg.save(update_fields=["status", "decided_at", "decided_by", "admin_notes"])
        create_booking_event(
            booking=booking, event_type=BookingEventType.CREATED, created_by=user,
            comment=f"Allocated for external special request #{urg.pk}; awaiting payment.",
            new_status=BookingStatus.PENDING_PAYMENT,
            metadata={"special_request_id": urg.pk, "previous_slot_statuses": previous,
                      "payment_deadline_at": deadline.isoformat()},
            send_notification=False,
        )
        record(urg, SpecialRequestAction.ALLOCATED, user, reason=note, details={
            "booking_id": booking.pk, "slot_ids": ids, "first_slot_start": first_start.isoformat(),
            "payment_deadline_at": deadline.isoformat(), "amount": f"{total:.2f}", "short_notice": short,
            "warnings": warnings,
        })
        when = timezone.localtime(first_start).strftime("%d %b %Y %I:%M %p")
        due = timezone.localtime(deadline).strftime("%d %b %Y %I:%M %p")
        notifications.notify(
            f"special:{urg.pk}:allocated", [urg.user],
            title=f"Special request #{urg.pk} approved: pay by {due} IST",
            message=(
                f"Your special booking request for {equipment.name} was approved for {when} IST. "
                f"Pay ₹{total:.2f} by {due} IST to confirm it; if payment is not realised by then the slot is "
                "released. "
                + ("This slot is less than the short-notice period away: please read and accept the short-notice "
                   "conditions before paying." if short and allocation.short_notice_ack_at is None else "")
            ),
            link=f"/external-special-requests?request={urg.pk}",
        )
    urg.refresh_from_db()
    out = serialize(urg, staff=True)
    out["warnings"] = warnings
    return out
