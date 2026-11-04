"""Beginner / Expert competency on an equipment (``EquipmentAuthorisationSetting.competency_levels``).

Per equipment, never across equipment:

* **Beginner (supervised)** - induction done (a trainer approved the training request). May book only with an eligible
  supervisor named on the booking (a *supervised booking*); never independently.
* **Expert (independent)** - passed the practical assessment and, when the equipment requires it, approved by the OIC.
  Independent booking needs Expert (or any level the gate already accepted before, so people authorised earlier keep
  their rights).

Rules set per equipment: a minimum number of completed supervised sessions (counted since the current Beginner
status began) before the Expert assessment can be requested; Beginner status lasts ``beginner_validity_months``; the
OIC can downgrade an Expert back to Beginner (with a reason), which closes their trainer designation and restarts the
supervised-session count.

Supervisors and assessors: the equipment's staff (OIC, temporary OIC, Lab Operators) and the Experts the OIC
designated as trainers (``EquipmentTrainer``) while their own authorisation is valid. Nobody supervises or assesses
themselves. Every step is in the training audit log; awards, assessments and sessions are never overwritten.

Equipment without ``competency_levels`` keeps the single "authorised" step used before.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.users.display import get_user_display_name

from . import access, notify
from . import serializers as s
from .audit import audit
from .errors import TrainingError
from .models import (
    OPEN_REQUEST_STATUSES,
    AuthorisationRequest,
    AuthorisationRequestKind,
    AuthorisationRequestStatus,
    AwardStatus,
    CertificationAward,
    CertificationLevel,
    EquipmentTrainer,
    SupervisedSession,
    SupervisionStatus,
)

logger = logging.getLogger(__name__)

BEGINNER = "BEGINNER"
EXPERT = "EXPERT"
SUPERVISION_CODE = "SUPERVISION_REQUIRED"
INDEPENDENT_RANK = 10  # the gate's long-standing minimum (Trained); Expert is 15, Beginner 5
OPEN_SUPERVISION = (SupervisionStatus.REQUESTED, SupervisionStatus.CONFIRMED)
LIVE_AWARD = (AwardStatus.ACTIVE, AwardStatus.DORMANT)


def _eid(equipment) -> int | None:
    return getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)


def level(code: str) -> CertificationLevel:
    row = CertificationLevel.objects.filter(code=code).first()
    if row is None:
        raise TrainingError(f"The {code.title()} level is missing; run production migrations.")
    return row


def two_level(row) -> bool:
    return bool(row is not None and getattr(row, "competency_levels", False))


def _valid(qs):
    now = timezone.now()
    return qs.filter(status__in=LIVE_AWARD).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))


def beginner_award(user, equipment):
    return _valid(CertificationAward.objects.filter(user=user, equipment_id=_eid(equipment), level__code=BEGINNER)).first()


def independent_award(user, equipment):
    return _valid(CertificationAward.objects.filter(user=user, equipment_id=_eid(equipment),
                                                    level__rank__gte=INDEPENDENT_RANK)).order_by("-level__rank").first()


# --------------------------------------------------------------------------- trainers and supervisors
def equipment_staff(equipment) -> list:
    from iic_booking.communication.in_app import equipment_oic_users
    from iic_booking.equipment.models import EquipmentOperator

    people = list(equipment_oic_users(equipment))
    people += [o.operator for o in EquipmentOperator.objects.filter(equipment=equipment).select_related("operator")]
    seen, out = set(), []
    for p in people:
        if p is not None and p.pk not in seen and getattr(p, "is_active", True):
            seen.add(p.pk)
            out.append(p)
    return out


def designated_trainers(equipment) -> list:
    """Active designations whose holder still has a valid independent authorisation on the equipment."""
    rows = EquipmentTrainer.objects.filter(equipment_id=_eid(equipment), is_active=True).select_related("user")
    return [t for t in rows if t.user.is_active and independent_award(t.user, equipment) is not None]


def eligible_supervisors(equipment, *, exclude_user=None) -> list:
    staff = equipment_staff(equipment)
    people = staff + [t.user for t in designated_trainers(equipment) if t.user.pk not in {p.pk for p in staff}]
    return [p for p in people if exclude_user is None or p.pk != exclude_user.pk]


def is_eligible_supervisor(user, equipment, *, student=None) -> bool:
    if user is None or (student is not None and user.pk == student.pk):
        return False
    return any(p.pk == user.pk for p in eligible_supervisors(equipment))


def supervisor_out(user, equipment) -> dict:
    is_staff = any(p.pk == user.pk for p in equipment_staff(equipment))
    return {**s.user_brief(user), "role": "Staff" if is_staff else "Trainer"}


def designate_trainer(equipment, actor, user, note: str = "") -> EquipmentTrainer:
    from .authorisation import _require_configure

    _require_configure(actor, equipment.equipment_id)
    if independent_award(user, equipment) is None:
        raise TrainingError("Only someone with valid Expert (independent) authorisation on this equipment can be a trainer.")
    with transaction.atomic():
        if EquipmentTrainer.objects.filter(equipment=equipment, user=user, is_active=True).exists():
            raise TrainingError(f"{get_user_display_name(user)} is already a trainer on this equipment.", status=409)
        row = EquipmentTrainer.objects.create(equipment=equipment, user=user, designated_by=actor, note=(note or "")[:2000])
        audit(actor, "competency.trainer_designated", row, after={"equipment": equipment.pk, "user": user.pk}, note=note)
    return row


def remove_trainer(row: EquipmentTrainer, actor, reason: str) -> EquipmentTrainer:
    from .authorisation import _require_configure

    _require_configure(actor, row.equipment_id)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required.", code="reason_required")
    if not row.is_active:
        raise TrainingError("This trainer designation is already closed.")
    row.is_active = False
    row.removed_by, row.removed_at, row.remove_reason = actor, timezone.now(), reason[:2000]
    row.save()
    audit(actor, "competency.trainer_removed", row, after={"user": row.user_id}, note=reason)
    return row


def trainer_out(row: EquipmentTrainer) -> dict:
    award = independent_award(row.user, row.equipment)
    return {
        "id": row.pk,
        "user": s.user_brief(row.user),
        "is_active": row.is_active,
        "valid": row.is_active and award is not None,
        "authorisation_valid_until": s.iso(award.valid_until) if award else None,
        "designated_by": s.user_brief(row.designated_by)["name"] if row.designated_by_id else None,
        "designated_at": s.iso(row.designated_at),
        "removed_at": s.iso(row.removed_at),
        "remove_reason": row.remove_reason,
        "note": row.note,
    }


# --------------------------------------------------------------------------- Beginner (induction) and assessment requests
def _setting(equipment):
    from .authorisation import setting_row

    return setting_row(equipment)


def beginner_months(row) -> int:
    return int(getattr(row, "beginner_validity_months", 0) or 6)


def sessions_required(row) -> int:
    return int(getattr(row, "min_supervised_sessions", 0) or 0)


def sessions_done(user, equipment, beginner=None) -> int:
    """Completed supervised sessions on the equipment since the current Beginner status began."""
    beginner = beginner if beginner is not None else beginner_award(user, equipment)
    if beginner is None:
        return 0
    return SupervisedSession.objects.filter(student=user, equipment_id=_eid(equipment), status=SupervisionStatus.ATTENDED,
                                            recorded_at__gte=beginner.awarded_at).count()


def grant_beginner(user, equipment, actor, *, reason: str = "") -> CertificationAward:
    from . import certification

    held = beginner_award(user, equipment) or independent_award(user, equipment)
    if held is not None:
        return held
    award = certification.issue(user, equipment, level(BEGINNER), actor, months=beginner_months(_setting(equipment)))
    audit(actor, "competency.beginner_granted", award, after={"user": user.pk, "equipment": equipment.pk}, note=reason)
    return award


def downgrade(award: CertificationAward, actor, reason: str) -> CertificationAward:
    """OIC: an Expert goes back to Beginner (supervised use). Returns the new Beginner award."""
    from . import certification
    from .authorisation import _require_configure
    from .models import UserBadge

    _require_configure(actor, award.equipment_id)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required.", code="reason_required")
    equipment, user = award.equipment, award.user
    row = _setting(equipment)
    if not two_level(row):
        raise TrainingError("Downgrade applies only to equipment with Beginner and Expert levels.", code="not_two_level")
    if award.level.rank < INDEPENDENT_RANK or award.status not in (*LIVE_AWARD, AwardStatus.SUSPENDED):
        raise TrainingError("Only a held Expert (independent) authorisation can be downgraded.", code="not_expert")
    if user.pk == getattr(actor, "pk", None):
        raise TrainingError("You cannot downgrade your own authorisation.", status=403, code="forbidden")
    now = timezone.now()
    with transaction.atomic():
        held = CertificationAward.objects.select_for_update().filter(
            user=user, equipment=equipment, level__rank__gte=INDEPENDENT_RANK,
            status__in=(*LIVE_AWARD, AwardStatus.SUSPENDED),
        )
        for old in held:
            old.status, old.revoked_at, old.revoked_by = AwardStatus.REVOKED, now, actor
            old.revoke_reason = f"Downgraded to Beginner: {reason}"[:2000]
            old.save()
            UserBadge.objects.filter(award=old, revoked_at__isnull=True).update(revoked_at=now)
            audit(actor, "competency.downgraded", old, before={"level": old.level.code}, after={"level": BEGINNER}, note=reason)
        for t in EquipmentTrainer.objects.filter(equipment=equipment, user=user, is_active=True):
            t.is_active, t.removed_by, t.removed_at = False, actor, now
            t.remove_reason = f"Downgraded to Beginner: {reason}"[:2000]
            t.save()
            audit(actor, "competency.trainer_removed", t, after={"user": user.pk}, note=t.remove_reason)
        beginner = certification.issue(user, equipment, level(BEGINNER), actor, months=beginner_months(row))
    need = sessions_required(row)
    certification._notify_status(
        beginner, actor, "Downgraded",
        f"You are now a Beginner on {equipment.name} (supervised use only). Reason: {reason}"
        + (f" Complete {need} supervised session(s) before asking for the Expert assessment again." if need else ""),
    )
    return beginner


def request_assessment(user, equipment, message: str = "") -> AuthorisationRequest:
    from .authorisation import _req_context, gate_setting, staff_recipients

    row = gate_setting(equipment)
    if not two_level(row):
        raise TrainingError("This equipment has no Expert assessment.", code="not_required")
    if independent_award(user, equipment) is not None:
        raise TrainingError("You are already authorised to operate this equipment independently.", code="already_authorised")
    beginner = beginner_award(user, equipment)
    if beginner is None:
        raise TrainingError("Complete the induction first: an Expert assessment is for Beginners on this equipment.",
                            code="beginner_required")
    need, done = sessions_required(row), sessions_done(user, equipment, beginner)
    if done < need:
        raise TrainingError(f"Complete {need} supervised session(s) first: {done} of {need} recorded as completed.",
                            code="sessions_required")
    if AuthorisationRequest.objects.filter(user=user, equipment=equipment, status__in=OPEN_REQUEST_STATUSES).exists():
        raise TrainingError("You already have an open request for this equipment.", status=409, code="request_open")
    req = AuthorisationRequest.objects.create(user=user, equipment=equipment, kind=AuthorisationRequestKind.ASSESSMENT,
                                              message=(message or "").strip()[:2000])
    audit(user, "competency.assessment_requested", req, after={"equipment": equipment.pk})
    who = get_user_display_name(user)
    recipients = staff_recipients(equipment) + [t.user for t in designated_trainers(equipment)]
    notify.send(
        "training_authorisation_request_email",
        recipients,
        context=_req_context(req, summary=f"{who} asked for the Expert assessment on {equipment.name}.",
                             status="Beginner", remarks=req.message),
        title=f"Expert assessment request: {equipment.name}",
        message=f"{who} asked for the Expert assessment on {equipment.name} ({req.reference}).",
        path=f"/training/authorisations?equipment={equipment.pk}&tab=requests",
        actor=user,
        event="training.competency.assessment_requested",
    )
    return req


def close_assessment_request(assessment, actor, *, awarded: bool) -> None:
    """After an Expert assessment: close the student's open assessment request (left open while OIC approval is due)."""
    req = AuthorisationRequest.objects.filter(
        user_id=assessment.user_id, equipment_id=assessment.equipment_id, kind=AuthorisationRequestKind.ASSESSMENT,
        status__in=OPEN_REQUEST_STATUSES,
    ).first()
    if req is None:
        return
    from .models import AssessmentResult

    now = timezone.now()
    if awarded:
        req.status, req.award_id = AuthorisationRequestStatus.APPROVED, assessment.award_id
    elif assessment.result != AssessmentResult.PASS:
        req.status = AuthorisationRequestStatus.REJECTED
        req.decision_reason = assessment.get_result_display()
    else:
        req.staff_note = "Passed; awaiting the OIC's approval."
        req.save(update_fields=["staff_note", "updated_at"])
        return
    req.decided_by, req.decided_at = actor, now
    req.save()
    audit(actor, "competency.assessment_request_closed", req, after={"status": req.status, "assessment": assessment.pk})


