"""
Authorisation to book: per-equipment gate, the user's status, training requests, grants, grandfathering and
expiry reminders.

The authorisation record is the existing ``CertificationAward`` (level at least the equipment's minimum level,
default Trained). The gate applies only when all of these hold, so nothing changes until an OIC or the Main Admin
turns it on:

* the equipment's ``EquipmentAuthorisationSetting.required`` is on (off by default, no row = off);
* the Training module is on and the equipment is in Training scope;
* the booking user is not staff, falls under ``applies_to`` (internal only by default) and is in the Training
  audience (so a user is never blocked without being able to request training).

Staff booking on someone's behalf (OIC, Main Admin, department admin, Lab Operator) bypass the gate; the bypass is
written to the training audit log.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from iic_booking.users.display import get_user_display_name
from iic_booking.users.models.user_type import UserType

from . import access, notify
from . import serializers as s
from .audit import audit
from .errors import TrainingError
from .models import (
    OPEN_REQUEST_STATUSES,
    AuthorisationAppliesTo,
    AuthorisationRequest,
    AuthorisationRequestStatus,
    AwardStatus,
    CertificationAward,
    CertificationLevel,
    EquipmentAuthorisationSetting,
    TrainingAuditLog,
)

logger = logging.getLogger(__name__)

GATE_CODE = "AUTHORISATION_REQUIRED"
AUTHORISED_STATUSES = (AwardStatus.ACTIVE, AwardStatus.DORMANT)
EXPIRING_DAYS = 30
REMINDER_DAYS = (30, 7)
MAX_MONTHS = 60
STAFF_BYPASS_TYPES = (UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN, UserType.OPERATOR)

STATUS_LABELS = {
    "NOT_REQUIRED": "No authorisation needed",
    "AUTHORISED": "Authorised",
    "EXPIRING": "Authorised (expiring soon)",
    "GRACE": "Allowed (past bookings)",
    "EXPIRED": "Expired",
    "SUSPENDED": "Suspended",
    "REVOKED": "Revoked",
    "NOT_AUTHORISED": "Not authorised",
}
ALLOWED = ("NOT_REQUIRED", "AUTHORISED", "EXPIRING", "GRACE")


# ---------------------------------------------------------------------------
# Settings and scope
# ---------------------------------------------------------------------------
def _eid(equipment) -> int | None:
    return getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)


def setting_row(equipment) -> EquipmentAuthorisationSetting | None:
    """The stored row, or None (also before the table is migrated)."""
    try:
        with transaction.atomic():
            return EquipmentAuthorisationSetting.objects.filter(equipment_id=_eid(equipment)).first()
    except DatabaseError:
        logger.warning("authorisation settings unavailable", exc_info=True)
        return None


def gate_setting(equipment) -> EquipmentAuthorisationSetting | None:
    """The setting when the gate is live for this equipment, else None."""
    row = setting_row(equipment)
    if row is None or not row.required:
        return None
    if not access.module_enabled() or not access.equipment_in_pilot(equipment):
        return None
    return row


def gated_equipment_ids() -> set[int]:
    """Equipment where the gate is live (for "Training required" badges)."""
    if not access.module_enabled():
        return set()
    try:
        with transaction.atomic():
            ids = set(EquipmentAuthorisationSetting.objects.filter(required=True).values_list("equipment_id", flat=True))
    except DatabaseError:
        return set()
    scope = access.pilot_equipment_ids()
    return ids if scope is None else ids & scope


def is_staff(user) -> bool:
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", None) in UserType.get_management_user_codes())


def user_subject_to(row: EquipmentAuthorisationSetting, user) -> bool:
    if user is None or not getattr(user, "is_authenticated", True) or is_staff(user):
        return False
    if row.applies_to == AuthorisationAppliesTo.INTERNAL and not UserType.is_internal_user(user.user_type):
        return False
    if not access.in_audience(user):
        return False
    return access.equipment_allows_user(row.equipment, user)


def min_level(row: EquipmentAuthorisationSetting | None) -> CertificationLevel | None:
    code = (row.min_level_code if row else "") or "TRAINED"
    return CertificationLevel.objects.filter(code=code).first() or CertificationLevel.objects.filter(code="TRAINED").first()


def _min_rank(level) -> int:
    return level.rank if level is not None else 10


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def current_award(user, equipment, min_rank: int):
    now = timezone.now()
    return (
        CertificationAward.objects.filter(
            user=user, equipment_id=_eid(equipment), status__in=AUTHORISED_STATUSES, level__rank__gte=min_rank
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .select_related("level")
        .order_by("-awarded_at")
        .first()
    )


def latest_award(user, equipment, min_rank: int):
    return (
        CertificationAward.objects.filter(user=user, equipment_id=_eid(equipment), level__rank__gte=min_rank)
        .exclude(status=AwardStatus.SUPERSEDED)
        .select_related("level")
        .order_by("-awarded_at")
        .first()
    )


def completed_bookings(user_ids, equipment, months: int) -> dict[int, dict]:
    """{user_id: {count, last}} of COMPLETED bookings on the equipment in the last ``months``."""
    from iic_booking.equipment.models import Booking, BookingStatus

    since = timezone.now() - relativedelta(months=int(months))
    qs = (
        Booking.objects.filter(equipment_id=_eid(equipment), status=BookingStatus.COMPLETED)
        .annotate(t=Coalesce("completed_at", "created_at"))
        .filter(t__gte=since)
    )
    if user_ids is not None:
        qs = qs.filter(user_id__in=list(user_ids))
    return {r["user_id"]: {"count": r["n"], "last": r["last"]} for r in qs.values("user_id").annotate(n=Count("pk"), last=Max("t"))}


def open_request(user, equipment):
    return AuthorisationRequest.objects.filter(user=user, equipment_id=_eid(equipment), status__in=OPEN_REQUEST_STATUSES).first()


def request_brief(req: AuthorisationRequest | None) -> dict | None:
    if req is None:
        return None
    return {
        "id": req.pk,
        "reference": req.reference,
        "status": req.status,
        "status_label": req.get_status_display(),
        "scheduled_start": s.iso(req.scheduled_start),
        "scheduled_end": s.iso(req.scheduled_end),
        "location": req.location,
        "created_at": s.iso(req.created_at),
    }


def _not_required(equipment) -> dict:
    return {
        "equipment_id": _eid(equipment),
        "required": False,
        "status": "NOT_REQUIRED",
        "status_label": STATUS_LABELS["NOT_REQUIRED"],
        "allowed": True,
        "valid_until": None,
        "award_id": None,
        "certificate_no": "",
        "level_name": "",
        "open_request": None,
        "can_request": False,
        "message": "",
    }


def evaluate(user, equipment, row: EquipmentAuthorisationSetting | None = None, *, _row_known: bool = False) -> dict:
    """The user's authorisation status for one equipment."""
    if not _row_known:
        row = gate_setting(equipment)
    if row is None or not user_subject_to(row, user):
        return _not_required(equipment)
    level = min_level(row)
    rank = _min_rank(level)
    now = timezone.now()
    award = current_award(user, equipment, rank)
    out = {
        **_not_required(equipment),
        "required": True,
        "level_name": getattr(level, "name", "Trained"),
        "applies_to": row.applies_to,
    }
    if award is not None:
        expiring = bool(award.valid_until and award.valid_until <= now + timedelta(days=EXPIRING_DAYS))
        out.update(status="EXPIRING" if expiring else "AUTHORISED", award_id=award.pk,
                   valid_until=s.iso(award.valid_until), certificate_no=award.certificate_no or "")
    else:
        last = latest_award(user, equipment, rank)
        if last is not None and last.status in (AwardStatus.SUSPENDED, AwardStatus.REVOKED):
            out.update(status=last.status, award_id=last.pk, valid_until=s.iso(last.valid_until))
        elif last is not None:
            out.update(status="EXPIRED", award_id=last.pk, valid_until=s.iso(last.valid_until))
        else:
            out["status"] = "NOT_AUTHORISED"
        if out["status"] in ("EXPIRED", "NOT_AUTHORISED") and row.grace_completed_bookings:
            done = completed_bookings([user.pk], equipment, row.grace_months).get(user.pk, {}).get("count", 0)
            if done >= row.grace_completed_bookings:
                out["status"] = "GRACE"
    req = open_request(user, equipment)
    out["open_request"] = request_brief(req)
    out["status_label"] = STATUS_LABELS[out["status"]]
    out["allowed"] = out["status"] in ALLOWED
    out["can_request"] = req is None and out["status"] not in ("AUTHORISED", "SUSPENDED", "REVOKED")
    out["message"] = gate_message(out, equipment)
    return out


