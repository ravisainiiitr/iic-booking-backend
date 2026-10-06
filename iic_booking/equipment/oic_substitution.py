"""OIC Substitute: the permanent OIC of an equipment lets other OICs of the same department manage it
for a period, keeping their own access.

Built on ``EquipmentTemporaryOIC``. Access is evaluated at check time through
``EquipmentTemporaryOIC.objects.active()`` (used by ``get_equipment_ids_managed_by_oic`` and every
other OIC check), so it never applies outside the period. The ``equipment.expire_oic_substitutions``
job only records the expiry and sends the notifications. Rows are never deleted; every change is
written to ``EquipmentTemporaryOICEvent``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import (
    Equipment,
    EquipmentManager,
    EquipmentTemporaryOIC,
    EquipmentTemporaryOICEvent,
)

logger = logging.getLogger(__name__)

MAX_SUBSTITUTES = 5
MAX_PERIOD_DAYS = 366
REASON_MAX_LENGTH = 2000
CANDIDATE_LIMIT = 25
# The expiry job does not email about periods that ended longer ago than this (e.g. after an outage).
EXPIRY_NOTIFY_WINDOW = timedelta(days=2)
PAGE_PATH = "/oic-substitute"

TEMPLATE_ASSIGNED = "oic_substitute_assigned_email"
TEMPLATE_ENDED = "oic_substitute_ended_email"
TEMPLATE_LAB_STAFF = "oic_substitute_lab_staff_email"
TEMPLATE_OIC_COPY = "oic_substitute_oic_copy_email"

Status = EquipmentTemporaryOIC.Status
Action = EquipmentTemporaryOICEvent.Action


class SubstitutionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --------------------------------------------------------------------------- roles


def is_main_admin(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "user_type", None) == UserType.ADMIN or getattr(user, "is_superuser", False))
    )


def is_oic_user(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and getattr(user, "user_type", None) == UserType.MANAGER)


def permanent_oic_equipment_queryset(user):
    """Equipment the user is the permanent OIC of (substitute access does not count)."""
    ids = EquipmentManager.objects.filter(manager_id=user.pk).values_list("equipment_id", flat=True)
    return Equipment.objects.filter(pk__in=ids).order_by("code", "name")


def same_department_oics(user):
    """Active OIC users of the user's department, excluding the user. Empty when the user has no department."""
    from iic_booking.users.models.user import User

    dept_id = getattr(user, "department_id", None)
    if not dept_id:
        return User.objects.none()
    return (
        User.objects.filter(user_type=UserType.MANAGER, is_active=True, department_id=dept_id)
        .exclude(pk=user.pk)
        .order_by("name", "email")
    )


def search_candidates(user, search: str = ""):
    qs = same_department_oics(user)
    search = (search or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))
    return qs[:CANDIDATE_LIMIT]


# --------------------------------------------------------------------------- period