# --------------------------------------------------------------------------- supervised bookings
def supervision_refusal(student, equipment, supervisor) -> str:
    """Why ``supervisor`` cannot supervise ``student`` here ("" when they can)."""
    if supervisor is None:
        return "Choose a supervisor: as a Beginner you may book this equipment only under supervision."
    if supervisor.pk == student.pk:
        return "You cannot supervise yourself."
    if not is_eligible_supervisor(supervisor, equipment, student=student):
        return f"{get_user_display_name(supervisor)} is not an eligible supervisor for this equipment."
    return ""


def booking_supervisor(booking):
    session = SupervisedSession.objects.filter(booking=booking).select_related("supervisor").first()
    return session.supervisor if session and session.status in OPEN_SUPERVISION else None


def attach_supervision(booking, supervisor, *, actor) -> SupervisedSession | None:
    """Record a supervised booking (only for a Beginner of a two-level equipment) and ask the supervisor to confirm."""
    from .authorisation import gate_setting

    if supervisor is None or not two_level(gate_setting(booking.equipment)):
        return None
    if independent_award(booking.user, booking.equipment) is not None or beginner_award(booking.user, booking.equipment) is None:
        return None
    session, created = SupervisedSession.objects.get_or_create(
        booking=booking,
        defaults={"equipment": booking.equipment, "student": booking.user, "supervisor": supervisor},
    )
    if created:
        audit(actor, "competency.supervised_booking", session,
              after={"booking": booking.pk, "supervisor": supervisor.pk, "student": booking.user_id})
        transaction.on_commit(lambda: _notify_supervisor(session, actor))
    return session


