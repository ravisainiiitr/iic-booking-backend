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
CANDIDATE_LIMIT_MAX = 200
MAX_BULK_ROWS = 100
MAX_BULK_END = 200
# The expiry job does not email about periods that ended longer ago than this (e.g. after an outage).
EXPIRY_NOTIFY_WINDOW = timedelta(days=2)
PAGE_PATH = "/oic-substitute"

TEMPLATE_ASSIGNED = "oic_substitute_assigned_email"
TEMPLATE_ENDED = "oic_substitute_ended_email"
TEMPLATE_LAB_STAFF = "oic_substitute_lab_staff_email"
TEMPLATE_OIC_COPY = "oic_substitute_oic_copy_email"
TEMPLATE_BULK_ASSIGNED = "oic_substitute_bulk_assigned_email"
TEMPLATE_BULK_ENDED = "oic_substitute_bulk_ended_email"
TEMPLATE_BULK_LAB_STAFF = "oic_substitute_bulk_lab_staff_email"
TEMPLATE_BULK_OIC_COPY = "oic_substitute_bulk_oic_copy_email"

Status = EquipmentTemporaryOIC.Status
Action = EquipmentTemporaryOICEvent.Action


class SubstitutionError(Exception):
    def __init__(self, message: str, status_code: int = 400, row_errors: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.row_errors = row_errors or []


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


def search_candidates(user, search: str = "", limit: int | None = None):
    qs = same_department_oics(user)
    search = (search or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))
    return qs[: max(1, min(int(limit or CANDIDATE_LIMIT), CANDIDATE_LIMIT_MAX))]


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


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bulk_row_error(index: int, message: str, *, equipment_id=None, substitute_id=None, field: str = "") -> dict:
    return {
        "index": index,
        "equipment_id": equipment_id,
        "substitute_id": substitute_id,
        "field": field,
        "message": message,
    }