def gate_message(st: dict, equipment) -> str:
    name = getattr(equipment, "name", "this equipment")
    level = st.get("level_name") or "Trained"
    status = st["status"]
    if status == "EXPIRED":
        until = notify.fmt_dt(_parse(st.get("valid_until"))) if st.get("valid_until") else ""
        return f"Your authorisation for {name} expired{f' on {until}' if until else ''}. Request refresher training to book it again."
    if status == "SUSPENDED":
        return f"Your authorisation for {name} is suspended. Contact the OIC."
    if status == "REVOKED":
        return f"Your authorisation for {name} was revoked. Contact the OIC or request training again."
    if status == "NOT_AUTHORISED":
        return f"Authorisation required: {name} can be booked only by users authorised as {level}. Request training first."
    if status == "GRACE":
        return f"You may book {name} on the strength of your past bookings. Please request training to get authorised."
    if status == "EXPIRING":
        until = notify.fmt_dt(_parse(st.get("valid_until")))
        return f"Your authorisation for {name} expires on {until}. Request refresher training to renew it."
    return ""


def _parse(value):
    from django.utils.dateparse import parse_datetime

    return parse_datetime(value) if isinstance(value, str) else value


def statuses_for(user, equipment_list) -> dict[int, dict]:
    if not access.module_enabled():
        return {_eid(e): _not_required(e) for e in equipment_list}
    try:
        with transaction.atomic():
            rows = {r.equipment_id: r for r in EquipmentAuthorisationSetting.objects.filter(
                equipment_id__in=[_eid(e) for e in equipment_list], required=True).select_related("equipment")}
    except DatabaseError:
        rows = {}
    out = {}
    for eq in equipment_list:
        row = rows.get(_eid(eq))
        if row is not None and not access.equipment_in_pilot(eq):
            row = None
        out[_eid(eq)] = evaluate(user, eq, row, _row_known=True)
    return out


