"""External users' special booking requests (an ``UrgentBookingRequest`` of type ``EXTERNAL_SPECIAL``).

A special request asks for analysis on a date outside the ordinary external window (or when no slot is free). It
reserves nothing: the requested times are tentative, and an Officer in Charge or the Main Administrator later
allocates real slots (``special_staff``), after which the user must pay by the deadline (``special_payment``).

Rules enforced here (server-side; nothing is taken from the client except the user's own choices):
* only external users, only while ``special_requests_enabled``;
* the same portal locks, department switches and training authorisation as ordinary booking;
* at most ``max_open_special_requests`` open (pending / clarification) requests per user, counted under a row
  lock on the user so concurrent submissions cannot exceed it;
* the estimate is computed with the charge engine from the user's inputs (no surcharge unless switched on);
* a preferred start sooner than ``short_notice_days`` needs acknowledgement of the current short-notice wording,
  stored with its version, a hash of the text and the server time.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import notifications, policy
from .models import (
    ExternalServicesSettings,
    PolicyKey,
    SpecialRequestAction,
    SpecialRequestDetail,
    SpecialRequestEvent,
)
from .window import is_external_booking_user

MAX_ALTERNATIVES = 3
MAX_TEXT = 2000
MAX_DURATION = timedelta(hours=24)
OPEN_STATUSES = ("PENDING", "CLARIFICATION")


class SpecialRequestError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = "", extra=None):
        super().__init__(message)
        self.message, self.status, self.code, self.field = message, status, code, field
        self.extra = extra or {}

    def payload(self) -> dict:
        body = {"error": self.message, "code": self.code}
        if self.field:
            body["field"] = self.field
        body.update(self.extra)
        return body


def special_qs():
    from iic_booking.equipment.models import UrgentBookingRequest, UrgentBookingRequestType

    return UrgentBookingRequest.all_types.filter(request_type=UrgentBookingRequestType.EXTERNAL_SPECIAL)


def record(request, action, actor=None, reason: str = "", **details) -> SpecialRequestEvent:
    return SpecialRequestEvent.objects.create(
        request=request, action=action, actor=actor, reason=(reason or "")[:MAX_TEXT], details=details
    )


def is_short_notice(start, *, at=None, days: Optional[int] = None) -> bool:
    """Short notice when the slot starts less than ``days`` x 24 h after ``at`` (exactly ``days`` is not short
    notice). Durations are absolute, so the time zone does not change the result."""
    days = ExternalServicesSettings.load().short_notice_days if days is None else days
    return start - (at or timezone.now()) < timedelta(days=int(days))


# --- estimate -----------------------------------------------------------------------------------------------------

def quote(equipment, user, input_values: dict, *, total_time_minutes: Optional[int] = None) -> dict:
    """Required time and estimated amount from the user's inputs, with the user's own charge profile and GST for
    external users. The urgent surcharge is added only when the setting is on (policy decision)."""
    from iic_booking.equipment.calculators import (
        ChargeCalculationEngine,
        TimeCalculationEngine,
        apply_urgent_booking_surcharge,
        build_safe_input_values_for_charge_calculation,
        quantize_money,
    )
    from iic_booking.equipment.slot_allocation import slot_tolerance_minutes_for, slots_needed_for_analysis_time
    from iic_booking.equipment.urgent_allocation import _slot_duration
    from iic_booking.equipment.waitlist_booking import _get_external_gst_percent, _resolve_charge_profile_for_user

    charge_profile, user_type, is_external = _resolve_charge_profile_for_user(equipment, user)
    if not charge_profile:
        raise SpecialRequestError(
            f"No charge profile is set up for this equipment and user type {user_type}.", code="no_charge_profile"
        )
    safe = build_safe_input_values_for_charge_calculation(input_values or {}, equipment=equipment)
    slot_minutes = _slot_duration(equipment)
    try:
        required = int(TimeCalculationEngine.calculate_time(charge_profile, safe, slot_duration_minutes=slot_minutes))
    except Exception as exc:  # noqa: BLE001 - shown to the user as an input problem
        raise SpecialRequestError(f"The analysis time could not be worked out from your inputs ({exc}).")
    if required <= 0:
        raise SpecialRequestError("Your inputs give no analysis time. Check the sample details.", field="input_values")
    minutes = int(total_time_minutes) if total_time_minutes is not None else required
    try:
        base, breakdown = ChargeCalculationEngine.calculate_charge(charge_profile, safe, minutes, selected_parameters=None)
    except Exception as exc:  # noqa: BLE001
        raise SpecialRequestError(f"The charge could not be worked out from your inputs ({exc}).")
    total = quantize_money(base)
    breakdown = list(breakdown)
    surcharge = Decimal("0.00")
    if ExternalServicesSettings.load().special_request_surcharge:
        total, breakdown, surcharge = apply_urgent_booking_surcharge(total, breakdown)
    gst_percent, gst_amount = Decimal("0"), Decimal("0.00")
    if is_external:
        gst_percent = Decimal(str(_get_external_gst_percent() or 0))
        if gst_percent > 0:
            gst_amount = quantize_money(total * gst_percent / Decimal("100"))
            total = quantize_money(total + gst_amount)
            breakdown.append({"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)})
    return {
        "charge_profile": charge_profile,
        "user_type": user_type,
        "required_minutes": required,
        "required_slots": slots_needed_for_analysis_time(required, slot_minutes, slot_tolerance_minutes_for(equipment)),
        "slot_duration_minutes": slot_minutes,
        "total_charge": quantize_money(total),
        "charge_breakdown": breakdown,
        "surcharge_amount": surcharge,
        "gst_percent": float(gst_percent),
        "gst_amount": gst_amount,
    }


def quote_payload(q: dict) -> dict:
    return {
        "required_minutes": q["required_minutes"],
        "required_slots": q["required_slots"],
        "slot_duration_minutes": q["slot_duration_minutes"],
        "estimated_total": f"{q['total_charge']:.2f}",
        "surcharge_amount": f"{q['surcharge_amount']:.2f}",
        "gst_percent": q["gst_percent"],
        "gst_amount": f"{q['gst_amount']:.2f}",
        "charge_breakdown": q["charge_breakdown"],
        "is_estimate": True,
    }


# --- validation -------------------------------------------------------------------------------------------------------

def _require_enabled():
    if not ExternalServicesSettings.load().special_requests_enabled:
        raise SpecialRequestError("Special booking requests are not available at the moment.", status=403,
                                  code="special_requests_disabled")


def _require_external(user):
    if not is_external_booking_user(user):
        raise SpecialRequestError("Special booking requests are for external users.", status=403, code="not_external")


def _equipment(equipment_id):
    from iic_booking.equipment.models import Equipment

    try:
        return Equipment.objects.get(pk=int(equipment_id))
    except (TypeError, ValueError, Equipment.DoesNotExist):
        raise SpecialRequestError("Choose an equipment.", field="equipment_id", status=404, code="not_found")


def _gates(user, equipment):
    """The same gates an ordinary booking passes (portal lock, department switch, training authorisation)."""
    from iic_booking.users.legacy_ledger.booking_lock import booking_is_locked, department_equipment_booking_blocked

    locked, message = booking_is_locked(user)
    if locked:
        raise SpecialRequestError(message or "Booking is not open on the portal.", status=403, code="portal_locked")
    blocked, message = department_equipment_booking_blocked(equipment, user)
    if blocked:
        raise SpecialRequestError(message or "Booking is not available for this equipment.", status=403,
                                  code="department_blocked")
    if str(getattr(equipment, "status", "")).upper() != "ACTIVE":
        raise SpecialRequestError("This equipment is not available for booking.", status=400, code="equipment_inactive")
    from iic_booking.training.authorisation import check_booking as authorisation_gate

    gate = authorisation_gate(user, equipment, actor=user, context="special request")
    if gate is not None:
        raise SpecialRequestError(gate["error"], status=403, code=gate.get("code") or "authorisation_required")


def _parse_window(start_raw, end_raw, *, field: str, now) -> tuple:
    start = parse_datetime(str(start_raw or "")) if start_raw else None
    end = parse_datetime(str(end_raw or "")) if end_raw else None
    if start is None or end is None:
        raise SpecialRequestError("Give a start and an end date and time.", field=field)
    tz = timezone.get_current_timezone()
    if timezone.is_naive(start):
        start = timezone.make_aware(start, tz)
    if timezone.is_naive(end):
        end = timezone.make_aware(end, tz)
    if end <= start:
        raise SpecialRequestError("The end must be after the start.", field=field)
    if end - start > MAX_DURATION:
        raise SpecialRequestError("A requested time can span at most 24 hours.", field=field)
    if start <= now:
        raise SpecialRequestError("Choose a time in the future.", field=field)
    if start > now + timedelta(days=366):
        raise SpecialRequestError("Choose a time within the next year.", field=field)
    return start, end


def _alternatives(raw, now) -> list[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise SpecialRequestError("Alternatives must be a list.", field="alternatives")
    if len(raw) > MAX_ALTERNATIVES:
        raise SpecialRequestError(f"Give at most {MAX_ALTERNATIVES} alternatives.", field="alternatives")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise SpecialRequestError("Each alternative needs a start and an end.", field="alternatives")
        start, end = _parse_window(item.get("start"), item.get("end"), field="alternatives", now=now)
        out.append({"start": timezone.localtime(start).isoformat(), "end": timezone.localtime(end).isoformat()})
    return out


def _text(value, field, *, required=False) -> str:
    text = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    if len(text) > MAX_TEXT:
        raise SpecialRequestError(f"Keep {field.replace('_', ' ')} under {MAX_TEXT} characters.", field=field)
    if required and not text:
        raise SpecialRequestError(f"Fill in the {field.replace('_', ' ')}.", field=field)
    return text


def _positive_int(value, field, default=1, hi=1000) -> int:
    try:
        number = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        raise SpecialRequestError(f"{field.replace('_', ' ').capitalize()} must be a whole number.", field=field)
    if not 1 <= number <= hi:
        raise SpecialRequestError(f"{field.replace('_', ' ').capitalize()} must be between 1 and {hi}.", field=field)
    return number


def _clean_inputs(equipment, user, data) -> dict:
    from iic_booking.equipment.urgent_allocation import UrgentAllocationError, clean_request_input_values

    try:
        return clean_request_input_values(
            equipment, user, data.get("input_values"), selected_parameters=data.get("selected_parameters")
        )
    except UrgentAllocationError as exc:
        raise SpecialRequestError(exc.message, field="input_values", code=exc.code or "invalid")


# --- user actions -----------------------------------------------------------------------------------------------------

def preview(user, data: dict) -> dict:
    """Estimate and short-notice check without saving anything."""
    _require_enabled()
    _require_external(user)
    equipment = _equipment(data.get("equipment_id"))
    now = timezone.now()
    start, end = _parse_window(data.get("preferred_start"), data.get("preferred_end"), field="preferred_start", now=now)
    inputs = _clean_inputs(equipment, user, data)
    q = quote(equipment, user, inputs)
    short = is_short_notice(start, at=now)
    return {
        "quote": quote_payload(q),
        "short_notice": short,
        "short_notice_policy": policy.policy_dict(policy.current(PolicyKey.SHORT_NOTICE)) if short else None,
        "short_notice_days": ExternalServicesSettings.load().short_notice_days,
        "preferred_start": timezone.localtime(start).isoformat(),
        "preferred_end": timezone.localtime(end).isoformat(),
    }


def create(user, data: dict):
    """Submit a special request. Returns the ``UrgentBookingRequest``."""
    from iic_booking.equipment.models import UrgentBookingRequestStatus, UrgentBookingRequestType

    _require_enabled()
    _require_external(user)
    equipment = _equipment(data.get("equipment_id"))
    _gates(user, equipment)
    now = timezone.now()
    start, end = _parse_window(data.get("preferred_start"), data.get("preferred_end"), field="preferred_start", now=now)
    alternatives = _alternatives(data.get("alternatives"), now)
    samples = _positive_int(data.get("number_of_samples"), "number_of_samples")
    sample_description = _text(data.get("sample_description"), "sample_description", required=True)
    remarks = _text(data.get("remarks"), "remarks")
    inputs = _clean_inputs(equipment, user, data)
    q = quote(equipment, user, inputs)
    settings_row = ExternalServicesSettings.load()
    short = is_short_notice(start, at=now, days=settings_row.short_notice_days)
    ack_policy = None
    if short:
        try:
            ack_policy = policy.require_acknowledgement(PolicyKey.SHORT_NOTICE, data.get("short_notice_ack_version"))
        except policy.PolicyError as exc:
            raise SpecialRequestError(exc.message, code=exc.code, field=exc.field, extra=exc.extra)

    with transaction.atomic():
        # Serialise this user's submissions so the open-request limit cannot be exceeded concurrently.
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        open_count = special_qs().filter(user=user, status__in=OPEN_STATUSES).count()
        if open_count >= settings_row.max_open_special_requests:
            raise SpecialRequestError(
                f"You already have {open_count} open special request(s). Wait for a decision or withdraw one first.",
                status=429, code="too_many_open_requests",
            )
        urg = special_qs().model.all_types.create(
            user=user,
            equipment=equipment,
            request_type=UrgentBookingRequestType.EXTERNAL_SPECIAL,
            disclaimer_accepted=bool(ack_policy),
            number_of_samples=samples,
            slots_requested=q["required_slots"],
            duration_minutes=q["required_minutes"],
            requires_slot_allocation=True,
            input_values=inputs,
            estimated_charge=q["total_charge"],
            estimated_charge_breakdown=q["charge_breakdown"],
            preferred_schedule=(
                f"{timezone.localtime(start):%d %b %Y %H:%M} to {timezone.localtime(end):%d %b %Y %H:%M} (IST)"
            ),
            reviewer_comment=remarks,
            status=UrgentBookingRequestStatus.PENDING,
        )
        SpecialRequestDetail.objects.create(
            request=urg,
            preferred_start=start,
            preferred_end=end,
            alternatives=alternatives,
            sample_description=sample_description,
            remarks=remarks,
            short_notice=short,
            short_notice_policy=ack_policy,
            short_notice_text_sha256=policy.text_sha256(ack_policy.text) if ack_policy else "",
            short_notice_ack_at=now if ack_policy else None,
        )
        record(urg, SpecialRequestAction.SUBMITTED, user, details={
            "preferred_start": timezone.localtime(start).isoformat(),
            "estimated_total": f"{q['total_charge']:.2f}",
            "short_notice": short,
            "short_notice_policy_version": ack_policy.version if ack_policy else None,
        })
        if ack_policy:
            record(urg, SpecialRequestAction.SHORT_NOTICE_ACKNOWLEDGED, user,
                   details={"policy_version": ack_policy.version, "stage": "request"})
        _notify_submitted(urg)
    return urg


def _owned(user, request_id):
    try:
        urg = special_qs().select_related("equipment", "external_special").get(pk=int(request_id))
    except Exception:  # noqa: BLE001 - not found or not a number: same answer, no existence leak
        raise SpecialRequestError("Special request not found.", status=404, code="not_found")
    if urg.user_id != user.pk:
        raise SpecialRequestError("Special request not found.", status=404, code="not_found")
    return urg


def respond(user, request_id, message) -> Any:
    """The user answers a clarification request; the request goes back to the Officer in Charge."""
    from iic_booking.equipment.models import UrgentBookingRequestStatus

    urg = _owned(user, request_id)  # ownership before validating the caller's input
    text = _text(message, "message", required=True)
    with transaction.atomic():
        urg = special_qs().select_for_update().get(pk=urg.pk)
        if urg.status != UrgentBookingRequestStatus.CLARIFICATION:
            raise SpecialRequestError("No clarification was asked for on this request.", code="invalid_state")
        detail = urg.external_special
        detail.clarification_response = text
        detail.clarification_responded_at = timezone.now()
        detail.save(update_fields=["clarification_response", "clarification_responded_at", "updated_at"])
        urg.status = UrgentBookingRequestStatus.PENDING
        urg.save(update_fields=["status"])
        record(urg, SpecialRequestAction.CLARIFIED, user, reason=text)
        notifications.notify(
            f"special:{urg.pk}:clarified:{detail.clarification_responded_at:%Y%m%d%H%M%S}", staff_for(urg.equipment),
            title=f"Special request #{urg.pk}: reply received",
            message=f"The user replied to your question on special request #{urg.pk} ({urg.equipment.name}).",
            link=f"/external-special-requests/manage?request={urg.pk}",
        )
    return urg


def withdraw(user, request_id, reason: str = "") -> Any:
    from iic_booking.equipment.models import UrgentBookingRequestStatus

    with transaction.atomic():
        urg = _owned(user, request_id)
        urg = special_qs().select_for_update().get(pk=urg.pk)
        if urg.status not in OPEN_STATUSES:
            raise SpecialRequestError("Only an open request can be withdrawn.", code="invalid_state")
        urg.status = UrgentBookingRequestStatus.REJECTED
        urg.decided_at = timezone.now()
        urg.admin_notes = "Withdrawn by the user."
        urg.save(update_fields=["status", "decided_at", "admin_notes"])
        urg.external_special.withdrawn_at = urg.decided_at
        urg.external_special.save(update_fields=["withdrawn_at", "updated_at"])
        record(urg, SpecialRequestAction.WITHDRAWN, user, reason=_text(reason, "reason"))
    return urg


# --- reading ----------------------------------------------------------------------------------------------------------

def staff_for(equipment) -> list:
    """Officers in Charge of the equipment (the same helper the urgent-request alerts use); Main Administrators
    when the equipment has none."""
    from iic_booking.equipment.reports import get_equipment_oic_users
    from iic_booking.users.models.user_type import UserType

    people = list(get_equipment_oic_users(equipment))
    if not people:
        people = list(get_user_model().objects.filter(user_type=UserType.ADMIN, is_active=True)[:5])
    return [p for p in people if p is not None and p.is_active]


def _notify_submitted(urg) -> None:
    notifications.notify(
        f"special:{urg.pk}:submitted", [urg.user],
        title=f"Special booking request #{urg.pk} received",
        message=(
            f"We received your special booking request for {urg.equipment.name}. This is a request, not a "
            "confirmed booking: the Officer in Charge will review it and allocate a slot if one can be offered."
        ),
        link=f"/external-special-requests?request={urg.pk}",
    )
    notifications.notify(
        f"special:{urg.pk}:submitted:staff", staff_for(urg.equipment),
        title=f"New special booking request #{urg.pk}",
        message=f"An external user requested {urg.equipment.name}. Review it and allocate a slot or reply.",
        link=f"/external-special-requests/manage?request={urg.pk}",
    )


def serialize(urg, *, staff: bool = False) -> dict:
    from iic_booking.users.display import get_user_display_name

    detail = getattr(urg, "external_special", None)
    allocation = getattr(urg, "external_allocation", None)

    def iso(v):
        return timezone.localtime(v).isoformat() if v else None

    out = {
        "id": urg.pk,
        "status": urg.status,
        "status_label": urg.get_status_display(),
        "equipment": {"id": urg.equipment_id, "name": urg.equipment.name, "code": urg.equipment.code},
        "requested_at": iso(urg.requested_at),
        "number_of_samples": urg.number_of_samples,
        "required_minutes": urg.duration_minutes,
        "required_slots": urg.slots_requested,
        "estimated_total": f"{urg.estimated_charge:.2f}" if urg.estimated_charge is not None else None,
        "estimated_breakdown": urg.estimated_charge_breakdown or [],
        "input_values": urg.input_values or {},
        "decided_at": iso(urg.decided_at),
        "decision_note": urg.admin_notes or "",
    }
    if detail is not None:
        out.update({
            "preferred_start": iso(detail.preferred_start),
            "preferred_end": iso(detail.preferred_end),
            "alternatives": detail.alternatives,
            "sample_description": detail.sample_description,
            "remarks": detail.remarks,
            "short_notice": detail.short_notice,
            "short_notice_acknowledged_at": iso(detail.short_notice_ack_at),
            "short_notice_policy_version": detail.short_notice_policy.version if detail.short_notice_policy else None,
            "clarification_message": detail.clarification_message,
            "clarification_requested_at": iso(detail.clarification_requested_at),
            "clarification_response": detail.clarification_response,
            "clarification_responded_at": iso(detail.clarification_responded_at),
            "withdrawn_at": iso(detail.withdrawn_at),
        })
    if allocation is not None:
        from .special_payment import allocation_payload

        out["allocation"] = allocation_payload(allocation)
    if staff:
        out["user"] = {
            "id": urg.user_id,
            "name": get_user_display_name(urg.user),
            "email": urg.user.email,
            "user_type": urg.user.user_type,
        }
        out["history"] = [
            {"action": e.action, "action_label": e.get_action_display(), "reason": e.reason,
             "actor": get_user_display_name(e.actor) if e.actor_id else None, "at": iso(e.created_at),
             "details": e.details}
            for e in SpecialRequestEvent.objects.filter(request=urg).select_related("actor")
        ]
    return out


def my_requests(user) -> list[dict]:
    qs = special_qs().filter(user=user).select_related("equipment", "external_special").order_by("-requested_at")
    return [serialize(u) for u in qs[:100]]


def get_mine(user, request_id) -> dict:
    return serialize(_owned(user, request_id))