def create_substitutions_bulk(
    *, primary, assignments, reason, start_date=None, end_date=None, now=None
) -> list[EquipmentTemporaryOIC]:
    """Assign substitutes for several equipment in one go, all or nothing.

    ``assignments`` is a list of ``{"equipment_id", "substitute_ids", "start_date"?, "end_date"?}``; a row
    without dates uses the shared ``start_date``/``end_date``. Every (equipment, substitute) pair is checked
    with the same rules as a single assignment. Any problem raises ``SubstitutionError`` with ``row_errors``
    and nothing is saved. Notifications are grouped per recipient after commit."""
    from iic_booking.users.display import get_user_display_name

    now = now or timezone.now()
    if not is_oic_user(primary):
        raise SubstitutionError("Only an Officer in Charge (OIC) can assign a substitute.", 403)
    if not getattr(primary, "department_id", None):
        raise SubstitutionError(
            "Your profile has no department, so substitutes cannot be chosen. Please contact the Main Administrator."
        )
    if not isinstance(assignments, (list, tuple)) or not assignments:
        raise SubstitutionError("Select at least one equipment and its substitute.")
    if len(assignments) > MAX_BULK_ROWS:
        raise SubstitutionError(f"Assign at most {MAX_BULK_ROWS} equipment at a time.")
    reason = _clean_reason(reason)

    own_ids = set(EquipmentManager.objects.filter(manager_id=primary.pk).values_list("equipment_id", flat=True))
    candidates = {u.pk: u for u in same_department_oics(primary)}
    errors: list[dict] = []
    plan: list[tuple[int, list[int], datetime, datetime]] = []
    seen_equipment: set[int] = set()

    for index, raw in enumerate(assignments):
        if not isinstance(raw, dict):
            errors.append(_bulk_row_error(index, "Each row needs an equipment and a substitute."))
            continue
        eq_id = _int_or_none(raw.get("equipment_id"))
        row_ok = True
        if eq_id is None or eq_id not in own_ids:
            errors.append(
                _bulk_row_error(
                    index, "You can only assign a substitute for equipment you are the OIC of.",
                    equipment_id=eq_id, field="equipment_id",
                )
            )
            continue
        if eq_id in seen_equipment:
            errors.append(
                _bulk_row_error(
                    index, "This equipment is listed twice. Put all its substitutes in one row.",
                    equipment_id=eq_id, field="equipment_id",
                )
            )
            continue
        seen_equipment.add(eq_id)

        raw_subs = raw.get("substitute_ids")
        if raw_subs is None and raw.get("substitute_id") not in (None, ""):
            raw_subs = [raw.get("substitute_id")]
        raw_subs = raw_subs if isinstance(raw_subs, (list, tuple)) else [raw_subs]
        sub_ids = list(dict.fromkeys(i for i in (_int_or_none(x) for x in raw_subs if x not in (None, "")) if i))
        if not sub_ids:
            errors.append(_bulk_row_error(index, "Select a substitute OIC.", equipment_id=eq_id, field="substitute_ids"))
            row_ok = False
        elif len(sub_ids) > MAX_SUBSTITUTES:
            errors.append(
                _bulk_row_error(
                    index, f"Select at most {MAX_SUBSTITUTES} substitute OICs.", equipment_id=eq_id, field="substitute_ids"
                )
            )
            row_ok = False

        already_oic = set(
            EquipmentManager.objects.filter(equipment_id=eq_id, manager_id__in=sub_ids).values_list("manager_id", flat=True)
        )
        for sid in sub_ids:
            if sid == primary.pk:
                problem = "You cannot assign yourself as a substitute."
            elif sid not in candidates:
                problem = "Substitutes must be active OICs of your department."
            elif sid in already_oic:
                problem = f"{get_user_display_name(candidates[sid]) or 'This user'} is already an OIC of this equipment."
            else:
                continue
            errors.append(_bulk_row_error(index, problem, equipment_id=eq_id, substitute_id=sid, field="substitute_ids"))
            row_ok = False

        has_own_dates = bool(raw.get("start_date") or raw.get("end_date"))
        try:
            start_at, resume_at = parse_period(
                raw.get("start_date") if has_own_dates else start_date,
                raw.get("end_date") if has_own_dates else end_date,
                now=now,
            )
        except SubstitutionError as exc:
            errors.append(_bulk_row_error(index, exc.message, equipment_id=eq_id, field="period"))
            continue
        if not row_ok:
            continue

        for clash in _overlapping(eq_id, sub_ids, start_at, resume_at, now=now).select_related("temporary_oic"):
            errors.append(
                _bulk_row_error(
                    index,
                    f"{get_user_display_name(clash.temporary_oic) or 'This OIC'} already has substitute access to this "
                    f"equipment for an overlapping period ({format_period(clash)}). Revoke it first or choose other dates.",
                    equipment_id=eq_id, substitute_id=clash.temporary_oic_id, field="period",
                )
            )
            row_ok = False
        if row_ok:
            plan.append((eq_id, sub_ids, start_at, resume_at))

    if errors:
        rows_with_errors = len({e["index"] for e in errors})
        raise SubstitutionError(
            f"Nothing was assigned. Please fix {rows_with_errors} row{'s' if rows_with_errors != 1 else ''} and try again.",
            400,
            row_errors=errors,
        )

    equipment = Equipment.objects.in_bulk([p[0] for p in plan])
    with transaction.atomic():
        for eq_id, sub_ids, start_at, resume_at in plan:
            clash = _overlapping(eq_id, sub_ids, start_at, resume_at, now=now).first()
            if clash is not None:
                raise SubstitutionError(
                    "Another substitution was saved for the same equipment a moment ago. Refresh and try again.", 409
                )
        batch = uuid.uuid4()
        created: list[EquipmentTemporaryOIC] = []
        for eq_id, sub_ids, start_at, resume_at in plan:
            for sid in sub_ids:
                row = EquipmentTemporaryOIC.objects.create(
                    equipment=equipment[eq_id],
                    primary_oic=primary,
                    temporary_oic=candidates[sid],
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
                    details={
                        **_period_details(start_at, resume_at),
                        "substitute_ids": sub_ids,
                        "bulk": True,
                        "batch_equipment_count": len(plan),
                    },
                )
                created.append(row)
        _after_commit(lambda: notify_created_many(created, actor=primary))
    return created


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