# ---------------------------------------------------------------------------
# Booking gate
# ---------------------------------------------------------------------------
def may_bypass(actor) -> bool:
    return bool(actor is not None and (getattr(actor, "is_superuser", False) or getattr(actor, "user_type", None) in STAFF_BYPASS_TYPES))


def check_booking(booking_user, equipment, *, actor=None, context: str = "booking", record: bool = True) -> dict | None:
    """None when the booking may go ahead, else an error payload (``code`` = AUTHORISATION_REQUIRED).

    ``actor`` is who triggers it; staff acting for someone else bypass, with an audit entry when ``record``.
    """
    try:
        row = gate_setting(equipment)
        if row is None or not user_subject_to(row, booking_user):
            return None
        st = evaluate(booking_user, equipment, row, _row_known=True)
    except Exception:
        logger.exception("authorisation gate check failed equipment_id=%s", _eid(equipment))
        return None
    if st["allowed"]:
        return None
    if actor is not None and getattr(actor, "pk", None) != getattr(booking_user, "pk", None) and may_bypass(actor):
        if not record:
            return None
        audit(
            actor,
            "gate.bypassed",
            row,
            after={"user": booking_user.pk, "equipment": _eid(equipment), "context": context, "status": st["status"]},
            note=f"Staff {context} for a user who is {STATUS_LABELS[st['status']].lower()}; authorisation gate bypassed.",
        )
        return None
    return {"error": st["message"], "message": st["message"], "code": GATE_CODE, "authorisation": st}


def gate_error_text(booking_user, equipment, *, actor=None, context: str = "booking", record: bool = True) -> str | None:
    payload = check_booking(booking_user, equipment, actor=actor, context=context, record=record)
    return payload["error"] if payload else None


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def can_handle(user, equipment_id: int) -> bool:
    """OIC (or temporary OIC), Lab Operator of the equipment, or the Main Admin."""
    return access.can_mark_attendance(user, equipment_id)


def can_configure(user, equipment_id: int) -> bool:
    return access.can_manage_equipment(user, equipment_id)


def handled_equipment_ids(user) -> set[int] | None:
    if access.is_admin(user):
        return None
    return access.oic_equipment_ids(user) | access.operator_equipment_ids(user)


def _require_handle(user, equipment_id):
    if not can_handle(user, equipment_id):
        raise TrainingError("Only the OIC or a Lab Operator of this equipment can do this.", status=403, code="forbidden")


def _require_configure(user, equipment_id):
    if not can_configure(user, equipment_id):
        raise TrainingError("Only the equipment's OIC or the Main Admin can do this.", status=403, code="forbidden")


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------
SETTING_FIELDS = ("required", "applies_to", "min_level_code", "validity_months", "grace_completed_bookings", "grace_months", "note")


def setting_out(equipment, row: EquipmentAuthorisationSetting | None = None) -> dict:
    row = row or setting_row(equipment) or EquipmentAuthorisationSetting(equipment=equipment)
    level = min_level(row)
    return {
        "equipment": s.equipment_brief(equipment),
        "required": bool(row.required),
        "live": bool(row.required) and access.module_enabled() and access.equipment_in_pilot(equipment),
        "module_enabled": access.module_enabled(),
        "in_training_scope": access.equipment_in_pilot(equipment),
        "audience": access.audience(),
        "applies_to": row.applies_to,
        "applies_to_choices": [{"value": v, "label": str(lbl)} for v, lbl in AuthorisationAppliesTo.choices],
        "min_level_code": getattr(level, "code", "TRAINED"),
        "min_level_name": getattr(level, "name", "Trained"),
        "validity_months": row.validity_months,
        "grace_completed_bookings": row.grace_completed_bookings,
        "grace_months": row.grace_months,
        "note": row.note,
        "updated_at": s.iso(row.updated_at) if row.pk else None,
        "updated_by": s.user_brief(row.updated_by)["name"] if row.pk and row.updated_by_id else None,
    }