def _parse_date(value, label: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SubstitutionError(f"{label} must be a date (YYYY-MM-DD).") from None


def parse_period(start_date, end_date, *, now=None) -> tuple[datetime, datetime]:
    """(start_at, resume_at) for whole days in IST. Starting today starts now; access ends at the end
    of ``end_date`` (midnight IST)."""
    now = now or timezone.now()
    start = _parse_date(start_date, "Start date")
    end = _parse_date(end_date, "End date")
    today = timezone.localdate(now)
    if start < today:
        raise SubstitutionError("The start date cannot be in the past.")
    if end < start:
        raise SubstitutionError("The end date must be on or after the start date.")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise SubstitutionError(f"The period can be at most {MAX_PERIOD_DAYS} days.")
    tz = timezone.get_current_timezone()
    start_at = now if start == today else timezone.make_aware(datetime.combine(start, time.min), tz)
    resume_at = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    return start_at, resume_at


def _clean_reason(reason, *, label: str = "A reason") -> str:
    text = str(reason or "").strip()
    if not text:
        raise SubstitutionError(f"{label} is required.")
    if len(text) > REASON_MAX_LENGTH:
        raise SubstitutionError(f"Please keep the reason under {REASON_MAX_LENGTH} characters.")
    return text


def _overlapping(equipment_id: int, substitute_ids, start_at, resume_at, *, now, exclude_pk=None):
    qs = (
        EquipmentTemporaryOIC.objects.open(now)
        .filter(equipment_id=equipment_id, temporary_oic_id__in=list(substitute_ids), resume_at__gt=start_at)
        .filter(Q(start_at__isnull=True) | Q(start_at__lt=resume_at))
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs


def _record(delegation, action: str, *, actor=None, reason: str = "", details: dict | None = None):
    return EquipmentTemporaryOICEvent.objects.create(
        delegation=delegation, action=action, actor=actor, reason=reason, details=details or {}
    )


def _period_details(start_at, resume_at) -> dict:
    return {"start_at": start_at.isoformat() if start_at else None, "resume_at": resume_at.isoformat()}


# --------------------------------------------------------------------------- actions


def create_substitution(
    *,
    primary,
    equipment_id,
    substitute_ids,
    reason,
    start_date=None,
    end_date=None,
    period: tuple[datetime, datetime] | None = None,
    now=None,
) -> list[EquipmentTemporaryOIC]:
    """Create one delegation per substitute (sharing a batch id) and notify after commit.

    ``start_date``/``end_date`` are whole IST days; ``period`` (start_at, resume_at) is used by the
    legacy Temporary OIC endpoint, which starts now and ends at a chosen time."""
    now = now or timezone.now()
    if not is_oic_user(primary):
        raise SubstitutionError("Only an Officer in Charge (OIC) can assign a substitute.", 403)
    try:
        equipment = Equipment.objects.get(pk=int(equipment_id))
    except (Equipment.DoesNotExist, TypeError, ValueError):
        raise SubstitutionError("Select one of your equipment.", 404) from None
    if not EquipmentManager.objects.filter(equipment=equipment, manager=primary).exists():
        raise SubstitutionError("You can only assign a substitute for equipment you are the OIC of.", 403)

    raw_ids = substitute_ids if isinstance(substitute_ids, (list, tuple)) else [substitute_ids]
    try:
        ids = list(dict.fromkeys(int(x) for x in raw_ids if x not in (None, "")))
    except (TypeError, ValueError):
        raise SubstitutionError("Select the substitute OIC(s) from the search.") from None
    if not ids:
        raise SubstitutionError("Select at least one substitute OIC.")
    if len(ids) > MAX_SUBSTITUTES:
        raise SubstitutionError(f"Select at most {MAX_SUBSTITUTES} substitute OICs.")
    if primary.pk in ids:
        raise SubstitutionError("You cannot assign yourself as a substitute.")
    if not getattr(primary, "department_id", None):
        raise SubstitutionError(
            "Your profile has no department, so substitutes cannot be chosen. Please contact the Main Administrator."
        )
    substitutes = list(same_department_oics(primary).filter(pk__in=ids))
    if len(substitutes) != len(ids):
        raise SubstitutionError("Substitutes must be active OICs of your department.")
    already_oic = set(
        EquipmentManager.objects.filter(equipment=equipment, manager_id__in=ids).values_list("manager_id", flat=True)
    )
    if already_oic:
        raise SubstitutionError("A selected user is already an OIC of this equipment.")

    if period is not None:
        start_at, resume_at = period
        if resume_at <= now:
            raise SubstitutionError("Resume date and time must be in the future.")
        if resume_at - start_at > timedelta(days=MAX_PERIOD_DAYS):
            raise SubstitutionError(f"The period can be at most {MAX_PERIOD_DAYS} days.")
    else:
        start_at, resume_at = parse_period(start_date, end_date, now=now)
    reason = _clean_reason(reason)

    with transaction.atomic():
        clash = _overlapping(equipment.pk, ids, start_at, resume_at, now=now).select_related("temporary_oic").first()
        if clash is not None:
            from iic_booking.users.display import get_user_display_name

            raise SubstitutionError(
                f"{get_user_display_name(clash.temporary_oic) or 'A selected OIC'} already has substitute access to "
                f"this equipment for an overlapping period ({format_period(clash)}). Revoke it first or choose other dates.",
                409,
            )
        batch = uuid.uuid4()
        rows = []
        for sub in substitutes:
            row = EquipmentTemporaryOIC.objects.create(
                equipment=equipment,
                primary_oic=primary,
                temporary_oic=sub,
                start_at=start_at,
                resume_at=resume_at,
                reason=reason,
                batch_id=batch,
                created_by=primary,
            )
            _record(
                row,
                Action.CREATED,
                actor=primary,
                reason=reason,
                details={**_period_details(start_at, resume_at), "substitute_ids": [s.pk for s in substitutes]},
            )
            rows.append(row)
        _after_commit(lambda: notify_created(rows, actor=primary))
    return rows


def can_end(user, delegation) -> bool:
    return is_main_admin(user) or (is_oic_user(user) and delegation.primary_oic_id == user.pk)


def end_substitution(*, delegation, actor, reason, now=None) -> EquipmentTemporaryOIC:
    """Cancel (not started yet) or revoke (in progress) with a reason; access stops immediately."""
    now = now or timezone.now()
    if not can_end(actor, delegation):
        raise SubstitutionError("Only the OIC who assigned this substitute or the Main Administrator can end it.", 403)
    reason = _clean_reason(reason)
    if delegation.status != Status.ACTIVE or delegation.resume_at <= now:
        raise SubstitutionError("This substitution has already ended.", 409)
    new_status = Status.CANCELLED if delegation.start_at and delegation.start_at > now else Status.REVOKED
    with transaction.atomic():
        updated = EquipmentTemporaryOIC.objects.filter(pk=delegation.pk, status=Status.ACTIVE).update(
            status=new_status, ended_at=now, ended_by=actor, end_reason=reason
        )
        if not updated:
            raise SubstitutionError("This substitution has already ended.", 409)
        delegation.refresh_from_db()
        _record(
            delegation,
            Action.CANCELLED if new_status == Status.CANCELLED else Action.REVOKED,
            actor=actor,
            reason=reason,
            details={"by_main_admin": is_main_admin(actor) and actor.pk != delegation.primary_oic_id},
        )
        _after_commit(lambda: notify_ended(delegation, actor=actor))
    return delegation


def change_resume_at(*, delegation, actor, resume_at, now=None) -> EquipmentTemporaryOIC:
    """Legacy Temporary OIC edit: move the end of an open delegation."""
    now = now or timezone.now()
    if delegation.status != Status.ACTIVE or delegation.resume_at <= now:
        raise SubstitutionError("This substitution has already ended.", 409)
    if resume_at <= now or (delegation.start_at and resume_at <= delegation.start_at):
        raise SubstitutionError("Resume date and time must be in the future and after the start.")
    if _overlapping(
        delegation.equipment_id, [delegation.temporary_oic_id], delegation.effective_start(), resume_at,
        now=now, exclude_pk=delegation.pk,
    ).exists():
        raise SubstitutionError("This would overlap another substitution for the same OIC.", 409)
    old = delegation.resume_at
    with transaction.atomic():
        delegation.resume_at = resume_at
        delegation.save(update_fields=["resume_at"])
        _record(
            delegation,
            Action.PERIOD_CHANGED,
            actor=actor,
            details={"old_resume_at": old.isoformat(), "resume_at": resume_at.isoformat()},
        )
    return delegation


def expire_due_substitutions(now=None) -> int:
    """Mark delegations past resume_at as expired and notify. Access already stopped at resume_at."""
    now = now or timezone.now()
    count = 0
    due = (
        EquipmentTemporaryOIC.objects.filter(status=Status.ACTIVE, resume_at__lte=now)
        .select_related("equipment", "primary_oic", "temporary_oic")
        .order_by("resume_at")
    )
    for row in due:
        with transaction.atomic():
            updated = EquipmentTemporaryOIC.objects.filter(pk=row.pk, status=Status.ACTIVE).update(
                status=Status.EXPIRED, ended_at=row.resume_at
            )
            if not updated:
                continue
            row.status = Status.EXPIRED
            row.ended_at = row.resume_at
            _record(row, Action.EXPIRED, reason="The period ended.")
        count += 1
        if row.resume_at >= now - EXPIRY_NOTIFY_WINDOW:
            notify_ended(row, actor=None)
    return count


# --------------------------------------------------------------------------- display


def _end_display_dt(resume_at):
    """Midnight ends are shown as 11:59 PM of the previous day."""
    local = timezone.localtime(resume_at)
    if local.hour == 0 and local.minute == 0:
        return local - timedelta(minutes=1)
    return local


def format_start(delegation) -> str:
    from iic_booking.communication.email_branding import format_email_datetime

    return format_email_datetime(delegation.effective_start())


def format_end(delegation) -> str:
    from iic_booking.communication.email_branding import format_email_datetime

    return format_email_datetime(_end_display_dt(delegation.resume_at))


def format_period(delegation) -> str:
    return f"{format_start(delegation)} to {format_end(delegation)} IST"


STATUS_LABELS = {
    "scheduled": "Scheduled",
    Status.ACTIVE: "Active",
    Status.EXPIRED: "Expired",
    Status.CANCELLED: "Cancelled",
    Status.REVOKED: "Revoked",
}


def _person(user) -> dict | None:
    if user is None:
        return None
    from iic_booking.users.display import get_user_display_name

    return {"id": user.pk, "name": get_user_display_name(user) or "", "email": user.email or ""}


def delegation_to_dict(d, *, viewer=None, now=None) -> dict:
    from iic_booking.users.display import get_user_display_name

    now = now or timezone.now()
    status = d.display_status(now)
    events = [
        {
            "id": e.pk,
            "action": e.action,
            "action_label": e.get_action_display(),
            "actor_name": (get_user_display_name(e.actor) or "") if e.actor_id else "System",
            "reason": e.reason,
            "created_at": e.created_at.isoformat(),
        }
        for e in d.events.all()
    ]
    return {
        "id": d.pk,
        "batch_id": str(d.batch_id) if d.batch_id else None,
        "equipment": {"id": d.equipment_id, "code": d.equipment.code or "", "name": d.equipment.name or ""},
        "primary_oic": _person(d.primary_oic),
        "substitute": _person(d.temporary_oic),
        "start_at": d.effective_start().isoformat(),
        "resume_at": d.resume_at.isoformat(),
        "start_display": format_start(d),
        "end_display": format_end(d),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "reason": d.reason,
        "created_at": d.created_at.isoformat(),
        "created_by": _person(d.created_by) if d.created_by_id else None,
        "ended_at": d.ended_at.isoformat() if d.ended_at else None,
        "ended_by": _person(d.ended_by) if d.ended_by_id else None,
        "end_reason": d.end_reason,
        "can_end": bool(viewer) and status in ("scheduled", Status.ACTIVE) and can_end(viewer, d),
        "events": events,
    }


def delegation_queryset():
    return EquipmentTemporaryOIC.objects.select_related(
        "equipment", "primary_oic", "temporary_oic", "created_by", "ended_by"
    ).prefetch_related("events__actor")


# --------------------------------------------------------------------------- notifications


def _after_commit(fn) -> None:
    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(fn)
    else:
        fn()


def _send_email(user, code: str, context: dict, *, actor=None, delegation_ids=()) -> None:
    if user is None or not getattr(user, "is_active", False) or not (getattr(user, "email", "") or "").strip():
        return
    try:
        from iic_booking.communication.email_branding import user_display_name
        from iic_booking.communication.service import CommunicationService

        from .booking_lab_messages import ensure_email_template

        ensure_email_template(code)
        CommunicationService.send_email(
            recipient=user,
            template=code,
            template_context={"user_name": user_display_name(user), **context},
            metadata={"event": code, "delegation_ids": list(delegation_ids)},
            created_by=actor,
        )
    except Exception:
        logger.exception("OIC substitute email %s failed for user_id=%s", code, getattr(user, "pk", None))


def _link() -> str:
    from iic_booking.communication.email_branding import absolute_http_url
    from iic_booking.communication.utils import get_frontend_absolute_url

    return absolute_http_url(get_frontend_absolute_url(PAGE_PATH))


def _base_context(rows) -> dict:
    from iic_booking.users.display import get_user_display_name

    first = rows[0]
    names = ", ".join(get_user_display_name(r.temporary_oic) or "OIC" for r in rows)
    return {
        "equipment_name": first.equipment.name or first.equipment.code or "",
        "equipment_code": first.equipment.code or "",
        "oic_name": get_user_display_name(first.primary_oic) or "",
        "granted_by_name": get_user_display_name(first.created_by or first.primary_oic) or "",
        "substitute_names": names,
        "start_display": format_start(first),
        "end_display": format_end(first),
        "period_display": format_period(first),
        "reason": first.reason or "",
        "link": _link(),
    }


def _lab_staff(equipment, exclude_ids) -> list:
    from .reports import get_equipment_lab_incharge_users

    return [u for u in get_equipment_lab_incharge_users(equipment) if u.pk not in exclude_ids]


def notify_created(rows, *, actor=None) -> None:
    if not rows:
        return
    from iic_booking.communication.in_app import notify_in_app

    ctx = _base_context(rows)
    ids = [r.pk for r in rows]
    equipment = rows[0].equipment
    primary = rows[0].primary_oic
    period = ctx["period_display"]
    for row in rows:
        _send_email(row.temporary_oic, TEMPLATE_ASSIGNED, ctx, actor=actor, delegation_ids=[row.pk])
    notify_in_app(
        [r.temporary_oic for r in rows],
        title=f"You are an OIC substitute for {ctx['equipment_name']}",
        message=(
            f"{ctx['granted_by_name']} has given you OIC access to {ctx['equipment_name']} from {period}. "
            f"Reason: {ctx['reason']}"
        ),
        link=PAGE_PATH,
        event="oic_substitute_assigned",
        created_by=actor,
        extra={"delegation_ids": ids},
    )

    summary = (
        f"{ctx['granted_by_name']} has made {ctx['substitute_names']} OIC substitute for {ctx['equipment_name']} "
        f"from {period}. During this period they can manage the equipment's bookings and settings as OIC, "
        f"together with {ctx['oic_name']}."
    )
    change_ctx = {**ctx, "change_title": "OIC substitute assigned", "summary": summary, "status_display": "Assigned"}
    exclude = {primary.pk, *(r.temporary_oic_id for r in rows)}
    staff = _lab_staff(equipment, exclude)
    for user in staff:
        _send_email(user, TEMPLATE_LAB_STAFF, change_ctx, actor=actor, delegation_ids=ids)
    notify_in_app(
        staff,
        title=f"OIC substitute for {ctx['equipment_name']}",
        message=summary,
        event="oic_substitute_assigned",
        created_by=actor,
        extra={"delegation_ids": ids},
    )
    _send_email(primary, TEMPLATE_OIC_COPY, change_ctx, actor=actor, delegation_ids=ids)


_END_PHRASES = {
    Status.CANCELLED: ("OIC substitute cancelled", "was cancelled before it started", "Cancelled"),
    Status.REVOKED: ("OIC substitute access revoked", "was revoked", "Revoked"),
    Status.EXPIRED: ("OIC substitute period ended", "has ended because the period is over", "Expired"),
}


def notify_ended(row, *, actor=None) -> None:
    from iic_booking.communication.email_branding import format_email_datetime
    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.users.display import get_user_display_name

    title, phrase, status_display = _END_PHRASES.get(row.status, _END_PHRASES[Status.REVOKED])
    ctx = _base_context([row])
    sub_name = ctx["substitute_names"]
    ended_by = get_user_display_name(actor) if actor else ""
    ctx.update(
        {
            "change_title": title,
            "status_display": status_display,
            "end_phrase": phrase,
            "ended_by_name": ended_by,
            "ended_at_display": format_email_datetime(row.ended_at) if row.ended_at else "",
            "end_reason": row.end_reason or "",
        }
    )
    by = f" by {ended_by}" if ended_by else ""
    reason_txt = f" Reason: {row.end_reason}" if row.end_reason else ""

    _send_email(row.temporary_oic, TEMPLATE_ENDED, ctx, actor=actor, delegation_ids=[row.pk])
    notify_in_app(
        [row.temporary_oic],
        title=f"{title}: {ctx['equipment_name']}",
        message=f"Your OIC substitute access to {ctx['equipment_name']} {phrase}{by}.{reason_txt}",
        link=PAGE_PATH,
        event="oic_substitute_ended",
        created_by=actor,
        extra={"delegation_ids": [row.pk], "status": row.status},
    )

    summary = (
        f"The OIC substitute access of {sub_name} to {ctx['equipment_name']} ({ctx['period_display']}) {phrase}{by}. "
        f"The equipment is managed by {ctx['oic_name']} again."
    )
    change_ctx = {**ctx, "summary": summary}
    staff = _lab_staff(row.equipment, {row.primary_oic_id, row.temporary_oic_id})
    for user in staff:
        _send_email(user, TEMPLATE_LAB_STAFF, change_ctx, actor=actor, delegation_ids=[row.pk])
    notify_in_app(
        staff,
        title=f"{title}: {ctx['equipment_name']}",
        message=summary,
        event="oic_substitute_ended",
        created_by=actor,
        extra={"delegation_ids": [row.pk], "status": row.status},
    )
    _send_email(row.primary_oic, TEMPLATE_OIC_COPY, change_ctx, actor=actor, delegation_ids=[row.pk])
    if actor is None or actor.pk != row.primary_oic_id:
        notify_in_app(
            [row.primary_oic],
            title=f"{title}: {ctx['equipment_name']}",
            message=summary,
            link=PAGE_PATH,
            event="oic_substitute_ended",
            created_by=actor,
            extra={"delegation_ids": [row.pk], "status": row.status},
        )