def end_substitutions_bulk(*, delegations, actor, reason, now=None) -> list[EquipmentTemporaryOIC]:
    """Cancel / revoke several substitutions with one reason, all or nothing; one grouped notification per
    recipient. ``delegations`` must already be limited to what the actor may see."""
    now = now or timezone.now()
    reason = _clean_reason(reason)
    delegations = list(delegations)
    if not delegations:
        raise SubstitutionError("Select at least one substitution.")
    if len(delegations) > MAX_BULK_END:
        raise SubstitutionError(f"End at most {MAX_BULK_END} substitutions at a time.")
    errors = []
    for d in delegations:
        if not can_end(actor, d):
            message = "Only the OIC who assigned this substitute or the Main Administrator can end it."
        elif d.status != Status.ACTIVE or d.resume_at <= now:
            message = "This substitution has already ended."
        else:
            continue
        errors.append({"id": d.pk, "equipment_id": d.equipment_id, "substitute_id": d.temporary_oic_id, "message": message})
    if errors:
        raise SubstitutionError(
            "Nothing was changed. Some selected substitutions have already ended; refresh and try again.",
            409,
            row_errors=errors,
        )
    by_main_admin = is_main_admin(actor)
    ended: list[EquipmentTemporaryOIC] = []
    with transaction.atomic():
        for d in delegations:
            new_status = Status.CANCELLED if d.start_at and d.start_at > now else Status.REVOKED
            updated = EquipmentTemporaryOIC.objects.filter(pk=d.pk, status=Status.ACTIVE).update(
                status=new_status, ended_at=now, ended_by=actor, end_reason=reason
            )
            if not updated:
                raise SubstitutionError("A selected substitution changed a moment ago. Refresh and try again.", 409)
            d.refresh_from_db()
            _record(
                d,
                Action.CANCELLED if new_status == Status.CANCELLED else Action.REVOKED,
                actor=actor,
                reason=reason,
                details={"by_main_admin": by_main_admin and actor.pk != d.primary_oic_id, "bulk": True},
            )
            ended.append(d)
        _after_commit(lambda: notify_ended_many(ended, actor=actor))
    return ended


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
    to_notify = []
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
            to_notify.append(row)
    # Periods assigned together end together: one grouped message per recipient.
    notify_ended_many(to_notify, actor=None)
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


def _ended_parts(row, actor) -> dict:
    """Context and wording for one ended substitution (single-item messages)."""
    from iic_booking.communication.email_branding import format_email_datetime
    from iic_booking.users.display import get_user_display_name

    title, phrase, status_display = _END_PHRASES.get(row.status, _END_PHRASES[Status.REVOKED])
    ctx = _base_context([row])
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
    summary = (
        f"The OIC substitute access of {ctx['substitute_names']} to {ctx['equipment_name']} ({ctx['period_display']}) "
        f"{phrase}{by}. The equipment is managed by {ctx['oic_name']} again."
    )
    return {
        "ctx": ctx,
        "change_ctx": {**ctx, "summary": summary},
        "title": title,
        "phrase": phrase,
        "by": by,
        "reason_txt": f" Reason: {row.end_reason}" if row.end_reason else "",
        "summary": summary,
    }


def notify_ended(row, *, actor=None) -> None:
    notify_ended_many([row], actor=actor)


def _ended_bulk_ctx(rows, actor, *, lines: list[str], summary: str = "") -> dict:
    from iic_booking.communication.email_branding import format_email_datetime
    from iic_booking.users.display import get_user_display_name

    statuses = {r.status for r in rows}
    title = _END_PHRASES[statuses.pop()][0] if len(statuses) == 1 else "OIC substitute access ended"
    end_reasons = {r.end_reason or "" for r in rows}
    oics = {r.primary_oic_id for r in rows}
    ended_at = {r.ended_at for r in rows if r.ended_at}
    return {
        "change_title": title,
        "summary": summary,
        "equipment_count": len({r.equipment_id for r in rows}),
        "equipment_list": "\n".join(lines),
        "oic_name": (get_user_display_name(rows[0].primary_oic) or "") if len(oics) == 1 else "",
        "ended_by_name": (get_user_display_name(actor) or "") if actor else "",
        "ended_at_display": format_email_datetime(ended_at.pop()) if len(ended_at) == 1 else "",
        "end_reason": end_reasons.pop() if len(end_reasons) == 1 else "",
        "reason": "",
        "link": _link(),
    }


