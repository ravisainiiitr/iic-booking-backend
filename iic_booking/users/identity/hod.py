"""HoD assignment (local portal role). One active HoD per internal department."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from iic_booking.users.models.channel_i_identity import HeadOfDepartmentAssignment
from iic_booking.users.models.user_type import UserType


class HodError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


@transaction.atomic
def assign_hod(*, department, user, actor, effective_from=None, effective_to=None) -> HeadOfDepartmentAssignment:
    if getattr(actor, "user_type", None) != UserType.ADMIN:
        raise HodError("FORBIDDEN", "Only Main Administrator can assign Heads of Department.", status=403)
    effective_from = effective_from or timezone.localdate()
    department = type(department).objects.select_for_update().get(pk=department.pk)
    existing = HeadOfDepartmentAssignment.objects.select_for_update().filter(
        department=department, active=True
    ).first()
    if existing:
        existing.active = False
        existing.effective_to = effective_from
        existing.save(update_fields=["active", "effective_to", "updated_at"])
    assignment = HeadOfDepartmentAssignment.objects.create(
        user=user,
        department=department,
        active=True,
        effective_from=effective_from,
        effective_to=effective_to,
        created_by=actor,
    )
    # Keep legacy Department.head in sync for existing email/info paths; assignment is authoritative.
    if getattr(department, "head_id", None) != user.id:
        department.head = user
        department.save(update_fields=["head", "updated_at"])
    return assignment


@transaction.atomic
def disable_hod(*, assignment: HeadOfDepartmentAssignment, actor) -> HeadOfDepartmentAssignment:
    if getattr(actor, "user_type", None) != UserType.ADMIN:
        raise HodError("FORBIDDEN", "Only Main Administrator can disable Heads of Department.", status=403)
    assignment = HeadOfDepartmentAssignment.objects.select_for_update().get(pk=assignment.pk)
    assignment.active = False
    assignment.effective_to = timezone.localdate()
    assignment.save(update_fields=["active", "effective_to", "updated_at"])
    dept = assignment.department
    if getattr(dept, "head_id", None) == assignment.user_id:
        dept.head = None
        dept.save(update_fields=["head", "updated_at"])
    return assignment