def _int_in(data, field, lo, hi, default):
    raw = data.get(field, default)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise TrainingError(f"{field} must be a whole number.") from None
    if not lo <= v <= hi:
        raise TrainingError(f"{field} must be between {lo} and {hi}.")
    return v


def save_setting(equipment, actor, data: dict) -> dict:
    _require_configure(actor, equipment.equipment_id)
    with transaction.atomic():
        row, _ = EquipmentAuthorisationSetting.objects.select_for_update().get_or_create(equipment=equipment)
        before = {f: getattr(row, f) for f in SETTING_FIELDS}
        if "required" in data:
            row.required = data["required"] in (True, "true", "True", "1", 1)
        if "applies_to" in data:
            if data["applies_to"] not in AuthorisationAppliesTo.values:
                raise TrainingError("applies_to must be INTERNAL or ALL.")
            row.applies_to = data["applies_to"]
        if data.get("min_level_code"):
            if not CertificationLevel.objects.filter(code=data["min_level_code"], is_active=True).exists():
                raise TrainingError("Choose an active certification level.")
            row.min_level_code = data["min_level_code"]
        if "validity_months" in data:
            row.validity_months = _int_in(data, "validity_months", 1, MAX_MONTHS, row.validity_months)
        if "grace_completed_bookings" in data:
            row.grace_completed_bookings = _int_in(data, "grace_completed_bookings", 0, 50, 0)
        if "grace_months" in data:
            row.grace_months = _int_in(data, "grace_months", 1, MAX_MONTHS, row.grace_months)
        if "note" in data:
            row.note = str(data.get("note") or "")[:2000]
        after = {f: getattr(row, f) for f in SETTING_FIELDS}
        if after != before:
            row.updated_by = actor
            row.save()
            audit(actor, "authorisation.setting_updated", row, before=before, after={**after, "equipment_code": equipment.code},
                  note=str(data.get("reason") or ""))
    return setting_out(equipment, row)


# ---------------------------------------------------------------------------
# Training requests
# ---------------------------------------------------------------------------
def staff_recipients(equipment) -> list:
    from iic_booking.communication.in_app import equipment_oic_users
    from iic_booking.equipment.models import EquipmentOperator

    ops = [o.operator for o in EquipmentOperator.objects.filter(equipment=equipment).select_related("operator")]
    return [*equipment_oic_users(equipment), *ops]


def request_out(req: AuthorisationRequest, viewer=None) -> dict:
    st = None
    if viewer is not None and can_handle(viewer, req.equipment_id):
        done = completed_bookings([req.user_id], req.equipment, 12).get(req.user_id, {})
        st = {"completed_bookings_12m": done.get("count", 0)}
    return {
        **request_brief(req),
        "user": s.user_brief(req.user),
        "supervisor": s.user_brief(req.user.supervisor) if getattr(req.user, "supervisor_id", None) else None,
        "equipment": s.equipment_brief(req.equipment),
        "message": req.message,
        "staff_note": req.staff_note,
        "decision_reason": req.decision_reason,
        "decided_by": s.user_brief(req.decided_by) if req.decided_by_id else None,
        "decided_at": s.iso(req.decided_at),
        "award_id": req.award_id,
        "history": st,
        "can_handle": bool(viewer is not None and req.status in OPEN_REQUEST_STATUSES and can_handle(viewer, req.equipment_id)),
        "can_withdraw": bool(viewer is not None and viewer.pk == req.user_id and req.status in OPEN_REQUEST_STATUSES),
    }


def _req_context(req: AuthorisationRequest, **extra) -> dict:
    return {
        "reference": req.reference,
        "equipment_name": req.equipment.name,
        "equipment_code": req.equipment.code,
        "title": f"Authorisation – {req.equipment.name}",
        **extra,
    }


