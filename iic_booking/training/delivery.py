"""
Training events, sessions (with slot reservations), attendance and TRAINED awards.

A participant who is PRESENT/LATE (or EXCUSED) in every non-cancelled session of a training event
earns the event's level (TRAINED in Phase 1) and the matching badge, valid for the policy period.
"""

from __future__ import annotations

import logging

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.display import get_user_display_name

from . import access, notify
from .audit import audit
from .errors import TrainingError
from .models import (
    Attendance,
    AttendanceStatus,
    AwardStatus,
    BadgeDefinition,
    CertificationAward,
    CertificationLevel,
    EventKind,
    EventStatus,
    Registration,
    RegistrationStatus,
    SessionStatus,
    SessionType,
    TrainingEvent,
    TrainingSession,
    UserBadge,
)
from .policy import effective_policy
from .slots import ReservationError, release_session_slots, reserve_session_slots

logger = logging.getLogger(__name__)

ATTENDED = (AttendanceStatus.PRESENT, AttendanceStatus.LATE)
ROSTER_STATUSES = (
    RegistrationStatus.CONFIRMED,
    RegistrationStatus.ATTENDED,
    RegistrationStatus.PARTIAL,
    RegistrationStatus.NO_SHOW,
    RegistrationStatus.COMPLETED,
)
MAX_SESSION_MINUTES = 12 * 60


