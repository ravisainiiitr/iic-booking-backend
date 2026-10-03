"""Department Administrator actions on urgent requests, equipment waitlists and repeat samples.

A Department Administrator may act like the Officer In Charge, but only on equipment whose
internal department is their own and only with the "bookings.manage" grant. Every action on
these pages is written to the staff action audit log with the actor's role.
"""

from __future__ import annotations

import logging

from iic_booking.users.models.user_type import UserType

audit_logger = logging.getLogger("iic_booking.audit.staff_actions")

DEPT_ADMIN_OTHER_DEPARTMENT_MESSAGE = (
    "Department Administrators can act only on equipment in their own department."
)
DEPT_ADMIN_ROLE_LABEL = "Department Administrator"


def is_dept_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.DEPT_ADMIN


def dept_admin_can_manage_bookings(user) -> bool:
    if not is_dept_admin(user) or not getattr(user, "department_id", None):
        return False
    from iic_booking.users.rbac import user_has_permission

    return user_has_permission(user, "bookings.manage", department_id=user.department_id)


def dept_admin_manages_equipment(user, equipment_id) -> bool:
    """True when ``user`` is a Department Administrator with bookings.manage and the equipment is in their department."""
    if equipment_id in (None, "") or not dept_admin_can_manage_bookings(user):
        return False
    from iic_booking.equipment.models import Equipment

    try:
        equipment_pk = int(equipment_id)
    except (TypeError, ValueError):
        return False
    return Equipment.objects.filter(equipment_id=equipment_pk, internal_department_id=user.department_id).exists()


def actor_role_metadata(user) -> dict:
    """Booking event metadata marking an action taken by a Department Administrator."""
    if not is_dept_admin(user):
        return {}
    return {"actor_role": UserType.DEPT_ADMIN, "actor_role_label": DEPT_ADMIN_ROLE_LABEL}


def record_staff_action(actor, action: str, *, equipment_id=None, **details) -> None:
    """Audit log line for an urgent / waitlist / repeat sample action (who, in which role, on what)."""
    audit_logger.info(
        "staff_action action=%s actor_id=%s actor_role=%s department_id=%s equipment_id=%s details=%s",
        action,
        getattr(actor, "pk", None),
        getattr(actor, "user_type", None),
        getattr(actor, "department_id", None),
        equipment_id,
        details,
    )
