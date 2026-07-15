"""Home-department-only access rules for DailySlot booking eligibility."""

from __future__ import annotations

from typing import Any, Optional


def slot_allows_internal_user(slot: Any, user: Any, equipment: Any) -> bool:
    """
    True if an internal user may book this AVAILABLE slot under home-department rules.

    - home_department_only=False → open to all departments
    - equipment has no internal_department → open (cannot enforce)
    - else booker.department_id must equal equipment.internal_department_id
    """
    if not getattr(slot, "home_department_only", False):
        return True
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return True
    return getattr(user, "department_id", None) == home_id


def user_is_home_department(user: Any, equipment: Any) -> bool:
    """True when booker’s department matches equipment’s internal department."""
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return True
    return getattr(user, "department_id", None) == home_id


def filter_queryset_for_home_department(
    qs,
    *,
    user: Any,
    equipment: Any,
    is_admin: bool = False,
    is_external: bool = False,
):
    """
    Restrict a DailySlot queryset for non-admin internal users from other departments:
    exclude home_department_only=True slots.
    External users and admins are unchanged (external uses reserved_for_external separately).
    """
    if is_admin or is_external:
        return qs
    if user_is_home_department(user, equipment):
        return qs
    home_id = getattr(equipment, "internal_department_id", None)
    if not home_id:
        return qs
    return qs.filter(home_department_only=False)