def _notify_supervisor(session: SupervisedSession, actor) -> None:
    from iic_booking.communication.utils import booking_display_id_for_email

    who = get_user_display_name(session.student)
    ref = booking_display_id_for_email(session.booking)
    slot = session.booking.daily_slots.order_by("start_datetime").first()
    when = notify.fmt_dt(slot.start_datetime) if slot else ""
    try:
        notify.send(
            "training_authorisation_update_email",
            [session.supervisor],
            context={"reference": ref, "equipment_name": session.equipment.name, "equipment_code": session.equipment.code,
                     "title": f"Supervised session - {session.equipment.name}",
                     "summary": f"{who} (Beginner) booked {session.equipment.name}{f' on {when}' if when else ''} and named you as supervisor.",
                     "status": "Awaiting your confirmation"},
            title=f"Supervise {who} on {session.equipment.name}",
            message=f"{who} named you as supervisor for booking {ref}. Please confirm or decline.",
            path="/training/authorisations?tab=supervision",
            actor=actor,
            event="training.competency.supervision_requested",
        )
    except Exception:
        logger.exception("supervisor notification failed session=%s", session.pk)


def session_out(session: SupervisedSession, viewer=None) -> dict:
    from iic_booking.communication.utils import booking_display_id_for_email

    slots = list(session.booking.daily_slots.order_by("start_datetime"))
    start = slots[0].start_datetime if slots else None
    end = slots[-1].end_datetime if slots else None
    can_act = viewer is not None and (viewer.pk == session.supervisor_id or _is_equipment_staff(viewer, session.equipment))
    return {
        "id": session.pk,
        "booking_id": session.booking_id,
        "booking_reference": booking_display_id_for_email(session.booking),
        "equipment": s.equipment_brief(session.equipment),
        "student": s.user_brief(session.student),
        "supervisor": s.user_brief(session.supervisor),
        "status": session.status,
        "status_label": session.get_status_display(),
        "start": s.iso(start),
        "end": s.iso(end),
        "response_note": session.response_note,
        "remarks": session.remarks,
        "recorded_by": s.user_brief(session.recorded_by)["name"] if session.recorded_by_id else None,
        "recorded_at": s.iso(session.recorded_at),
        "can_respond": bool(can_act and session.status == SupervisionStatus.REQUESTED),
        "can_record": bool(can_act and session.status in OPEN_SUPERVISION and start is not None and start <= timezone.now()),
    }


