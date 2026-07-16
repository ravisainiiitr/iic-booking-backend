"""Internal hierarchical RBAC helpers."""

from __future__ import annotations

from django.db import transaction

from iic_booking.users.models import DepartmentType, UserType
from iic_booking.users.models.rbac import (
    DEFAULT_PERMISSION_DEFINITIONS,
    DeptAdminPermissionGrant,
    PermissionDefinition,
    StaffPermissionGrant,
)


STAFF_ROLE_CODES = {UserType.MANAGER, UserType.OPERATOR, UserType.FINANCE}


def is_internal_department_enabled(department) -> bool:
    if department is None:
        return False
    if getattr(department, "department_type", None) != DepartmentType.INTERNAL:
        return False
    return bool(getattr(department, "access_enabled", True))


def get_user_department_scope_id(user) -> int | None:
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return None
    department = getattr(user, "department", None)
    if not is_internal_department_enabled(department):
        return None
    if getattr(user, "user_type", None) in {UserType.DEPT_ADMIN, *STAFF_ROLE_CODES}:
        return getattr(user, "department_id", None)
    return None


def is_department_admin(user) -> bool:
    return (
        bool(user)
        and getattr(user, "user_type", None) == UserType.DEPT_ADMIN
        and is_internal_department_enabled(getattr(user, "department", None))
    )


def is_department_staff(user) -> bool:
    return (
        bool(user)
        and getattr(user, "user_type", None) in STAFF_ROLE_CODES
        and is_internal_department_enabled(getattr(user, "department", None))
    )


def user_has_permission(user, code: str, department_id: int | None = None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return True

    scope_department_id = get_user_department_scope_id(user)
    if scope_department_id is None:
        return False
    if department_id is not None and int(department_id) != int(scope_department_id):
        return False

    if getattr(user, "user_type", None) == UserType.DEPT_ADMIN:
        return DeptAdminPermissionGrant.objects.filter(
            dept_admin=user,
            department_id=scope_department_id,
            permission__code=code,
        ).exists()

    if getattr(user, "user_type", None) in STAFF_ROLE_CODES:
        return StaffPermissionGrant.objects.filter(
            staff_user=user,
            department_id=scope_department_id,
            permission__code=code,
            dept_admin__user_type=UserType.DEPT_ADMIN,
            dept_admin__department_id=scope_department_id,
            dept_admin__department__access_enabled=True,
            dept_admin__department__department_type=DepartmentType.INTERNAL,
            dept_admin__dept_admin_permission_grants__department_id=scope_department_id,
            dept_admin__dept_admin_permission_grants__permission__code=code,
        ).exists()

    return False


def ensure_default_permission_definitions() -> None:
    for code, name, description in DEFAULT_PERMISSION_DEFINITIONS:
        PermissionDefinition.objects.get_or_create(
            code=code,
            defaults={"name": name, "description": description},
        )


@transaction.atomic
def ensure_default_dept_admin_permission_grants(user, granted_by=None) -> None:
    if not is_department_admin(user):
        return
    ensure_default_permission_definitions()
    if DeptAdminPermissionGrant.objects.filter(
        dept_admin=user,
        department_id=user.department_id,
    ).exists():
        return
    permissions = PermissionDefinition.objects.filter(
        code__in=[row[0] for row in DEFAULT_PERMISSION_DEFINITIONS]
    )
    DeptAdminPermissionGrant.objects.bulk_create(
        [
            DeptAdminPermissionGrant(
                department_id=user.department_id,
                dept_admin=user,
                permission=permission,
                granted_by=granted_by,
            )
            for permission in permissions
        ],
        ignore_conflicts=True,
    )


def scope_queryset_to_department(queryset, user, lookup: str):
    department_id = get_user_department_scope_id(user)
    if department_id is None:
        return queryset if getattr(user, "user_type", None) == UserType.ADMIN else queryset.none()
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return queryset
    return queryset.filter(**{lookup: department_id})