def notify_ended_many(rows, *, actor=None) -> None:
    """Notify about ended substitutions with one message per recipient: a single-item message when the
    recipient is concerned by one substitution, otherwise one combined list."""
    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.users.display import get_user_display_name

    rows = list(rows)
    if not rows:
        return
    parts = {r.pk: _ended_parts(r, actor) for r in rows}
    by_name = get_user_display_name(actor) if actor else ""
    by = f" by {by_name}" if by_name else ""

    def event_extra(group):
        return {"delegation_ids": [r.pk for r in group], "status": group[0].status if len(group) == 1 else "ended"}

    for group in _group_by(rows, lambda r: r.temporary_oic_id).values():
        sub = group[0].temporary_oic
        if len(group) == 1:
            p = parts[group[0].pk]
            _send_email(sub, TEMPLATE_ENDED, p["ctx"], actor=actor, delegation_ids=[group[0].pk])
            notify_in_app(
                [sub],
                title=f"{p['title']}: {p['ctx']['equipment_name']}",
                message=f"Your OIC substitute access to {p['ctx']['equipment_name']} {p['phrase']}{by}.{p['reason_txt']}",
                link=PAGE_PATH,
                event="oic_substitute_ended",
                created_by=actor,
                extra=event_extra(group),
            )
            continue
        lines = [f"• {_equipment_label(r.equipment)}: {_short_period(r)} ({_status_label(r)})" for r in group]
        ctx = _ended_bulk_ctx(group, actor, lines=lines)
        _send_email(sub, TEMPLATE_BULK_ENDED, ctx, actor=actor, delegation_ids=[r.pk for r in group])
        notify_in_app(
            [sub],
            title=f"{ctx['change_title']}: {len(group)} equipment",
            message=f"Your OIC substitute access{by} ended for: " + "; ".join(_equipment_label(r.equipment) for r in group),
            link=PAGE_PATH,
            event="oic_substitute_ended",
            created_by=actor,
            extra=event_extra(group),
        )

    staff_rows: dict[int, tuple] = {}
    staff_cache: dict[int, list] = {}
    for r in rows:
        if r.equipment_id not in staff_cache:
            staff_cache[r.equipment_id] = _lab_staff(r.equipment, set())
        for user in staff_cache[r.equipment_id]:
            if user.pk in (r.primary_oic_id, r.temporary_oic_id):
                continue
            staff_rows.setdefault(user.pk, (user, []))[1].append(r)
    for user, group in staff_rows.values():
        if len(group) == 1:
            p = parts[group[0].pk]
            _send_email(user, TEMPLATE_LAB_STAFF, p["change_ctx"], actor=actor, delegation_ids=[group[0].pk])
            notify_in_app(
                [user],
                title=f"{p['title']}: {p['ctx']['equipment_name']}",
                message=p["summary"],
                event="oic_substitute_ended",
                created_by=actor,
                extra=event_extra(group),
            )
            continue
        summary = (
            f"OIC substitute access{by} ended for {len(group)} substitution{'s' if len(group) != 1 else ''} on equipment "
            "you look after. Each equipment is managed by its OIC again."
        )
        ctx = _ended_bulk_ctx(group, actor, lines=_ended_lines_with_names(group), summary=summary)
        _send_email(user, TEMPLATE_BULK_LAB_STAFF, ctx, actor=actor, delegation_ids=[r.pk for r in group])
        notify_in_app(
            [user],
            title=f"{ctx['change_title']}: {ctx['equipment_count']} equipment",
            message=summary,
            event="oic_substitute_ended",
            created_by=actor,
            extra=event_extra(group),
        )

    for group in _group_by(rows, lambda r: r.primary_oic_id).values():
        primary = group[0].primary_oic
        in_app_to_primary = actor is None or actor.pk != primary.pk
        if len(group) == 1:
            p = parts[group[0].pk]
            _send_email(primary, TEMPLATE_OIC_COPY, p["change_ctx"], actor=actor, delegation_ids=[group[0].pk])
            if in_app_to_primary:
                notify_in_app(
                    [primary],
                    title=f"{p['title']}: {p['ctx']['equipment_name']}",
                    message=p["summary"],
                    link=PAGE_PATH,
                    event="oic_substitute_ended",
                    created_by=actor,
                    extra=event_extra(group),
                )
            continue
        summary = (
            f"{len(group)} OIC substitutions for your equipment ended{by}. You manage the equipment as before; "
            "the substitutes and Lab in-charges have been notified."
        )
        ctx = _ended_bulk_ctx(group, actor, lines=_ended_lines_with_names(group), summary=summary)
        _send_email(primary, TEMPLATE_BULK_OIC_COPY, ctx, actor=actor, delegation_ids=[r.pk for r in group])
        if in_app_to_primary:
            notify_in_app(
                [primary],
                title=f"{ctx['change_title']}: {ctx['equipment_count']} equipment",
                message=summary,
                link=PAGE_PATH,
                event="oic_substitute_ended",
                created_by=actor,
                extra=event_extra(group),
            )