def _is_equipment_staff(user, equipment) -> bool:
    return access.is_admin(user) or any(p.pk == user.pk for p in equipment_staff(equipment))


def act_on_session(session: SupervisedSession, actor, action: str, data: dict) -> SupervisedSession:
    if not (actor.pk == session.supervisor_id or _is_equipment_staff(actor, session.equipment)):
        raise TrainingError("Only the named supervisor or the equipment's staff can do this.", status=403, code="forbidden")
    note = str(data.get("note") or data.get("remarks") or "").strip()[:2000]
    now = timezone.now()
    before = session.status
    if action in ("confirm", "decline"):
        if session.status != SupervisionStatus.REQUESTED:
            raise TrainingError("This supervised session has already been answered.")
        if action == "decline" and not note:
            raise TrainingError("Give the student a reason.", code="reason_required")
        session.status = SupervisionStatus.CONFIRMED if action == "confirm" else SupervisionStatus.DECLINED
        session.responded_at, session.response_note = now, note
    elif action in ("attended", "no_show"):
        if session.status not in OPEN_SUPERVISION:
            raise TrainingError("This supervised session is already closed.")
        slot = session.booking.daily_slots.order_by("start_datetime").first()
        if slot is None or slot.start_datetime > now:
            raise TrainingError("The session can be recorded once it has started.")
        session.status = SupervisionStatus.ATTENDED if action == "attended" else SupervisionStatus.NO_SHOW
        session.recorded_by, session.recorded_at, session.remarks = actor, now, note
    else:
        raise TrainingError("Unknown action.", status=404, code="not_found")
    session.save()
    audit(actor, f"competency.supervision_{action}", session, before={"status": before}, after={"status": session.status}, note=note)
    try:
        notify.send(
            "training_authorisation_update_email",
            [session.student],
            context={"reference": f"Booking {session.booking_id}", "equipment_name": session.equipment.name,
                     "equipment_code": session.equipment.code, "title": f"Supervised session - {session.equipment.name}",
                     "summary": f"Supervised session on {session.equipment.name}: {session.get_status_display().lower()}."
                                + (f" Note: {note}" if note else ""),
                     "status": session.get_status_display()},
            title=f"Supervised session {session.get_status_display().lower()}: {session.equipment.name}",
            message=f"Supervised session on {session.equipment.name}: {session.get_status_display().lower()}.",
            path="/my-authorisations",
            actor=actor,
            event=f"training.competency.supervision_{action}",
        )
    except Exception:
        logger.exception("student notification failed session=%s", session.pk)
    return session