def create_request(user, equipment, message: str = "") -> AuthorisationRequest:
    row = gate_setting(equipment)
    if row is None or not user_subject_to(row, user):
        raise TrainingError("This equipment does not need authorisation for your account.", code="not_required")
    st = evaluate(user, equipment, row, _row_known=True)
    if st["status"] == "AUTHORISED":
        raise TrainingError("You are already authorised on this equipment.", code="already_authorised")
    if st["status"] in ("SUSPENDED", "REVOKED") and not message.strip():
        raise TrainingError("Tell the OIC why you are asking again (your authorisation was suspended or revoked).", code="reason_required")
    if st["open_request"]:
        raise TrainingError("You already have an open training request for this equipment.", status=409, code="request_open",
                            extra={"request": st["open_request"]})
    try:
        with transaction.atomic():
            req = AuthorisationRequest.objects.create(user=user, equipment=equipment, message=(message or "").strip()[:2000])
    except IntegrityError:
        raise TrainingError("You already have an open training request for this equipment.", status=409, code="request_open") from None
    audit(user, "authorisation.requested", req, after={"equipment": equipment.pk, "status_before": st["status"]})
    who = get_user_display_name(user)
    notify.send(
        "training_authorisation_request_email",
        staff_recipients(equipment),
        context=_req_context(req, summary=f"{who} asked for training to be authorised on {equipment.name}.",
                             status=STATUS_LABELS[st["status"]], remarks=req.message),
        title=f"Training request: {equipment.name}",
        message=f"{who} asked for training on {equipment.name} ({req.reference}).",
        path=f"/training/authorisations?equipment={equipment.pk}&tab=requests",
        actor=user,
        event="training.authorisation.requested",
    )
    return req


def _notify_user(req: AuthorisationRequest, actor, label: str, summary: str, **extra) -> None:
    recipients = [req.user]
    if getattr(req.user, "supervisor_id", None):
        recipients.append(req.user.supervisor)
    notify.send(
        "training_authorisation_update_email",
        recipients,
        context=_req_context(req, summary=summary, status=label, **extra),
        title=f"Training request {label.lower()}: {req.equipment.name}",
        message=summary,
        path="/my-authorisations",
        actor=actor,
        event=f"training.authorisation.{label.lower().replace(' ', '_')}",
    )


def _dt(raw, field):
    from django.utils.dateparse import parse_datetime

    value = parse_datetime(str(raw or ""))
    if value is None:
        raise TrainingError(f"{field} must be a date and time.")
    return timezone.make_aware(value) if timezone.is_naive(value) else value


def act_on_request(req: AuthorisationRequest, actor, action: str, data: dict) -> AuthorisationRequest:
    if action == "withdraw":
        if actor.pk != req.user_id:
            raise TrainingError("Only the requester can withdraw.", status=403, code="forbidden")
        if req.status not in OPEN_REQUEST_STATUSES:
            raise TrainingError("This request is already closed.")
        req.status = AuthorisationRequestStatus.WITHDRAWN
        req.decided_at = timezone.now()
        req.save()
        audit(actor, "authorisation.request_withdrawn", req)
        return req
    _require_handle(actor, req.equipment_id)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise TrainingError("This request is already closed.")
    now = timezone.now()
    if action == "schedule":
        start = _dt(data.get("start_at"), "start_at")
        end = _dt(data.get("end_at"), "end_at") if data.get("end_at") else start + timedelta(hours=1)
        if end <= start:
            raise TrainingError("The session must end after it starts.")
        req.status = AuthorisationRequestStatus.SCHEDULED
        req.scheduled_start, req.scheduled_end = start, end
        req.location = str(data.get("location") or "")[:255]
        req.staff_note = str(data.get("note") or "")[:2000]
        req.save()
        audit(actor, "authorisation.request_scheduled", req, after={"start": start.isoformat(), "end": end.isoformat()})
        _notify_user(req, actor, "Scheduled", f"Your training on {req.equipment.name} is scheduled for {notify.fmt_window(start, end)}"
                     + (f" at {req.location}." if req.location else "."), when=notify.fmt_window(start, end), remarks=req.staff_note)
        return req
    if action == "approve":
        months = _int_in(data, "months", 1, MAX_MONTHS, None) if data.get("months") not in (None, "") else None
        with transaction.atomic():
            award, _ = grant(req.user, req.equipment, actor, months=months, reason=str(data.get("note") or "Training completed"), source="request")
            req.status = AuthorisationRequestStatus.APPROVED
            req.award = award
            req.decided_by, req.decided_at = actor, now
            req.staff_note = str(data.get("note") or req.staff_note)[:2000]
            req.save()
            audit(actor, "authorisation.request_approved", req, after={"award": award.pk})
        until = notify.fmt_dt(award.valid_until) if award.valid_until else "no expiry"
        _notify_user(req, actor, "Approved", f"You are authorised to book {req.equipment.name} (valid until {until}).")
        return req
    if action == "reject":
        reason = str(data.get("reason") or "").strip()
        if not reason:
            raise TrainingError("A reason is required.", code="reason_required")
        req.status = AuthorisationRequestStatus.REJECTED
        req.decision_reason = reason[:2000]
        req.decided_by, req.decided_at = actor, now
        req.save()
        audit(actor, "authorisation.request_rejected", req, note=reason)
        _notify_user(req, actor, "Rejected", f"Your training request for {req.equipment.name} was not accepted. Reason: {reason}", remarks=reason)
        return req
    raise TrainingError("Unknown action.", status=404, code="not_found")


