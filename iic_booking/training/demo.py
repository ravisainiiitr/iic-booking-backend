"""
Faculty demonstration requests.

SUBMITTED → UNDER_REVIEW (OIC opened it) → one of:
  APPROVED (as requested, or curtailed — curtailment is final, the faculty member is notified)
  PROPOSED_ALTERNATIVE → faculty ACCEPT → APPROVED | COUNTER (once) → UNDER_REVIEW | DECLINE → WITHDRAWN
                       → EXPIRED after the policy's working days
  REJECTED
APPROVED → SCHEDULED (slots reserved) → COMPLETED | NO_SHOW;  APPROVED/SCHEDULED → CANCELLED
SUBMITTED/UNDER_REVIEW → WITHDRAWN (faculty)

Charges (see ``charges``): every demonstration is charged at the equipment's internal IITR rate for the
approved duration (course/curricular ones are free only while the Main Admin's "Course/curricular
demonstrations are free" switch is on). The amount is deducted from the wallet the faculty member's bookings
use when the request becomes APPROVED (OIC approval, or the faculty accepting a proposed time); nothing is
charged before that, so rejected, withdrawn or expired requests cost nothing and curtailment lowers the charge.
Refund is 100% when IIC cancels; faculty cancellations follow the policy windows (default 100% ≥7 days,
50% ≥2 days).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.communication.email_branding import format_inr
from iic_booking.users.display import get_user_display_name

from . import access, charges, notify
from .audit import audit
from .errors import TrainingError
from .models import (
    AttendanceStatus,
    ChargeMode,
    CurtailReason,
    DemoPurpose,
    DemoRequest,
    DemoRequestRevision,
    DemoStatus,
    EventKind,
    EventStatus,
    Registration,
    RegistrationSource,
    RegistrationStatus,
    SelectionMode,
    SessionStatus,
    SessionType,
    TrainingEvent,
    TrainingSession,
)
from .policy import add_working_days, effective_policy, working_days_between
from .slots import ReservationError, demo_label, release_session_slots, reserve_session_slots

OPEN_FOR_DECISION = (DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW)
INBOX_STATUSES = (DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW, DemoStatus.PROPOSED_ALTERNATIVE, DemoStatus.APPROVED)
SNAPSHOT_FIELDS = (
    "status",
    "approved_duration_minutes",
    "approved_participants",
    "approved_start_at",
    "approved_end_at",
    "proposed_start_at",
    "proposed_end_at",
    "curtailed",
    "curtail_reason_code",
    "charge_mode",
    "rate_per_hour",
    "charge_amount",
    "refund_amount",
    "preferred_windows",
)


def _snap(req: DemoRequest) -> dict:
    out = {}
    for f in SNAPSHOT_FIELDS:
        v = getattr(req, f)
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = str(v)
        out[f] = v
    return out


def _revise(req: DemoRequest, actor, action: str, before: dict, *, reason_code: str = "", reason: str = "") -> None:
    after = _snap(req)
    DemoRequestRevision.objects.create(
        request=req,
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        from_status=before.get("status", ""),
        to_status=after["status"],
        before={k: v for k, v in before.items() if after.get(k) != v or k == "status"},
        after={k: v for k, v in after.items() if before.get(k) != v or k == "status"},
        reason_code=reason_code or "",
        reason=reason or "",
    )
    audit(actor, f"demo.{action}", req, before=before, after=after, note=reason)


def _parse_dt(value, field: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_datetime(str(value or "").strip())
    if dt is None:
        raise TrainingError(f"{field} must be an ISO date-time.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _parse_windows(raw) -> list[dict]:
    windows = []
    for item in (raw or [])[:3]:
        start = _parse_dt(item.get("start"), "Preferred window start")
        end = _parse_dt(item.get("end"), "Preferred window end")
        if end <= start:
            raise TrainingError("Each preferred window must end after it starts.")
        if start <= timezone.now():
            raise TrainingError("Preferred windows must be in the future.")
        windows.append({"start": start.isoformat(), "end": end.isoformat()})
    return windows


def _faculty_name(user) -> str:
    return get_user_display_name(user)


def _course_text(req: DemoRequest) -> str:
    parts = [p for p in (req.course_code, req.course_name) if p]
    if parts:
        return " ".join(parts)
    return dict(DemoPurpose.choices).get(req.purpose, "Demonstration")


def charge_text(req: DemoRequest) -> str:
    duration = req.approved_duration_minutes or req.requested_duration_minutes
    if req.charge_mode != ChargeMode.WALLET or not req.charge_amount:
        return "No charge"
    basis = f"internal IITR rate, {notify.fmt_minutes(duration)}"
    if req.wallet_txn_id:
        text = f"{format_inr(req.charge_amount)} ({basis}) deducted from the faculty member's {charges.wallet_label(req.equipment)}"
    else:
        text = f"{format_inr(req.charge_amount)} ({basis}), deducted from the faculty member's wallet when approved"
    if req.refund_amount:
        text += f"; {format_inr(req.refund_amount)} refunded"
    return text


def _context(req: DemoRequest, **extra) -> dict:
    duration = req.approved_duration_minutes or req.requested_duration_minutes
    participants = req.approved_participants or req.participants_requested
    ctx = {
        "reference": req.reference,
        "equipment_name": req.equipment.name,
        "equipment_code": req.equipment.code,
        "title": f"{_course_text(req)} · {_faculty_name(req.requester)}",
        "when": notify.fmt_window(req.approved_start_at or req.proposed_start_at, req.approved_end_at or req.proposed_end_at),
        "duration": notify.fmt_minutes(duration),
        "participants": str(participants),
        "status": req.get_status_display(),
        "charge": charge_text(req),
        "remarks": req.oic_remarks,
    }
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Faculty actions
# ---------------------------------------------------------------------------
def create_request(faculty, data: dict) -> DemoRequest:
    from iic_booking.equipment.models import Equipment

    if not access.is_faculty(faculty):
        raise TrainingError("Only faculty can request a demonstration.", status=403, code="forbidden")
    try:
        equipment = Equipment.objects.select_related("internal_department").get(pk=data.get("equipment_id"))
    except (Equipment.DoesNotExist, ValueError, TypeError):
        raise TrainingError("Equipment not found.", status=404) from None
    if not access.equipment_in_pilot(equipment):
        raise TrainingError("Demonstration requests are not open for this equipment yet.", code="not_in_pilot")
    policy = effective_policy(equipment)
    purpose = data.get("purpose") or DemoPurpose.COURSE
    if purpose not in DemoPurpose.values:
        raise TrainingError("Invalid purpose.")
    try:
        duration = int(data.get("requested_duration_minutes") or 0)
        participants = int(data.get("participants_requested") or 0)
    except (TypeError, ValueError):
        raise TrainingError("Duration and number of participants must be numbers.") from None
    if duration < 15:
        raise TrainingError("Requested duration must be at least 15 minutes.")
    if policy.demo_max_minutes and duration > policy.demo_max_minutes:
        raise TrainingError(
            f"Demonstrations are limited to {notify.fmt_minutes(policy.demo_max_minutes)} by policy.", code="over_max_duration"
        )
    if participants < 1:
        raise TrainingError("Number of students must be at least 1.")
    if purpose == DemoPurpose.COURSE and not (data.get("course_code") or data.get("course_name")):
        raise TrainingError("Course code or name is required for a course demonstration.")
    windows = _parse_windows(data.get("preferred_windows"))
    if not windows:
        raise TrainingError("Give at least one preferred window.")
    quote = charges.quote(equipment, faculty, purpose=purpose, minutes=duration)
    if quote["chargeable"] and not data.get("charge_acknowledged"):
        raise TrainingError("Please acknowledge the demonstration charge.", code="charge_ack_required")
    if quote["balance_error"]:
        raise TrainingError(quote["balance_error"], code="insufficient_balance")
    participant_ids = [int(x) for x in (data.get("participant_user_ids") or []) if str(x).isdigit()]
    if participant_ids:
        allowed = access.faculty_group_student_ids(faculty)
        if not set(participant_ids) <= allowed:
            raise TrainingError("Participants must be students in your group.")
    with transaction.atomic():
        req = DemoRequest.objects.create(
            requester=faculty,
            equipment=equipment,
            purpose=purpose,
            course_code=(data.get("course_code") or "").strip()[:50],
            course_name=(data.get("course_name") or "").strip()[:255],
            participants_requested=participants,
            participant_list_text=(data.get("participant_list_text") or "").strip(),
            preferred_windows=windows,
            requested_duration_minutes=duration,
            notes=(data.get("notes") or "").strip(),
            charge_acknowledged=bool(data.get("charge_acknowledged")),
            charge_mode=ChargeMode.WALLET if quote["chargeable"] else ChargeMode.FREE,
            rate_per_hour=Decimal(quote["rate_per_hour"] or "0"),
            charge_amount=Decimal(quote["amount"] or "0"),
            status=DemoStatus.SUBMITTED,
        )
        if participant_ids:
            req.participant_users.set(participant_ids)
        _revise(req, faculty, "submitted", {"status": ""})
    notify.send(
        "demo_request_submitted_oic_email",
        _oic_recipients(equipment),
        context=_context(req, summary=f"{_faculty_name(faculty)} requested a demonstration on {equipment.name}."),
        title=f"Demo request {req.reference}",
        message=f"{_faculty_name(faculty)} requested a demonstration on {equipment.name}.",
        path=f"/training/oic?tab=requests&request={req.pk}",
        actor=faculty,
        event="training.demo.submitted",
    )
    return req


def withdraw(req: DemoRequest, faculty, reason: str = "") -> DemoRequest:
    _require_owner(req, faculty)
    with transaction.atomic():
        req = _lock(req)
        if req.status not in OPEN_FOR_DECISION:
            raise TrainingError("Only requests that are not yet decided can be withdrawn.")
        before = _snap(req)
        req.status = DemoStatus.WITHDRAWN
        req.save(update_fields=["status", "updated_at"])
        _revise(req, faculty, "withdrawn", before, reason=reason)
    return req


def respond(req: DemoRequest, faculty, *, response: str, windows=None, note: str = "") -> DemoRequest:
    _require_owner(req, faculty)
    response = (response or "").lower()
    if response not in ("accept", "counter", "decline"):
        raise TrainingError("Response must be accept, counter or decline.")
    schedule_error = None
    with transaction.atomic():
        req = _lock(req)
        if req.status != DemoStatus.PROPOSED_ALTERNATIVE:
            raise TrainingError("There is no proposed time to respond to.")
        if req.proposal_expires_at and req.proposal_expires_at < timezone.now():
            raise TrainingError("The proposed time has expired.", code="proposal_expired")
        before = _snap(req)
        req.faculty_response = (note or "").strip()
        if response == "accept":
            req.approved_start_at = req.proposed_start_at
            req.approved_end_at = req.proposed_end_at
            req.status = DemoStatus.APPROVED
            req.save()
            _debit(req)
            _revise(req, faculty, "accepted_proposal", before, reason=note)
            before_schedule = _snap(req)
            try:
                with transaction.atomic():
                    _schedule(req, req.approved_start_at, actor=faculty)
                    _revise(req, faculty, "scheduled", before_schedule)
            except (ReservationError, TrainingError) as exc:
                schedule_error = getattr(exc, "message", str(exc))
                req.refresh_from_db()
        elif response == "counter":
            if req.counter_used:
                raise TrainingError("You can counter a proposed time only once. Accept or decline it.")
            req.preferred_windows = _parse_windows(windows)
            if not req.preferred_windows:
                raise TrainingError("Give at least one window for your counter-proposal.")
            req.counter_used = True
            req.status = DemoStatus.UNDER_REVIEW
            req.proposed_start_at = req.proposed_end_at = req.proposal_expires_at = None
            req.save()
            _revise(req, faculty, "countered", before, reason=note)
        else:
            req.status = DemoStatus.WITHDRAWN
            req.save()
            _revise(req, faculty, "declined_proposal", before, reason=note)
    paid = f" ({format_inr(req.charge_amount)} deducted from their wallet)" if req.wallet_txn_id and response == "accept" else ""
    summary = {
        "accept": "accepted the proposed time" + paid + (" — please schedule it, the slots are no longer free" if schedule_error else ""),
        "counter": "countered the proposed time with new windows",
        "decline": "declined the proposed time and withdrew the request",
    }[response]
    notify.send(
        "demo_request_response_oic_email",
        _oic_recipients(req.equipment),
        context=_context(req, summary=f"{_faculty_name(faculty)} {summary}.", remarks=note or schedule_error or ""),
        title=f"Demo {req.reference}: faculty {response}ed",
        message=f"{_faculty_name(faculty)} {summary}.",
        path=f"/training/oic?tab=requests&request={req.pk}",
        actor=faculty,
        event=f"training.demo.{response}",
    )
    if req.status == DemoStatus.SCHEDULED:
        _notify_scheduled(req, faculty)
    return req


# ---------------------------------------------------------------------------
# OIC actions
# ---------------------------------------------------------------------------
def mark_viewed(req: DemoRequest, actor) -> None:
    if req.status != DemoStatus.SUBMITTED or not access.can_manage_equipment(actor, req.equipment_id):
        return
    with transaction.atomic():
        req = _lock(req)
        if req.status != DemoStatus.SUBMITTED:
            return
        before = _snap(req)
        req.status = DemoStatus.UNDER_REVIEW
        req.save(update_fields=["status", "updated_at"])
        _revise(req, actor, "viewed", before)


def decide(req: DemoRequest, actor, data: dict) -> DemoRequest:
    if not access.can_manage_equipment(actor, req.equipment_id):
        raise TrainingError("Only the equipment's OIC can decide this request.", status=403, code="forbidden")
    action = (data.get("action") or "").lower()
    if action not in ("approve", "propose", "reject"):
        raise TrainingError("Action must be approve, propose or reject.")
    remarks = (data.get("remarks") or "").strip()
    with transaction.atomic():
        req = _lock(req)
        if req.status not in OPEN_FOR_DECISION:
            raise TrainingError(f"This request is {req.get_status_display().lower()} and cannot be decided now.")
        before = _snap(req)
        policy = effective_policy(req.equipment)
        if action == "reject":
            if not remarks:
                raise TrainingError("Give a reason for rejecting.")
            req.status = DemoStatus.REJECTED
            req.oic_remarks = remarks
            _stamp(req, actor)
            req.save()
            _revise(req, actor, "rejected", before, reason=remarks)
        else:
            duration, participants, curtailed = _approved_size(req, data)
            reason_code = (data.get("reason_code") or "").strip().upper()
            if curtailed and reason_code not in CurtailReason.values:
                raise TrainingError("A reason is required when curtailing duration or participants.", code="reason_required")
            req.approved_duration_minutes = duration
            req.approved_participants = participants
            req.curtailed = curtailed
            req.curtail_reason_code = reason_code if curtailed else ""
            req.oic_remarks = remarks
            _apply_charge(req, data, duration)
            _stamp(req, actor)
            if action == "approve":
                req.status = DemoStatus.APPROVED
                req.save()
                _debit(req)
                _revise(req, actor, "approved_curtailed" if curtailed else "approved", before, reason_code=req.curtail_reason_code, reason=remarks)
                if data.get("start_at"):
                    start = _parse_dt(data.get("start_at"), "start_at")
                    scheduled_before = _snap(req)
                    _schedule(req, start, actor=actor)
                    _revise(req, actor, "scheduled", scheduled_before)
            else:
                start = _parse_dt(data.get("start_at"), "Proposed start")
                if start <= timezone.now():
                    raise TrainingError("The proposed time must be in the future.")
                req.proposed_start_at = start
                req.proposed_end_at = start + timedelta(minutes=duration)
                req.proposal_expires_at = add_working_days(timezone.now(), policy.proposal_expiry_working_days)
                req.status = DemoStatus.PROPOSED_ALTERNATIVE
                req.save()
                _revise(req, actor, "proposed", before, reason_code=req.curtail_reason_code, reason=remarks)
    _notify_decision(req, actor, action)
    if req.status == DemoStatus.SCHEDULED:
        _notify_scheduled(req, actor)
    return req


def schedule(req: DemoRequest, actor, start_at) -> DemoRequest:
    if not access.can_manage_equipment(actor, req.equipment_id):
        raise TrainingError("Only the equipment's OIC can schedule this request.", status=403, code="forbidden")
    start = _parse_dt(start_at, "start_at")
    with transaction.atomic():
        req = _lock(req)
        if req.status not in (DemoStatus.APPROVED, DemoStatus.SCHEDULED):
            raise TrainingError("Only approved requests can be scheduled.")
        before = _snap(req)
        _schedule(req, start, actor=actor)
        _revise(req, actor, "rescheduled" if before["status"] == DemoStatus.SCHEDULED else "scheduled", before)
    _notify_scheduled(req, actor)
    return req


def cancel(req: DemoRequest, actor, reason: str = "") -> DemoRequest:
    is_owner = req.requester_id == getattr(actor, "id", None)
    is_staff = access.can_manage_equipment(actor, req.equipment_id)
    if not (is_owner or is_staff):
        raise TrainingError("You cannot cancel this request.", status=403, code="forbidden")
    reason = (reason or "").strip()
    if is_staff and not is_owner and not reason:
        raise TrainingError("Give a reason for cancelling.")
    with transaction.atomic():
        req = _lock(req)
        if req.status not in (DemoStatus.APPROVED, DemoStatus.SCHEDULED, DemoStatus.PROPOSED_ALTERNATIVE):
            raise TrainingError("Only approved, scheduled or proposed requests can be cancelled.")
        before = _snap(req)
        side = "FACULTY" if is_owner and not is_staff else "IIC"
        _release(req, actor, note=f"Demo cancelled by {side.lower()}")
        refund = refund_amount_for(req, side)
        _refund(req, refund)
        req.status = DemoStatus.CANCELLED
        req.cancelled_by_side = side
        req.cancel_reason = reason
        req.save()
        if req.event_id:
            TrainingEvent.objects.filter(pk=req.event_id).update(
                status=EventStatus.CANCELLED, cancelled_at=timezone.now(), cancelled_reason=reason[:2000]
            )
        _revise(req, actor, "cancelled", before, reason=reason)
    refund_text = _refund_sentence(req)
    recipients = [req.requester] + _oic_recipients(req.equipment)
    notify.send(
        "demo_request_cancelled_email",
        recipients,
        context=_context(req, summary=f"Demo request {req.reference} was cancelled by {'IIC' if req.cancelled_by_side == 'IIC' else 'the faculty member'}.{refund_text}", remarks=reason),
        title=f"Demo {req.reference} cancelled",
        message=f"Demo {req.reference} on {req.equipment.name} was cancelled.{refund_text}",
        path=f"/training/demo-requests?request={req.pk}",
        actor=actor,
        event="training.demo.cancelled",
    )
    return req


def record_attendance(req: DemoRequest, actor, *, attended_count=None, present_user_ids=None) -> DemoRequest:
    if not access.can_mark_attendance(actor, req.equipment_id):
        raise TrainingError("Only the OIC or a Lab Operator of this equipment can mark attendance.", status=403, code="forbidden")
    if req.status not in (DemoStatus.SCHEDULED, DemoStatus.COMPLETED):
        raise TrainingError("Attendance can be recorded only for scheduled demonstrations.")
    from .delivery import mark_attendance

    with transaction.atomic():
        before = _snap(req)
        if attended_count is not None:
            req.attended_count = max(0, int(attended_count))
            req.save(update_fields=["attended_count", "updated_at"])
        session = req.event.sessions.order_by("seq").first() if req.event_id else None
        if session is not None and present_user_ids is not None:
            present = {int(x) for x in present_user_ids}
            rows = [
                {"registration_id": r.id, "status": AttendanceStatus.PRESENT if r.user_id in present else AttendanceStatus.ABSENT}
                for r in session.event.registrations.all()
            ]
            mark_attendance(session, actor, rows, complete_event=False)
        _revise(req, actor, "attendance", before, reason=f"headcount={req.attended_count}")
    return req


def complete(req: DemoRequest, actor, *, no_show: bool = False, attended_count=None) -> DemoRequest:
    if not access.can_mark_attendance(actor, req.equipment_id):
        raise TrainingError("Only the OIC or a Lab Operator of this equipment can complete a demonstration.", status=403, code="forbidden")
    with transaction.atomic():
        req = _lock(req)
        if req.status != DemoStatus.SCHEDULED:
            raise TrainingError("Only scheduled demonstrations can be completed.")
        before = _snap(req)
        req.status = DemoStatus.NO_SHOW if no_show else DemoStatus.COMPLETED
        req.completed_at = timezone.now()
        if attended_count is not None:
            req.attended_count = max(0, int(attended_count))
        req.save()
        if req.event_id:
            req.event.sessions.update(status=SessionStatus.COMPLETED)
            TrainingEvent.objects.filter(pk=req.event_id).update(status=EventStatus.COMPLETED, completed_at=req.completed_at)
        _revise(req, actor, "no_show" if no_show else "completed", before)
    return req


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
def expire_proposals(now=None) -> int:
    now = now or timezone.now()
    count = 0
    for req in DemoRequest.objects.filter(status=DemoStatus.PROPOSED_ALTERNATIVE, proposal_expires_at__lt=now).select_related("equipment", "requester"):
        with transaction.atomic():
            locked = _lock(req)
            if locked.status != DemoStatus.PROPOSED_ALTERNATIVE:
                continue
            before = _snap(locked)
            locked.status = DemoStatus.EXPIRED
            locked.save(update_fields=["status", "updated_at"])
            _revise(locked, None, "proposal_expired", before)
        count += 1
        notify.send(
            "demo_request_decision_faculty_email",
            [req.requester] + _oic_recipients(req.equipment),
            context=_context(locked, summary=f"The proposed time for demo request {req.reference} expired without a response."),
            title=f"Demo {req.reference}: proposal expired",
            message=f"The proposed time for demo request {req.reference} expired.",
            path=f"/training/demo-requests?request={req.pk}",
            event="training.demo.expired",
        )
    return count


def escalate_overdue(now=None) -> int:
    now = now or timezone.now()
    count = 0
    pending = DemoRequest.objects.filter(status__in=OPEN_FOR_DECISION, sla_escalated_at__isnull=True).select_related(
        "equipment", "requester"
    )
    for req in pending:
        policy = effective_policy(req.equipment)
        if working_days_between(req.submitted_at, now) < policy.review_sla_working_days:
            continue
        DemoRequest.objects.filter(pk=req.pk).update(sla_escalated_at=now)
        audit(None, "demo.escalated", req, note=f"Pending > {policy.review_sla_working_days} working days")
        count += 1
        notify.send(
            "demo_request_escalated_email",
            escalation_recipients(req.equipment),
            context=_context(req, summary=f"Demo request {req.reference} has waited more than {policy.review_sla_working_days} working days for the OIC's decision."),
            title=f"Overdue demo request {req.reference}",
            message=f"Demo request {req.reference} on {req.equipment.name} is waiting for a decision.",
            path=f"/training/oic?tab=requests&request={req.pk}",
            event="training.demo.escalated",
        )
    return count


def escalation_recipients(equipment) -> list:
    from iic_booking.communication.in_app import admin_users
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.rbac import user_has_permission

    users = list(admin_users())
    if equipment.internal_department_id:
        for user in User.objects.filter(
            user_type=UserType.DEPT_ADMIN, department_id=equipment.internal_department_id, is_active=True
        ):
            if user_has_permission(user, "training.manage"):
                users.append(user)
    return users


def refund_amount_for(req: DemoRequest, side: str, now=None) -> Decimal:
    paid = req.charge_amount if req.wallet_txn_id else Decimal("0")
    if not paid:
        return Decimal("0")
    if side == "IIC" or not req.approved_start_at:
        return paid
    policy = effective_policy(req.equipment)
    days_before = (req.approved_start_at - (now or timezone.now())).total_seconds() / 86400
    if days_before >= policy.demo_refund_full_days:
        return paid
    if days_before >= policy.demo_refund_half_days:
        return (paid / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Decimal("0")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _lock(req: DemoRequest) -> DemoRequest:
    return DemoRequest.objects.select_for_update().select_related("equipment", "requester").get(pk=req.pk)


def _require_owner(req: DemoRequest, user) -> None:
    if req.requester_id != getattr(user, "id", None):
        raise TrainingError("This is not your request.", status=403, code="forbidden")


def _stamp(req: DemoRequest, actor) -> None:
    req.decided_by = actor
    req.decided_at = timezone.now()


def _approved_size(req: DemoRequest, data: dict) -> tuple[int, int, bool]:
    try:
        duration = int(data.get("approved_duration_minutes") or req.requested_duration_minutes)
        participants = int(data.get("approved_participants") or req.participants_requested)
    except (TypeError, ValueError):
        raise TrainingError("Approved duration and participants must be numbers.") from None
    if duration < 15 or participants < 1:
        raise TrainingError("Approved duration must be at least 15 minutes and participants at least 1.")
    if duration > req.requested_duration_minutes or participants > req.participants_requested:
        raise TrainingError("Approved duration and participants cannot exceed what was requested.")
    curtailed = duration < req.requested_duration_minutes or participants < req.participants_requested
    return duration, participants, curtailed


def _apply_charge(req: DemoRequest, data: dict, duration: int) -> None:
    """Internal IITR rate × approved duration. The OIC enters an hourly rate only when the equipment has no
    internal rate the portal can use; otherwise the rate cannot be changed or waived."""
    if not charges.is_chargeable(req.purpose):
        req.charge_mode, req.rate_per_hour, req.charge_amount = ChargeMode.FREE, Decimal("0.00"), Decimal("0.00")
        return
    rate = charges.internal_rate(req.equipment, req.requester).rate_per_hour
    if rate is None:
        raw = data.get("rate_per_hour")
        if raw in (None, ""):
            raise TrainingError(
                "This equipment has no internal IITR rate the portal can convert to an hourly charge. "
                "Enter the hourly rate to charge.",
                code="rate_required",
            )
        try:
            rate = Decimal(str(raw))
        except Exception:
            raise TrainingError("Invalid hourly rate.") from None
        if rate <= 0:
            raise TrainingError("Hourly rate must be above zero.")
    if rate <= 0:
        req.charge_mode, req.rate_per_hour, req.charge_amount = ChargeMode.FREE, Decimal("0.00"), Decimal("0.00")
        return
    req.charge_mode, req.rate_per_hour = ChargeMode.WALLET, rate
    req.charge_amount = charges.amount_for(rate, duration)


def _charge_date(req: DemoRequest) -> str:
    when = req.approved_start_at or req.proposed_start_at
    if when is None and req.preferred_windows:
        when = parse_datetime(str(req.preferred_windows[0].get("start") or ""))
    return timezone.localtime(when).strftime("%d %b %Y") if when else timezone.localdate().strftime("%d %b %Y")


def _debit(req: DemoRequest) -> None:
    if req.charge_mode != ChargeMode.WALLET or req.charge_amount <= 0 or req.wallet_txn_id:
        return
    sub, txn = charges.debit(
        req, description=f"Demonstration charge – {req.equipment.code} – {_charge_date(req)} ({req.reference})"
    )
    req.sub_wallet = sub
    req.wallet_txn = txn
    req.save(update_fields=["sub_wallet", "wallet_txn", "updated_at"])


def _refund(req: DemoRequest, amount: Decimal) -> None:
    if amount <= 0 or not req.sub_wallet_id or req.refund_txn_id:
        return
    share = "full" if amount >= req.charge_amount else f"{(amount * 100 / req.charge_amount).quantize(Decimal('1'))}%"
    txn = req.sub_wallet.credit(
        amount,
        description=f"Demonstration refund ({share}) – {req.equipment.code} – {_charge_date(req)} ({req.reference})",
        related_user=req.requester,
    )
    req.refund_txn = txn
    req.refund_amount = amount


def _refund_sentence(req: DemoRequest) -> str:
    if not req.wallet_txn_id:
        return ""
    label = charges.wallet_label(req.equipment)
    if req.refund_amount:
        return f" {format_inr(req.refund_amount)} of the {format_inr(req.charge_amount)} charge was refunded to the {label}."
    return f" The {format_inr(req.charge_amount)} charge is not refunded (cancelled less than the policy's refund window ahead)."


def _ensure_event(req: DemoRequest) -> TrainingEvent:
    if req.event_id:
        return req.event
    event = TrainingEvent.objects.create(
        kind=EventKind.DEMO,
        title=f"Demo: {_course_text(req)}"[:255],
        slug=f"demo-{req.pk}-{secrets.token_hex(3)}",
        equipment=req.equipment,
        department=req.equipment.internal_department,
        status=EventStatus.CONFIRMED,
        capacity=req.approved_participants or req.participants_requested,
        selection_mode=SelectionMode.INVITE,
        created_by=req.decided_by,
    )
    for user in req.participant_users.all():
        Registration.objects.get_or_create(
            event=event,
            user=user,
            defaults={"source": RegistrationSource.DEMO, "status": RegistrationStatus.CONFIRMED},
        )
    req.event = event
    return event


def _schedule(req: DemoRequest, start: datetime, *, actor) -> None:
    duration = req.approved_duration_minutes or req.requested_duration_minutes
    end = start + timedelta(minutes=duration)
    if start <= timezone.now():
        raise TrainingError("The start time must be in the future.")
    event = _ensure_event(req)
    session = event.sessions.order_by("seq").first()
    if session is None:
        session = TrainingSession.objects.create(
            event=event, seq=1, session_type=SessionType.DEMO, equipment=req.equipment, start_at=start, end_at=end
        )
    else:
        release_session_slots(session, actor=actor, note="Rescheduled")
        session.start_at, session.end_at = start, end
        session.save(update_fields=["start_at", "end_at", "updated_at"])
    label = demo_label(course=_course_text(req), faculty_name=_faculty_name(req.requester))
    try:
        reserve_session_slots(session, actor=actor, label=label)
    except ReservationError as exc:
        raise TrainingError(exc.message, code=exc.code, extra={"conflicts": exc.conflicts}) from exc
    req.approved_start_at, req.approved_end_at = start, end
    req.status = DemoStatus.SCHEDULED
    req.save()


def _release(req: DemoRequest, actor, note: str) -> None:
    if not req.event_id:
        return
    for session in req.event.sessions.all():
        release_session_slots(session, actor=actor, note=note)


def _oic_recipients(equipment) -> list:
    from iic_booking.communication.in_app import equipment_oic_users

    return equipment_oic_users(equipment)


def _notify_decision(req: DemoRequest, actor, action: str) -> None:
    if action == "reject":
        summary = f"Your demonstration request {req.reference} on {req.equipment.name} was not approved."
    elif action == "propose":
        summary = (
            f"The OIC proposed another time for {req.reference}: {notify.fmt_window(req.proposed_start_at, req.proposed_end_at)}. "
            "Accept it, or counter once, before the deadline."
        )
    elif req.curtailed:
        reason = dict(CurtailReason.choices).get(req.curtail_reason_code, "")
        summary = (
            f"Your demonstration request {req.reference} was approved with changes ({reason}): "
            f"{notify.fmt_minutes(req.approved_duration_minutes)} for {req.approved_participants} participants. "
            "This decision is final."
        )
    else:
        summary = f"Your demonstration request {req.reference} on {req.equipment.name} was approved as requested."
    if action != "reject" and req.charge_mode == ChargeMode.WALLET and req.charge_amount:
        label = charges.wallet_label(req.equipment)
        if req.wallet_txn_id:
            summary += f" {format_inr(req.charge_amount)} (internal IITR rate) was deducted from your {label}."
        else:
            summary += f" If you accept, {format_inr(req.charge_amount)} (internal IITR rate) will be deducted from your {label}."
    notify.send(
        "demo_request_decision_faculty_email",
        [req.requester],
        context=_context(req, summary=summary, deadline=notify.fmt_dt(req.proposal_expires_at) if action == "propose" else ""),
        title=f"Demo {req.reference}: {req.get_status_display()}",
        message=summary,
        path=f"/training/demo-requests?request={req.pk}",
        actor=actor,
        event=f"training.demo.{action}",
    )


def _notify_scheduled(req: DemoRequest, actor) -> None:
    recipients = [req.requester] + list(req.participant_users.all())
    notify.send(
        "demo_scheduled_participants_email",
        recipients,
        context=_context(req, summary=f"A demonstration on {req.equipment.name} is scheduled for {notify.fmt_window(req.approved_start_at, req.approved_end_at)}."),
        title=f"Demo scheduled: {req.equipment.name}",
        message=f"Demonstration on {req.equipment.name} at {notify.fmt_window(req.approved_start_at, req.approved_end_at)}.",
        path=f"/training/demo-requests?request={req.pk}",
        actor=actor,
        event="training.demo.scheduled",
    )