def _ended_lines_with_names(rows) -> list[str]:
    from iic_booking.users.display import get_user_display_name

    return [
        f"• {_equipment_label(r.equipment)}: {get_user_display_name(r.temporary_oic) or 'OIC'}, "
        f"{_short_period(r)} ({_status_label(r)})"
        for r in rows
    ]


# --------------------------------------------------------------------------- grouped (bulk) helpers


def _group_by(rows, key) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return groups


def _equipment_label(equipment) -> str:
    name = (equipment.name or "").strip()
    code = (equipment.code or "").strip()
    if name and code and code != name:
        return f"{name} ({code})"
    return name or code or f"Equipment {equipment.pk}"


def _short_period(delegation) -> str:
    """06-10-2026 to 08-10-2026 (whole IST days; the end day is inclusive)."""
    start = timezone.localtime(delegation.effective_start())
    end = _end_display_dt(delegation.resume_at)
    return f"{start:%d-%m-%Y} to {end:%d-%m-%Y}"


def _status_label(delegation) -> str:
    return STATUS_LABELS.get(delegation.status, delegation.status)


def _common_period(rows) -> str:
    periods = {(r.effective_start(), r.resume_at) for r in rows}
    return format_period(rows[0]) if len(periods) == 1 else ""


def notify_created_many(rows, *, actor=None) -> None:
    """Notify about substitutions created together: each substitute gets one message listing all their
    equipment, each Lab in-charge one message covering all their equipment, and the OIC one summary.
    A recipient concerned by a single equipment gets the usual single-equipment message."""
    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.users.display import get_user_display_name

    rows = list(rows)
    if not rows:
        return
    by_equipment = _group_by(rows, lambda r: r.equipment_id)
    if len(by_equipment) == 1:
        notify_created(rows, actor=actor)
        return

    primary = rows[0].primary_oic
    granted_by = get_user_display_name(rows[0].created_by or primary) or ""
    oic_name = get_user_display_name(primary) or ""
    reason = rows[0].reason or ""
    link = _link()

    def names(group):
        return ", ".join(get_user_display_name(r.temporary_oic) or "OIC" for r in group)

    def equipment_lines(eq_ids):
        return [
            f"• {_equipment_label(by_equipment[e][0].equipment)}: {names(by_equipment[e])}, {_short_period(by_equipment[e][0])}"
            for e in eq_ids
        ]

    def bulk_ctx(group_rows, lines, summary=""):
        return {
            "granted_by_name": granted_by,
            "oic_name": oic_name,
            "equipment_count": len({r.equipment_id for r in group_rows}),
            "equipment_list": "\n".join(lines),
            "period_display": _common_period(group_rows),
            "reason": reason,
            "summary": summary,
            "change_title": "OIC substitute assigned",
            "link": link,
        }

    for group in _group_by(rows, lambda r: r.temporary_oic_id).values():
        sub = group[0].temporary_oic
        ids = [r.pk for r in group]
        if len(group) == 1:
            ctx = _base_context(by_equipment[group[0].equipment_id])
            _send_email(sub, TEMPLATE_ASSIGNED, ctx, actor=actor, delegation_ids=ids)
            notify_in_app(
                [sub],
                title=f"You are an OIC substitute for {ctx['equipment_name']}",
                message=(
                    f"{granted_by} has given you OIC access to {ctx['equipment_name']} from {ctx['period_display']}. "
                    f"Reason: {reason}"
                ),
                link=PAGE_PATH,
                event="oic_substitute_assigned",
                created_by=actor,
                extra={"delegation_ids": ids},
            )
            continue
        lines = [f"• {_equipment_label(r.equipment)}: {_short_period(r)}" for r in group]
        ctx = bulk_ctx(group, lines)
        _send_email(sub, TEMPLATE_BULK_ASSIGNED, ctx, actor=actor, delegation_ids=ids)
        notify_in_app(
            [sub],
            title=f"You are an OIC substitute for {len(group)} equipment",
            message=(
                f"{granted_by} has given you OIC access to: "
                + "; ".join(f"{_equipment_label(r.equipment)} ({_short_period(r)})" for r in group)
                + f". Reason: {reason}"
            ),
            link=PAGE_PATH,
            event="oic_substitute_assigned",
            created_by=actor,
            extra={"delegation_ids": ids},
        )

    staff_equipment: dict[int, tuple] = {}
    for eq_id, group in by_equipment.items():
        exclude = {primary.pk, *(r.temporary_oic_id for r in group)}
        for user in _lab_staff(group[0].equipment, exclude):
            staff_equipment.setdefault(user.pk, (user, []))[1].append(eq_id)
    for user, eq_ids in staff_equipment.values():
        group_rows = [r for e in eq_ids for r in by_equipment[e]]
        ids = [r.pk for r in group_rows]
        if len(eq_ids) == 1:
            group = by_equipment[eq_ids[0]]
            ctx = _base_context(group)
            summary = (
                f"{granted_by} has made {ctx['substitute_names']} OIC substitute for {ctx['equipment_name']} "
                f"from {ctx['period_display']}. During this period they can manage the equipment's bookings and "
                f"settings as OIC, together with {oic_name}."
            )
            change_ctx = {**ctx, "change_title": "OIC substitute assigned", "summary": summary, "status_display": "Assigned"}
            _send_email(user, TEMPLATE_LAB_STAFF, change_ctx, actor=actor, delegation_ids=ids)
            notify_in_app(
                [user],
                title=f"OIC substitute for {ctx['equipment_name']}",
                message=summary,
                event="oic_substitute_assigned",
                created_by=actor,
                extra={"delegation_ids": ids},
            )
            continue
        summary = (
            f"{granted_by} has assigned OIC substitutes for {len(eq_ids)} equipment you look after. During each period "
            f"the substitute can manage the equipment's bookings and settings as OIC, together with {oic_name}."
        )
        ctx = bulk_ctx(group_rows, equipment_lines(eq_ids), summary)
        _send_email(user, TEMPLATE_BULK_LAB_STAFF, ctx, actor=actor, delegation_ids=ids)
        notify_in_app(
            [user],
            title=f"OIC substitutes for {len(eq_ids)} equipment",
            message=summary,
            event="oic_substitute_assigned",
            created_by=actor,
            extra={"delegation_ids": ids},
        )

    summary = (
        f"You have assigned OIC substitutes for {len(by_equipment)} equipment. During each period the substitute can "
        "manage the equipment with you. The substitutes and the Lab in-charges have been notified."
    )
    ctx = bulk_ctx(rows, equipment_lines(list(by_equipment)), summary)
    _send_email(primary, TEMPLATE_BULK_OIC_COPY, ctx, actor=actor, delegation_ids=[r.pk for r in rows])