# ---------------------------------------------------------------------------
# Grants, renewals, revocations
# ---------------------------------------------------------------------------
def grant(user, equipment, actor, *, months: int | None = None, reason: str = "", source: str = "manual"):
    """Authorise ``user``: renew a held award at or above the minimum level, else issue the minimum level.

    Returns ``(award, "granted" | "renewed")``.
    """
    from . import certification

    row = setting_row(equipment)
    level = min_level(row)
    if level is None:
        raise TrainingError("No certification level is configured.")
    months = months or (row.validity_months if row else None) or certification.validity_months_for(level, equipment)
    now = timezone.now()
    held = (
        CertificationAward.objects.filter(user=user, equipment=equipment, status__in=certification.HELD, level__rank__gte=level.rank)
        .order_by("-level__rank", "-awarded_at")
        .first()
    )
    if held is not None:
        before = {"status": held.status, "valid_until": s.iso(held.valid_until)}
        base = held.valid_until if held.valid_until and held.valid_until > now else now
        held.valid_until = base + relativedelta(months=int(months))
        held.status = AwardStatus.ACTIVE
        held.last_used_at = now
        held.save(update_fields=["valid_until", "status", "last_used_at"])
        audit(actor, "authorisation.renewed", held, before=before, after={"valid_until": s.iso(held.valid_until), "source": source}, note=reason)
        return held, "renewed"
    award = certification.issue(user, equipment, level, actor, months=int(months))
    audit(actor, "authorisation.granted", award, after={"source": source, "months": int(months)}, note=reason)
    return award, "granted"


def bulk_grant(equipment, actor, user_ids, *, months=None, reason: str = "") -> dict:
    from iic_booking.users.models import User

    _require_handle(actor, equipment.equipment_id)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required.", code="reason_required")
    ids = [int(i) for i in (user_ids or []) if str(i).isdigit()][:500]
    if not ids:
        raise TrainingError("Choose at least one person.")
    months = _int_in({"months": months}, "months", 1, MAX_MONTHS, None) if months not in (None, "") else None
    out = {"granted": 0, "renewed": 0, "skipped": []}
    for user in User.objects.filter(id__in=ids, is_active=True):
        if is_staff(user):
            out["skipped"].append({"user_id": user.pk, "reason": "Staff accounts do not need authorisation."})
            continue
        with transaction.atomic():
            _, kind = grant(user, equipment, actor, months=months, reason=reason, source="bulk")
        out[kind] += 1
    missing = set(ids) - {u for u in User.objects.filter(id__in=ids, is_active=True).values_list("id", flat=True)}
    out["skipped"] += [{"user_id": i, "reason": "No active account."} for i in sorted(missing)]
    return out


def act_on_award(award: CertificationAward, actor, action: str, data: dict) -> CertificationAward:
    from . import certification

    if not award.equipment_id:
        raise TrainingError("This certification is not tied to an equipment.")
    _require_handle(actor, award.equipment_id)
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise TrainingError("A reason is required.", code="reason_required")
    now = timezone.now()
    before = {"status": award.status, "valid_until": s.iso(award.valid_until)}
    if action == "revoke":
        if award.status in (AwardStatus.REVOKED, AwardStatus.SUPERSEDED):
            raise TrainingError("This authorisation is already closed.")
        award.status = AwardStatus.REVOKED
        award.revoked_at, award.revoked_by, award.revoke_reason = now, actor, reason
        label = "Revoked"
    elif action == "renew":
        if award.status not in (AwardStatus.ACTIVE, AwardStatus.DORMANT, AwardStatus.EXPIRED):
            raise TrainingError("Only active, dormant or expired authorisations can be renewed.")
        row = setting_row(award.equipment)
        months = _int_in(data, "months", 1, MAX_MONTHS, (row.validity_months if row else None) or 24)
        base = award.valid_until if award.valid_until and award.valid_until > now else now
        award.valid_until = base + relativedelta(months=months)
        award.status = AwardStatus.ACTIVE
        award.last_used_at = now
        label = "Renewed"
    else:
        raise TrainingError("Unknown action.", status=404, code="not_found")
    award.save()
    audit(actor, f"authorisation.{action}", award, before=before, after={"status": award.status, "valid_until": s.iso(award.valid_until)}, note=reason)
    certification._notify_status(award, actor, label, f"Authorisation on {award.equipment.name}: {label.lower()}. Reason: {reason}")
    return award


