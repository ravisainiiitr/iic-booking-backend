"""
Multi-mode family management shared by the OIC and Main Administrator page.

A family is a base instrument plus the equipment linked to it as modes (``parent_equipment``).
Invariant kept here: a base is flagged ``enable_multi_mode`` exactly when it has at least one
mode, and a mode is never flagged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import is_department_admin, user_has_permission

from .models import (
    Booking,
    BookingStatus,
    Equipment,
    EquipmentModeAuditLog,
    EquipmentModeSchedule,
    ModeAvailability,
)
from .reports import get_equipment_ids_managed_by_oic

SCOPE_ADMIN = "admin"
SCOPE_OIC = "oic"
SCOPE_DEPT_ADMIN = "dept_admin"

_FUTURE_BOOKING_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.WAITLISTED,
    BookingStatus.BOOKED,
    BookingStatus.HOLD,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
)


class FamilyChangeError(Exception):
    def __init__(self, message: str, *, status_code: int = 400, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def access_scope(user) -> Optional[str]:
    if not user or not getattr(user, "is_authenticated", False):
        return None
    user_type = getattr(user, "user_type", None)
    if user_type == UserType.ADMIN:
        return SCOPE_ADMIN
    if user_type == UserType.MANAGER:
        return SCOPE_OIC
    if is_department_admin(user):
        dept_id = getattr(user, "department_id", None)
        if user_has_permission(user, "admin_settings.equipment", department_id=dept_id) or user_has_permission(
            user, "equipment.manage", department_id=dept_id
        ):
            return SCOPE_DEPT_ADMIN
    return None


def manageable_equipment_qs(user) -> QuerySet:
    scope = access_scope(user)
    if scope == SCOPE_ADMIN:
        return Equipment.objects.all()
    if scope == SCOPE_OIC:
        return Equipment.objects.filter(equipment_id__in=get_equipment_ids_managed_by_oic(user.id))
    if scope == SCOPE_DEPT_ADMIN:
        return Equipment.objects.filter(internal_department_id=user.department_id)
    return Equipment.objects.none()


def can_manage_equipment(user, equipment_id: int) -> bool:
    return manageable_equipment_qs(user).filter(equipment_id=equipment_id).exists()


def log_mode_change(equipment: Equipment, action: str, details: dict, actor=None) -> None:
    EquipmentModeAuditLog.objects.create(
        equipment=equipment,
        equipment_code=equipment.code or "",
        action=action,
        details=details,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
    )


def mode_candidates(base: Equipment, user) -> list[Equipment]:
    """
    Equipment that may be ticked as a mode of ``base``: same department, not a mode of another
    family, not itself a base with modes, and (for OIC / department admin) managed by the user.
    Current modes of ``base`` are included.
    """
    qs = (
        manageable_equipment_qs(user)
        .filter(internal_department_id=base.internal_department_id)
        .filter(Q(parent_equipment__isnull=True) | Q(parent_equipment_id=base.equipment_id))
        .exclude(equipment_id=base.equipment_id)
        .exclude(mode_children__isnull=False)
        .distinct()
        .order_by("code", "name")
    )
    return list(qs)


def future_usage_for_mode(mode: Equipment) -> dict[str, list[dict[str, Any]]]:
    """Upcoming bookings and current/future schedules that make unlinking a mode unsafe."""
    now = timezone.now()
    today = timezone.localdate()
    bookings = (
        Booking.objects.filter(
            equipment_id=mode.equipment_id,
            status__in=_FUTURE_BOOKING_STATUSES,
            daily_slots__end_datetime__gt=now,
        )
        .distinct()
        .order_by("booking_id")
    )
    schedules = EquipmentModeSchedule.objects.filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today), mode_equipment_id=mode.equipment_id
    ).order_by("start_date", "id")
    return {
        "bookings": [
            {"booking_id": b.booking_id, "reference": b.virtual_booking_id or str(b.booking_id), "status": b.status}
            for b in bookings[:50]
        ],
        "schedules": [
            {
                "id": s.id,
                "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
            }
            for s in schedules[:50]
        ],
    }


def _schedule_range_text(item: dict) -> str:
    if not item.get("start_date") and not item.get("end_date"):
        return "always (no dates)"
    return f"{item['start_date']} to {item['end_date']}"


def sync_family_flags(equipment_ids: Iterable[int], actor=None, *, reason: str = "") -> None:
    """Enforce the flag invariant for the given equipment (bases and modes) and log any change."""
    ids = {int(i) for i in equipment_ids if i}
    if not ids:
        return
    for eq in Equipment.objects.filter(equipment_id__in=ids):
        if eq.parent_equipment_id:
            wanted = False
        else:
            wanted = Equipment.objects.filter(parent_equipment_id=eq.equipment_id).exists()
        if eq.enable_multi_mode != wanted:
            Equipment.objects.filter(pk=eq.pk).update(enable_multi_mode=wanted, updated_at=timezone.now())
            log_mode_change(
                eq,
                "FLAG_SYNC",
                {"before": {"enable_multi_mode": eq.enable_multi_mode}, "after": {"enable_multi_mode": wanted},
                 "reason": reason},
                actor,
            )


@dataclass
class FamilyChangeResult:
    added: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    availability_changed: list[int] = field(default_factory=list)


def _parse_modes(raw) -> dict[int, Optional[str]]:
    if not isinstance(raw, list):
        raise FamilyChangeError("modes must be a list.")
    wanted: dict[int, Optional[str]] = {}
    valid = {c for c, _ in ModeAvailability.choices}
    for item in raw:
        if isinstance(item, dict):
            eq_id = item.get("equipment_id")
            availability = item.get("mode_availability")
        else:
            eq_id, availability = item, None
        try:
            eq_id = int(eq_id)
        except (TypeError, ValueError):
            raise FamilyChangeError("Each mode needs a numeric equipment_id.")
        if availability is not None:
            availability = str(availability).strip().upper()
            if availability not in valid:
                raise FamilyChangeError(
                    "mode_availability must be ALWAYS or SCHEDULED_ONLY."
                )
        wanted[eq_id] = availability
    return wanted


@transaction.atomic
def set_family_modes(base: Equipment, raw_modes, user) -> FamilyChangeResult:
    """Replace the modes of ``base`` with ``raw_modes`` ([{equipment_id, mode_availability?}] or ids)."""
    if not can_manage_equipment(user, base.equipment_id):
        raise FamilyChangeError("You can only set up modes for equipment you manage.", status_code=403)
    base = Equipment.objects.select_for_update().get(pk=base.pk)
    if base.parent_equipment_id:
        raise FamilyChangeError(
            f"{base.code} is itself a mode of another instrument and cannot be a base."
        )

    wanted = _parse_modes(raw_modes)
    wanted.pop(base.equipment_id, None)
    current = {e.equipment_id: e for e in Equipment.objects.filter(parent_equipment_id=base.equipment_id)}
    to_add = [eid for eid in wanted if eid not in current]
    to_remove = [eid for eid in current if eid not in wanted]

    allowed_ids = {e.equipment_id for e in mode_candidates(base, user)}
    added_objs = {e.equipment_id: e for e in Equipment.objects.filter(equipment_id__in=to_add)}
    for eid in to_add:
        eq = added_objs.get(eid)
        if eq is None:
            raise FamilyChangeError(f"Equipment {eid} not found.", status_code=404)
        if eid not in allowed_ids:
            if not can_manage_equipment(user, eid):
                raise FamilyChangeError(f"You do not manage {eq.code}.", status_code=403)
            if eq.parent_equipment_id:
                raise FamilyChangeError(f"{eq.code} is already a mode of another instrument.")
            if Equipment.objects.filter(parent_equipment_id=eid).exists():
                raise FamilyChangeError(f"{eq.code} is a base with its own modes and cannot be a mode.")
            if eq.internal_department_id != base.internal_department_id:
                raise FamilyChangeError(f"{eq.code} belongs to a different department than {base.code}.")
            raise FamilyChangeError(f"{eq.code} cannot be added as a mode of {base.code}.")

    blocked: list[dict[str, Any]] = []
    for eid in to_remove:
        eq = current[eid]
        if not can_manage_equipment(user, eid):
            raise FamilyChangeError(f"You do not manage {eq.code}.", status_code=403)
        usage = future_usage_for_mode(eq)
        if usage["bookings"] or usage["schedules"]:
            blocked.append({"equipment_id": eid, "code": eq.code, "name": eq.name, **usage})
    if blocked:
        parts = []
        for b in blocked:
            bits = []
            if b["bookings"]:
                bits.append(f"{len(b['bookings'])} upcoming booking(s): " + ", ".join(x["reference"] for x in b["bookings"][:10]))
            if b["schedules"]:
                bits.append(
                    f"{len(b['schedules'])} current or future schedule(s): "
                    + ", ".join(_schedule_range_text(x) for x in b["schedules"][:10])
                )
            parts.append(f"{b['code']} has " + " and ".join(bits))
        raise FamilyChangeError(
            "Cannot remove a mode that is still in use. " + "; ".join(parts)
            + ". Delete its schedules and finish or move its bookings first.",
            status_code=409,
            details={"blocked_modes": blocked},
        )

    result = FamilyChangeResult()
    now = timezone.now()
    for eid in to_add:
        eq = added_objs[eid]
        availability = wanted[eid] or ModeAvailability.ALWAYS
        Equipment.objects.filter(pk=eid).update(
            parent_equipment_id=base.equipment_id,
            enable_multi_mode=False,
            mode_availability=availability,
            updated_at=now,
        )
        log_mode_change(
            eq,
            "MODE_LINKED",
            {"base_equipment_id": base.equipment_id, "base_code": base.code,
             "before": {"enable_multi_mode": eq.enable_multi_mode, "mode_availability": eq.mode_availability},
             "after": {"enable_multi_mode": False, "mode_availability": availability}},
            user,
        )
        result.added.append(eid)
    for eid in to_remove:
        eq = current[eid]
        Equipment.objects.filter(pk=eid).update(parent_equipment=None, enable_multi_mode=False, updated_at=now)
        log_mode_change(
            eq,
            "MODE_UNLINKED",
            {"base_equipment_id": base.equipment_id, "base_code": base.code,
             "before": {"enable_multi_mode": eq.enable_multi_mode, "mode_availability": eq.mode_availability}},
            user,
        )
        result.removed.append(eid)
    for eid, availability in wanted.items():
        if eid in current and availability and current[eid].mode_availability != availability:
            Equipment.objects.filter(pk=eid).update(mode_availability=availability, updated_at=now)
            log_mode_change(
                current[eid],
                "MODE_AVAILABILITY",
                {"before": current[eid].mode_availability, "after": availability},
                user,
            )
            result.availability_changed.append(eid)

    sync_family_flags([base.equipment_id, *to_add, *to_remove], user, reason="family update")
    return result


# Mode membership follows the schedules: adding a schedule for eligible equipment makes it a mode of the
# base, and a mode with no schedule left is unlinked (unless it still has upcoming bookings). A linked mode
# is bookable only while one of its schedules is active; a schedule with blank dates means always.


def link_mode_for_schedule(base: Equipment, mode: Equipment, user) -> bool:
    """Make ``mode`` a mode of ``base`` (scheduled-only). Returns True when a link was created."""
    if mode.parent_equipment_id == base.equipment_id:
        return False
    if base.parent_equipment_id:
        raise FamilyChangeError(f"{base.code} is itself a mode of another instrument and cannot be a base.")
    if mode.equipment_id not in {e.equipment_id for e in mode_candidates(base, user)}:
        if mode.parent_equipment_id:
            raise FamilyChangeError(f"{mode.code} is already a mode of another instrument.")
        if Equipment.objects.filter(parent_equipment_id=mode.equipment_id).exists():
            raise FamilyChangeError(f"{mode.code} is a base with its own modes and cannot be a mode.")
        if mode.internal_department_id != base.internal_department_id:
            raise FamilyChangeError(f"{mode.code} belongs to a different department than {base.code}.")
        raise FamilyChangeError(f"{mode.code} cannot be a mode of {base.code}.")
    before = {"enable_multi_mode": mode.enable_multi_mode, "mode_availability": mode.mode_availability}
    Equipment.objects.filter(pk=mode.pk).update(
        parent_equipment_id=base.equipment_id,
        enable_multi_mode=False,
        mode_availability=ModeAvailability.SCHEDULED_ONLY,
        updated_at=timezone.now(),
    )
    mode.parent_equipment_id = base.equipment_id
    mode.enable_multi_mode = False
    mode.mode_availability = ModeAvailability.SCHEDULED_ONLY
    log_mode_change(
        mode,
        "MODE_LINKED",
        {"base_equipment_id": base.equipment_id, "base_code": base.code, "via": "schedule",
         "before": before, "after": {"enable_multi_mode": False, "mode_availability": ModeAvailability.SCHEDULED_ONLY}},
        user,
    )
    sync_family_flags([base.equipment_id, mode.equipment_id], user, reason="mode linked by schedule")
    return True


def _unlink(base_id: int, mode: Equipment, user, reason: str) -> None:
    Equipment.objects.filter(pk=mode.pk).update(parent_equipment=None, enable_multi_mode=False, updated_at=timezone.now())
    log_mode_change(
        mode,
        "MODE_UNLINKED",
        {"base_equipment_id": base_id, "reason": reason,
         "before": {"enable_multi_mode": mode.enable_multi_mode, "mode_availability": mode.mode_availability}},
        user,
    )
    sync_family_flags([base_id, mode.equipment_id], user, reason=reason)


def unlink_mode_if_unscheduled(base_id: int, mode_id: int, user) -> bool:
    """After a schedule is removed: unlink the mode when no current/future schedule and no upcoming booking is left."""
    mode = Equipment.objects.filter(pk=mode_id, parent_equipment_id=base_id).first()
    if mode is None:
        return False
    usage = future_usage_for_mode(mode)
    if usage["schedules"] or usage["bookings"]:
        return False
    _unlink(base_id, mode, user, "last schedule removed")
    return True


@transaction.atomic
def remove_mode(base: Equipment, mode: Equipment, user) -> int:
    """
    Unlink ``mode`` from ``base`` and delete its current and future schedules (past ones stay as history).
    Refused while the mode has upcoming bookings. Returns the number of schedules deleted.
    """
    if not can_manage_equipment(user, base.equipment_id) or not can_manage_equipment(user, mode.equipment_id):
        raise FamilyChangeError("You can only change modes of equipment you manage.", status_code=403)
    if mode.parent_equipment_id != base.equipment_id:
        raise FamilyChangeError(f"{mode.code} is not a mode of {base.code}.", status_code=404)
    bookings = future_usage_for_mode(mode)["bookings"]
    if bookings:
        refs = ", ".join(b["reference"] for b in bookings[:10])
        raise FamilyChangeError(
            f"{mode.code} has {len(bookings)} upcoming booking(s): {refs}. Finish or move them before removing the mode.",
            status_code=409,
            details={"bookings": bookings},
        )
    today = timezone.localdate()
    deleted, _ = EquipmentModeSchedule.objects.filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today),
        parent_equipment_id=base.equipment_id,
        mode_equipment_id=mode.equipment_id,
    ).delete()
    _unlink(base.equipment_id, mode, user, "mode removed")
    return deleted