def _dt(value, field: str):
    parsed = value if hasattr(value, "tzinfo") else parse_datetime(str(value or ""))
    if parsed is None:
        raise TrainingError(f"{field} must be an ISO date-time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def require_event_manager(event: TrainingEvent, actor) -> None:
    if not event.equipment_id or not access.can_manage_equipment(actor, event.equipment_id):
        raise TrainingError("Only the equipment's OIC can manage this event.", status=403, code="forbidden")


def session_label(event: TrainingEvent) -> str:
    return f"Training: {event.title}"[:120]


def update_event(event: TrainingEvent, actor, data: dict) -> TrainingEvent:
    require_event_manager(event, actor)
    if event.kind == EventKind.DEMO:
        raise TrainingError("Demonstrations are managed from the demo request.")
    fields = []
    for key in ("title", "description", "venue"):
        if key in data:
            setattr(event, key, (data.get(key) or "").strip()[:255] if key != "description" else (data.get(key) or "").strip())
            fields.append(key)
    if "capacity" in data:
        try:
            event.capacity = max(1, int(data["capacity"]))
        except (TypeError, ValueError):
            raise TrainingError("Capacity must be a number.") from None
        fields.append("capacity")
    if fields:
        event.save(update_fields=[*fields, "updated_at"])
        audit(actor, "event.updated", event, after={k: str(getattr(event, k)) for k in fields})
    return event


def cancel_event(event: TrainingEvent, actor, *, reason: str) -> TrainingEvent:
    require_event_manager(event, actor)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required to cancel an event.")
    if event.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
        raise TrainingError("This event is already closed.")
    with transaction.atomic():
        for session in event.sessions.exclude(status__in=(SessionStatus.COMPLETED, SessionStatus.CANCELLED)):
            release_session_slots(session, actor=actor, note="Event cancelled")
            TrainingSession.objects.filter(pk=session.pk).update(status=SessionStatus.CANCELLED)
        event.status = EventStatus.CANCELLED
        event.cancelled_at = timezone.now()
        event.cancelled_reason = reason
        event.save(update_fields=["status", "cancelled_at", "cancelled_reason", "updated_at"])
        event.nomination_calls.exclude(status="PUBLISHED").update(status="CANCELLED")
        event.registrations.filter(status=RegistrationStatus.CONFIRMED).update(
            status=RegistrationStatus.CANCELLED, cancelled_at=event.cancelled_at
        )
        audit(actor, "event.cancelled", event, note=reason)
    participants = [r.user for r in event.registrations.select_related("user")]
    if participants:
        notify.send(
            "training_session_scheduled_email",
            participants,
            context=_event_context(event, summary=f"{event.title} has been cancelled. Reason: {reason}"),
            title=f"Training cancelled: {event.title}",
            message=f"{event.title} has been cancelled. Reason: {reason}",
            path="/my-trainings",
            actor=actor,
            event="training.event.cancelled",
        )
    return event


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def add_session(event: TrainingEvent, actor, data: dict) -> TrainingSession:
    require_event_manager(event, actor)
    if event.status in (EventStatus.COMPLETED, EventStatus.CANCELLED, EventStatus.CLOSED):
        raise TrainingError("Sessions cannot be added to a closed event.")
    start = _dt(data.get("start_at"), "start_at")
    end = _dt(data.get("end_at"), "end_at")
    _validate_window(start, end)
    session_type = data.get("session_type") or SessionType.HANDS_ON
    if session_type not in SessionType.values:
        raise TrainingError("Invalid session type.")
    with transaction.atomic():
        seq = (event.sessions.aggregate(m=Max("seq"))["m"] or 0) + 1
        session = TrainingSession.objects.create(
            event=event,
            seq=seq,
            title=(data.get("title") or f"Session {seq}").strip()[:255],
            session_type=session_type,
            equipment=event.equipment,
            start_at=start,
            end_at=end,
            location=(data.get("location") or event.venue or "").strip()[:255],
            notes=(data.get("notes") or "").strip(),
        )
        audit(actor, "session.created", session, after={"start": start.isoformat(), "end": end.isoformat()})
        if data.get("reserve_slots", True) and session_type != SessionType.THEORY:
            try:
                with transaction.atomic():
                    reserve_session_slots(session, actor=actor, label=session_label(event))
            except ReservationError as exc:
                raise TrainingError(exc.message, code=exc.code, extra={"conflicts": exc.conflicts}) from None
    if session.status == SessionStatus.SCHEDULED:
        notify_session_scheduled(session, actor)
    return session


def update_session(session: TrainingSession, actor, data: dict) -> TrainingSession:
    event = session.event
    require_event_manager(event, actor)
    if session.status in (SessionStatus.COMPLETED, SessionStatus.CANCELLED):
        raise TrainingError("Completed or cancelled sessions cannot be changed.")
    start = _dt(data["start_at"], "start_at") if data.get("start_at") else session.start_at
    end = _dt(data["end_at"], "end_at") if data.get("end_at") else session.end_at
    _validate_window(start, end)
    moved = start != session.start_at or end != session.end_at
    with transaction.atomic():
        was_reserved = session.slot_reservations.filter(released_at__isnull=True).exists()
        if moved and was_reserved:
            release_session_slots(session, actor=actor, note="Session rescheduled")
        session.start_at, session.end_at = start, end
        for key in ("title", "location", "notes"):
            if key in data:
                setattr(session, key, (data.get(key) or "").strip())
        session.save()
        if moved and was_reserved:
            try:
                with transaction.atomic():
                    reserve_session_slots(session, actor=actor, label=session_label(event))
            except ReservationError as exc:
                raise TrainingError(exc.message, code=exc.code, extra={"conflicts": exc.conflicts}) from None
        audit(actor, "session.updated", session, after={"start": start.isoformat(), "end": end.isoformat()})
    if moved and session.status == SessionStatus.SCHEDULED:
        notify_session_scheduled(session, actor, rescheduled=True)
    return session


def cancel_session(session: TrainingSession, actor, *, note: str = "") -> TrainingSession:
    require_event_manager(session.event, actor)
    if session.status in (SessionStatus.COMPLETED, SessionStatus.CANCELLED):
        raise TrainingError("This session is already closed.")
    with transaction.atomic():
        release_session_slots(session, actor=actor, note=note or "Session cancelled")
        session.status = SessionStatus.CANCELLED
        session.save(update_fields=["status", "updated_at"])
        audit(actor, "session.cancelled", session, note=note)
    return session


def reserve(session: TrainingSession, actor) -> dict:
    require_event_manager(session.event, actor)
    if session.status in (SessionStatus.COMPLETED, SessionStatus.CANCELLED):
        raise TrainingError("This session is closed.")
    try:
        result = reserve_session_slots(session, actor=actor, label=session_label(session.event))
    except ReservationError as exc:
        raise TrainingError(exc.message, code=exc.code, extra={"conflicts": exc.conflicts}) from None
    audit(actor, "session.slots_reserved", session, after={"slots": result.reserved_slot_ids, "family": result.family_slot_ids})
    notify_session_scheduled(session, actor)
    return {
        "reserved_slot_ids": result.reserved_slot_ids,
        "family_slot_ids": result.family_slot_ids,
        "skipped_family": result.skipped_family,
    }


def release(session: TrainingSession, actor, *, note: str = "") -> dict:
    require_event_manager(session.event, actor)
    out = release_session_slots(session, actor=actor, note=note or "Released by OIC")
    audit(actor, "session.slots_released", session, after=out)
    return out


def _validate_window(start, end) -> None:
    if end <= start:
        raise TrainingError("The session must end after it starts.")
    if (end - start).total_seconds() / 60 > MAX_SESSION_MINUTES:
        raise TrainingError("A session cannot be longer than 12 hours.")


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------
def roster(session: TrainingSession) -> list[dict]:
    marks = {a.registration_id: a for a in session.attendance.all()}
    rows = []
    for reg in session.event.registrations.filter(status__in=ROSTER_STATUSES).select_related("user", "user__department"):
        mark = marks.get(reg.id)
        rows.append(
            {
                "registration_id": reg.id,
                "user_id": reg.user_id,
                "name": get_user_display_name(reg.user),
                "email": reg.user.email,
                "department": getattr(reg.user.department, "name", "") or "",
                "registration_status": reg.status,
                "attendance": mark.status if mark else None,
                "remarks": mark.remarks if mark else "",
            }
        )
    return rows


def mark_attendance(session: TrainingSession, actor, rows: list[dict], *, complete_event: bool = True) -> dict:
    equipment_id = session.equipment_id or session.event.equipment_id
    if not access.can_mark_attendance(actor, equipment_id):
        raise TrainingError("Only the OIC or a Lab Operator of this equipment can mark attendance.", status=403, code="forbidden")
    if session.status == SessionStatus.CANCELLED:
        raise TrainingError("This session was cancelled.")
    if session.start_at > timezone.now():
        raise TrainingError("Attendance can be marked once the session has started.")
    regs = {r.id: r for r in session.event.registrations.filter(status__in=ROSTER_STATUSES)}
    saved = 0
    with transaction.atomic():
        for row in rows or []:
            try:
                reg_id = int(row.get("registration_id"))
            except (TypeError, ValueError):
                continue
            status = (row.get("status") or "").upper()
            if reg_id not in regs or status not in AttendanceStatus.values:
                continue
            Attendance.objects.update_or_create(
                registration_id=reg_id,
                session=session,
                defaults={
                    "status": status,
                    "remarks": (row.get("remarks") or "")[:255],
                    "marked_by": actor,
                    "method": "MANUAL",
                },
            )
            saved += 1
        session.status = SessionStatus.COMPLETED
        session.attendance_marked_at = timezone.now()
        session.attendance_marked_by = actor
        session.save(update_fields=["status", "attendance_marked_at", "attendance_marked_by", "updated_at"])
        audit(actor, "session.attendance", session, after={"rows": saved})
        awarded = []
        if complete_event and session.event.kind != EventKind.DEMO:
            awarded = complete_if_done(session.event, actor)
    return {"saved": saved, "awarded_user_ids": [a.user_id for a in awarded]}


def complete_if_done(event: TrainingEvent, actor) -> list[CertificationAward]:
    sessions = list(event.sessions.exclude(status=SessionStatus.CANCELLED))
    if not sessions or any(s.status != SessionStatus.COMPLETED for s in sessions):
        if sessions and event.status in (EventStatus.SELECTION_PUBLISHED, EventStatus.CONFIRMED, EventStatus.OPEN, EventStatus.SHORTLISTING):
            TrainingEvent.objects.filter(pk=event.pk).update(status=EventStatus.IN_PROGRESS)
        return []
    session_ids = {s.id for s in sessions}
    awards = []
    now = timezone.now()
    for reg in event.registrations.filter(status__in=ROSTER_STATUSES).select_related("user"):
        marks = {a.session_id: a.status for a in reg.attendance.filter(session_id__in=session_ids)}
        attended = [sid for sid in session_ids if marks.get(sid) in ATTENDED]
        excused = [sid for sid in session_ids if marks.get(sid) == AttendanceStatus.EXCUSED]
        if attended and len(attended) + len(excused) == len(session_ids):
            reg.status = RegistrationStatus.COMPLETED
            award = issue_award(reg.user, event, reg, actor)
            if award:
                awards.append(award)
        elif attended:
            reg.status = RegistrationStatus.PARTIAL
        else:
            reg.status = RegistrationStatus.NO_SHOW
        reg.save(update_fields=["status", "updated_at"])
    TrainingEvent.objects.filter(pk=event.pk).update(status=EventStatus.COMPLETED, completed_at=now)
    audit(actor, "event.completed", event, after={"awards": len(awards)})
    return awards


def issue_award(user, event: TrainingEvent, registration: Registration | None, actor) -> CertificationAward | None:
    level = event.level_awarded or CertificationLevel.objects.filter(code="TRAINED").first()
    if level is None or not level.is_active:
        return None
    equipment = event.equipment
    existing = CertificationAward.objects.filter(
        user=user,
        equipment=equipment,
        status__in=(AwardStatus.ACTIVE, AwardStatus.PROVISIONAL, AwardStatus.DORMANT),
        level__rank__gte=level.rank,
    ).first()
    if existing:
        return None
    policy = effective_policy(equipment)
    months = policy.trained_validity_months if level.code == "TRAINED" else (level.default_validity_months or policy.trained_validity_months)
    now = timezone.now()
    award = CertificationAward.objects.create(
        user=user,
        equipment=equipment,
        equipment_group_id=getattr(equipment, "equipment_group_id", None),
        level=level,
        status=AwardStatus.ACTIVE,
        awarded_at=now,
        valid_until=now + relativedelta(months=int(months)) if months else None,
        source_event=event,
        source_registration=registration,
        awarded_by=actor if getattr(actor, "pk", None) else None,
    )
    from .certification import certificate_number

    award.certificate_no = certificate_number(award)
    award.save(update_fields=["certificate_no"])
    badge = BadgeDefinition.objects.filter(level=level, is_active=True).first() or BadgeDefinition.objects.filter(code="trained").first()
    if badge:
        UserBadge.objects.filter(user=user, badge=badge, equipment=equipment, revoked_at__isnull=True).update(revoked_at=now)
        UserBadge.objects.create(user=user, badge=badge, equipment=equipment, award=award, source=f"event:{event.pk}", awarded_at=now)
    audit(actor, "award.issued", award, after={"level": level.code, "user": user.pk, "equipment": getattr(equipment, "pk", None)})
    recipients = [user]
    if getattr(user, "supervisor_id", None):
        recipients.append(user.supervisor)
    notify.send(
        "certification_awarded_email",
        recipients,
        context=_event_context(
            event,
            summary=f"{get_user_display_name(user)} is now {level.name} on {getattr(equipment, 'name', 'the instrument')}"
            + (f", valid until {notify.fmt_dt(award.valid_until)}." if award.valid_until else "."),
            status=level.name,
        ),
        title=f"{level.name}: {getattr(equipment, 'name', '')}",
        message=f"{get_user_display_name(user)} earned {level.name} on {getattr(equipment, 'name', '')}.",
        path="/my-trainings?tab=certifications",
        actor=actor,
        event="training.award.issued",
    )
    return award


# ---------------------------------------------------------------------------
# Badges for UI chips
# ---------------------------------------------------------------------------
def badges_for_users(user_ids) -> dict[int, list[dict]]:
    ids = [int(x) for x in user_ids if str(x).isdigit()][:500]
    out: dict[int, list[dict]] = {i: [] for i in ids}
    if not ids:
        return out
    now = timezone.now()
    qs = (
        UserBadge.objects.filter(user_id__in=ids, revoked_at__isnull=True, is_public=True)
        .select_related("badge", "equipment", "award", "award__level")
        .order_by("-awarded_at")
    )
    for ub in qs:
        award = ub.award
        if award and (award.status not in (AwardStatus.ACTIVE, AwardStatus.PROVISIONAL, AwardStatus.DORMANT) or (award.valid_until and award.valid_until < now)):
            continue
        out.setdefault(ub.user_id, []).append(
            {
                "code": ub.badge.code,
                "name": ub.badge.name,
                "color": ub.badge.color,
                "icon": ub.badge.icon,
                "equipment_id": ub.equipment_id,
                "equipment_code": getattr(ub.equipment, "code", ""),
                "equipment_name": getattr(ub.equipment, "name", ""),
                "level": award.level.code if award else "",
                "awarded_at": ub.awarded_at.isoformat(),
                "valid_until": award.valid_until.isoformat() if award and award.valid_until else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _event_context(event: TrainingEvent, **extra) -> dict:
    ctx = {
        "reference": f"E-{event.pk:04d}",
        "equipment_name": getattr(event.equipment, "name", ""),
        "equipment_code": getattr(event.equipment, "code", ""),
        "title": event.title,
    }
    ctx.update(extra)
    return ctx


def notify_session_scheduled(session: TrainingSession, actor, *, rescheduled: bool = False) -> None:
    event = session.event
    if event.kind == EventKind.DEMO:
        return
    users = [r.user for r in event.registrations.filter(status=RegistrationStatus.CONFIRMED).select_related("user")]
    if not users:
        return
    when = notify.fmt_window(session.start_at, session.end_at)
    verb = "rescheduled" if rescheduled else "scheduled"
    notify.send(
        "training_session_scheduled_email",
        users,
        context=_event_context(event, summary=f"{session.title} of {event.title} is {verb} for {when}.", when=when),
        title=f"Training session {verb}",
        message=f"{session.title} of {event.title}: {when}.",
        path="/my-trainings",
        actor=actor,
        event="training.session.scheduled",
    )