def authorised_users(equipment, *, include_closed: bool = False) -> list[dict]:
    from .certification import effective_status

    row = setting_row(equipment)
    rank = _min_rank(min_level(row))
    qs = CertificationAward.objects.filter(equipment=equipment, level__rank__gte=rank).exclude(status=AwardStatus.SUPERSEDED)
    if not include_closed:
        qs = qs.exclude(status=AwardStatus.REVOKED)
    qs = qs.select_related("user", "user__department", "user__supervisor", "level", "awarded_by").order_by("user__name", "-awarded_at")
    sources = {
        int(a.object_id): (a.after or {}).get("source", "")
        for a in TrainingAuditLog.objects.filter(object_type="CertificationAward", action__in=("authorisation.granted",),
                                                 object_id__in=[str(x.pk) for x in qs])
    }
    now = timezone.now()
    out, seen = [], set()
    for a in qs:
        if a.user_id in seen:
            continue
        seen.add(a.user_id)
        status = effective_status(a)
        expiring = status == AwardStatus.ACTIVE and a.valid_until and a.valid_until <= now + timedelta(days=EXPIRING_DAYS)
        out.append({
            **s.award_out(a),
            "status": status,
            "status_label": "Expiring soon" if expiring else (AwardStatus(status).label if status in AwardStatus.values else status),
            "expiring": bool(expiring),
            "supervisor": s.user_brief(a.user.supervisor) if a.user.supervisor_id else None,
            "source": sources.get(a.pk) or ("assessment" if a.source_event_id or a.assessments.exists() else "certificate"),
            "awarded_by": s.user_brief(a.awarded_by) if a.awarded_by_id else None,
        })
    return out


def export_csv(equipment) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["User ID", "Name", "Email", "Department", "Supervisor", "Level", "Status", "Authorised on", "Valid until",
                "Certificate no.", "Source"])
    for r in authorised_users(equipment, include_closed=True):
        u = r["user"] or {}
        w.writerow([u.get("id"), u.get("name"), u.get("email"), u.get("department"), (r.get("supervisor") or {}).get("name", ""),
                    r["level_name"], r["status_label"], (r["awarded_at"] or "")[:10], (r["valid_until"] or "")[:10],
                    r["certificate_no"], r["source"]])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Grandfathering
# ---------------------------------------------------------------------------
def grandfather_candidates(equipment, *, min_bookings: int, months: int) -> list[dict]:
    from iic_booking.users.models import User

    row = setting_row(equipment) or EquipmentAuthorisationSetting(equipment=equipment)
    rank = _min_rank(min_level(row))
    counts = completed_bookings(None, equipment, months)
    ids = [uid for uid, c in counts.items() if c["count"] >= min_bookings]
    users = User.objects.filter(id__in=ids, is_active=True).select_related("department")
    if row.applies_to == AuthorisationAppliesTo.INTERNAL:
        users = users.filter(user_type__in=list(UserType.get_internal_user_codes()))
    else:
        users = users.exclude(user_type__in=list(UserType.get_management_user_codes()))
    now = timezone.now()
    authorised = set(
        CertificationAward.objects.filter(equipment=equipment, user_id__in=ids, status__in=AUTHORISED_STATUSES, level__rank__gte=rank)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .values_list("user_id", flat=True)
    )
    out = []
    for u in users.order_by("name"):
        c = counts[u.pk]
        out.append({"user": s.user_brief(u), "completed_bookings": c["count"], "last_completed": s.iso(c["last"]),
                    "already_authorised": u.pk in authorised})
    return out


