"""Department-based access rules for DailySlot booking eligibility.

When the equipment has an internal (home) department and at least one upcoming
slot is marked home_department_only=True ("reserved for non-home"):

- Marked slots → reserved for non-home-department users only
- Unmarked slots → home department only
- Marked slots that remain unbooked until ``equipment.reschedule_hours_threshold``
  hours before start → available to all departments

When no upcoming marked slots exist (or equipment has no internal department),
department marking is inactive and all internal users may book unmarked slots.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from django.db.models import Q
from django.utils import timezone


def user_is_home_department(user: Any, equipment: Any) -> bool:
    """True when booker’s department matches equipment’s internal department."""
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return True
    return getattr(user, "department_id", None) == home_id


def equipment_department_slot_policy_active(equipment: Any) -> bool:
    """
    True when home/non-home slot split should be enforced.

    Active only when the equipment has an internal department and at least one
    upcoming (today or later) slot is marked reserved-for-non-home.
    """
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return False
    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    if eid is None:
        return False
    from .models import DailySlot

    return DailySlot.objects.filter(
        slot_master__equipment_id=eid,
        home_department_only=True,
        date__gte=timezone.localdate(),
    ).exists()


def non_home_reservation_release_cutoff(slot: Any, equipment: Any):
    """
    Datetime when a marked (non-home reserved) unbooked slot opens to all departments.
    None if the slot is not marked or has no start time.
    """
    if not getattr(slot, "home_department_only", False):
        return None
    start = getattr(slot, "start_datetime", None)
    if not start:
        return None
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    threshold = int(getattr(equipment, "reschedule_hours_threshold", None) or 48)
    return start - timedelta(hours=threshold)


def non_home_reservation_released_to_all(
    slot: Any, equipment: Any, *, now=None
) -> bool:
    """True when a marked reserved slot is within the threshold window (open to all)."""
    cutoff = non_home_reservation_release_cutoff(slot, equipment)
    if cutoff is None:
        return False
    now = now or timezone.now()
    return now >= cutoff


def slot_department_booking_rule(
    slot: Any, equipment: Any, *, now=None, policy_active: Optional[bool] = None
) -> str:
    """
    Effective rule for an AVAILABLE internal-user booking decision.

    Returns one of: ``open``, ``home_only``, ``non_home``, ``open_all``.
    ``open_all`` = was non-home reserved but released by threshold.
    """
    if policy_active is None:
        policy_active = equipment_department_slot_policy_active(equipment)
    if not policy_active:
        return "open"
    if getattr(slot, "home_department_only", False):
        if non_home_reservation_released_to_all(slot, equipment, now=now):
            return "open_all"
        return "non_home"
    return "home_only"


def slot_allows_internal_user(slot: Any, user: Any, equipment: Any) -> bool:
    """
    True if an internal user may book this slot under department reservation rules.
    """
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return True
    if not equipment_department_slot_policy_active(equipment):
        return True

    is_home = user_is_home_department(user, equipment)
    rule = slot_department_booking_rule(slot, equipment, policy_active=True)
    if rule in ("open", "open_all"):
        return True
    if rule == "home_only":
        return is_home
    if rule == "non_home":
        return not is_home
    return True


def filter_queryset_for_home_department(
    qs,
    *,
    user: Any,
    equipment: Any,
    is_admin: bool = False,
    is_external: bool = False,
):
    """
    Restrict a DailySlot queryset for non-admin internal users.

    External users and admins are unchanged (external uses reserved_for_external).
    """
    if is_admin or is_external:
        return qs
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return qs
    if not equipment_department_slot_policy_active(equipment):
        return qs

    is_home = user_is_home_department(user, equipment)
    threshold = int(getattr(equipment, "reschedule_hours_threshold", None) or 48)
    release_boundary = timezone.now() + timedelta(hours=threshold)
    # Marked + start within threshold window → open to everyone.
    released_q = Q(home_department_only=True, start_datetime__lte=release_boundary)

    if is_home:
        # Unmarked (home-only) or released reserved slots.
        return qs.filter(Q(home_department_only=False) | released_q)
    # Non-home: only reserved (marked) slots — before and after release.
    return qs.filter(home_department_only=True)


def department_access_denial_message(slot: Any, user: Any, equipment: Any) -> str:
    """Human-readable reason when slot_allows_internal_user is False."""
    rule = slot_department_booking_rule(slot, equipment)
    is_home = user_is_home_department(user, equipment)
    if rule == "home_only" and not is_home:
        return (
            f"Slot {getattr(slot, 'id', '')} is reserved for this equipment's home department only."
        )
    if rule == "non_home" and is_home:
        threshold = int(getattr(equipment, "reschedule_hours_threshold", None) or 48)
        return (
            f"Slot {getattr(slot, 'id', '')} is reserved for other (non-home) departments. "
            f"If still unbooked, it opens to all departments {threshold} hours before the slot starts."
        )
    return f"Slot {getattr(slot, 'id', '')} is not available for your department."