def sessions_for(user, *, scope: str = "mine") -> list[SupervisedSession]:
    qs = SupervisedSession.objects.select_related("booking", "equipment", "student", "supervisor", "recorded_by")
    if scope == "supervising":
        qs = qs.filter(supervisor=user)
    elif scope == "equipment":
        from .authorisation import handled_equipment_ids

        ids = handled_equipment_ids(user)
        if ids is not None:
            qs = qs.filter(Q(equipment_id__in=ids) | Q(supervisor=user))
    else:
        qs = qs.filter(student=user)
    return list(qs[:200])


# --------------------------------------------------------------------------- status for the gate and the pages
def describe(user, equipment, row, status: dict) -> dict:
    """Extra fields for a two-level equipment: level, independent / supervised, and the next step."""
    if not two_level(row) or status.get("status") == "NOT_REQUIRED":
        return {"two_level": False}
    independent = status.get("allowed", False)
    beginner = beginner_award(user, equipment) if not independent else None
    open_req = AuthorisationRequest.objects.filter(user=user, equipment_id=_eid(equipment),
                                                   status__in=OPEN_REQUEST_STATUSES).first()
    if independent and status.get("status") == "GRACE":
        stage, label = "GRACE", status.get("status_label", "Allowed (past bookings)")
    elif independent:
        stage, label = "EXPERT", "Expert - independent use"
    elif beginner is not None and open_req is not None and open_req.kind == AuthorisationRequestKind.ASSESSMENT:
        stage, label = "ASSESSMENT_PENDING", "Beginner - Expert assessment pending"
    elif beginner is not None:
        stage, label = "BEGINNER", "Beginner - supervised use only"
    elif status.get("status") in ("SUSPENDED", "REVOKED", "EXPIRED"):
        stage, label = status["status"], status.get("status_label", "")
    elif open_req is not None and open_req.status == AuthorisationRequestStatus.SCHEDULED:
        stage, label = "TRAINING_SCHEDULED", "Training scheduled"
    elif open_req is not None:
        stage, label = "TRAINING_REQUESTED", "Training requested"
    else:
        stage, label = "NOT_TRAINED", "Not trained"
    supervised = bool(beginner is not None and getattr(row, "beginner_supervised_allowed", True))
    need = sessions_required(row)
    done = sessions_done(user, equipment, beginner) if beginner is not None else 0
    return {
        "two_level": True,
        "stage": stage,
        "stage_label": label,
        "independent": bool(independent),
        "supervised_only": supervised,
        "beginner_since": s.iso(beginner.awarded_at) if beginner else None,
        "beginner_valid_until": s.iso(beginner.valid_until) if beginner and beginner.valid_until else None,
        "supervised_sessions_done": done,
        "supervised_sessions_required": need,
        "can_request_assessment": bool(beginner is not None and open_req is None and done >= need),
        "supervisors": [supervisor_out(p, equipment) for p in eligible_supervisors(equipment, exclude_user=user)]
        if supervised else [],
    }


# --------------------------------------------------------------------------- booking cancelled -> session cancelled
def _on_booking_saved(sender, instance, created=False, **kwargs):
    from iic_booking.equipment.models import BookingStatus

    if created or instance.status not in (BookingStatus.CANCELLED, BookingStatus.REFUNDED):
        return
    SupervisedSession.objects.filter(booking_id=instance.pk, status__in=OPEN_SUPERVISION).update(
        status=SupervisionStatus.CANCELLED, updated_at=timezone.now()
    )


def connect_signals() -> None:
    from django.db.models.signals import post_save

    from iic_booking.equipment.models import Booking

    post_save.connect(_on_booking_saved, sender=Booking, weak=False, dispatch_uid="competency_supervised_booking_cancel")