def grandfather(equipment, actor, data: dict, *, apply: bool) -> dict:
    if apply:
        _require_configure(actor, equipment.equipment_id)
    else:
        _require_handle(actor, equipment.equipment_id)
    min_bookings = _int_in(data, "min_bookings", 1, 50, 2)
    months = _int_in(data, "months", 1, MAX_MONTHS, 12)
    rows = grandfather_candidates(equipment, min_bookings=min_bookings, months=months)
    eligible = [r for r in rows if not r["already_authorised"]]
    result = {
        "dry_run": not apply,
        "min_bookings": min_bookings,
        "months": months,
        "candidates": rows,
        "would_authorise": len(eligible),
        "already_authorised": len(rows) - len(eligible),
    }
    if not apply:
        return result
    if str(data.get("confirm") or "") != "GRANDFATHER":
        raise TrainingError('Type GRANDFATHER to confirm.', code="confirm_required")
    chosen = data.get("user_ids")
    if isinstance(chosen, list):
        keep = {int(i) for i in chosen if str(i).isdigit()}
        eligible = [r for r in eligible if r["user"]["id"] in keep]
    from iic_booking.users.models import User

    row = setting_row(equipment)
    months_valid = _int_in(data, "validity_months", 1, MAX_MONTHS, (row.validity_months if row else 24))
    reason = f"Grandfathered: {min_bookings}+ completed bookings in the last {months} months"
    granted = 0
    for r in eligible:
        user = User.objects.get(pk=r["user"]["id"])
        with transaction.atomic():
            grant(user, equipment, actor, months=months_valid, reason=reason, source="grandfathered")
        granted += 1
    audit(actor, "authorisation.grandfathered", row or equipment, after={"equipment": equipment.pk, "granted": granted,
                                                                          "min_bookings": min_bookings, "months": months})
    result.update(dry_run=False, granted=granted)
    return result


# ---------------------------------------------------------------------------
# Reminders (30 and 7 days before expiry); expiry itself is certification.housekeeping
# ---------------------------------------------------------------------------
def send_expiry_reminders(now=None) -> dict:
    from . import certification

    now = now or timezone.now()
    gated = gated_equipment_ids()
    if not gated:
        return {"reminded": 0}
    sent = {
        (r["object_id"], r["action"])
        for r in TrainingAuditLog.objects.filter(
            object_type="CertificationAward",
            action__in=("award.expiry_reminded", *(f"authorisation.expiry_reminder_{d}" for d in REMINDER_DAYS)),
        ).values("object_id", "action")
    }
    reminded = 0
    horizon = now + timedelta(days=max(REMINDER_DAYS))
    awards = CertificationAward.objects.filter(
        equipment_id__in=gated, status=AwardStatus.ACTIVE, valid_until__gt=now, valid_until__lte=horizon
    ).select_related("user", "user__supervisor", "equipment", "level")
    for award in awards:
        days_left = (award.valid_until - now).total_seconds() / 86400
        due = min(d for d in REMINDER_DAYS if days_left <= d)
        key = str(award.pk)
        if (key, f"authorisation.expiry_reminder_{due}") in sent:
            continue
        if due == max(REMINDER_DAYS) and (key, "award.expiry_reminded") in sent:
            continue
        audit(None, f"authorisation.expiry_reminder_{due}", award)
        until = notify.fmt_dt(award.valid_until)
        notify.send(
            "certification_expiring_email",
            [award.user],
            context=certification._award_context(
                award,
                summary=f"Your authorisation to book {award.equipment.name} expires on {until}. Request refresher training "
                        "so you can keep booking it.",
                deadline=until,
            ),
            title=f"Authorisation expiring: {award.equipment.name}",
            message=f"Your authorisation for {award.equipment.name} expires on {until} ({due}-day reminder).",
            path="/my-authorisations",
            event="training.authorisation.expiring",
        )
        reminded += 1
    return {"reminded": reminded}


# ---------------------------------------------------------------------------
# Supervisor and personal views
# ---------------------------------------------------------------------------
def gated_equipment_queryset():
    from iic_booking.equipment.models import Equipment

    return Equipment.objects.filter(equipment_id__in=gated_equipment_ids()).select_related("internal_department").order_by("name")


def my_overview(user) -> dict:
    gated = list(gated_equipment_queryset())
    st = statuses_for(user, gated)
    awards = CertificationAward.objects.filter(user=user).exclude(status=AwardStatus.SUPERSEDED).select_related("user", "equipment", "level")
    reqs = AuthorisationRequest.objects.filter(user=user).select_related("user", "equipment", "decided_by")[:100]
    return {
        "equipment": [{"equipment": s.equipment_brief(eq), **st[eq.equipment_id]} for eq in gated if st[eq.equipment_id]["required"]],
        "certifications": [s.award_out(a) for a in awards],
        "requests": [request_out(r, user) for r in reqs],
    }


def supervisor_overview(faculty) -> dict:
    from iic_booking.users.models import User

    ids = access.faculty_group_student_ids(faculty)
    students = list(User.objects.filter(id__in=ids, is_active=True).select_related("department").order_by("name"))
    gated = list(gated_equipment_queryset())
    rows = []
    for st_user in students:
        statuses = statuses_for(st_user, gated)
        cells = [{"equipment_id": eq.equipment_id, **{k: statuses[eq.equipment_id][k] for k in ("status", "status_label", "valid_until", "required", "open_request")}}
                 for eq in gated]
        rows.append({"student": s.user_brief(st_user), "equipment": cells})
    return {"equipment": [s.equipment_brief(e) for e in gated], "students": rows}
