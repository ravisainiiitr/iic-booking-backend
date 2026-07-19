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

# Legacy defaults when staff have no explicit StaffPermissionGrant rows yet.
ROLE_DEFAULT_PERMISSIONS: dict[str, frozenset[str]] = {
    UserType.MANAGER: frozenset(
        {
            "bookings.manage",
            "equipment.manage",
            "reports.view",
            "oic.assign",
        }
    ),
    UserType.OPERATOR: frozenset(
        {
            "bookings.manage",
            "equipment.manage",
            "reports.view",
            "lab.assign",
        }
    ),
    UserType.FINANCE: frozenset(
        {
            "wallet.manage",
            "reports.view",
            "finance.assign",
        }
    ),
}


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
    if getattr(user, "user_type", None) == UserType.EXTERNAL_RELATIONS:
        return None
    department = getattr(user, "department", None)
    if getattr(user, "user_type", None) == UserType.ORG_ADMIN:
        if department is None or getattr(department, "department_type", None) != DepartmentType.EXTERNAL:
            return None
        return getattr(user, "department_id", None)
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


def is_external_relations_admin(user) -> bool:
    return bool(user) and getattr(user, "user_type", None) == UserType.EXTERNAL_RELATIONS


def is_organization_admin(user) -> bool:
    if not user or getattr(user, "user_type", None) != UserType.ORG_ADMIN:
        return False
    department = getattr(user, "department", None)
    return (
        department is not None
        and getattr(department, "department_type", None) == DepartmentType.EXTERNAL
    )


def is_main_or_external_relations(user) -> bool:
    """Main Admin or External Relations Administrator (org verification)."""
    if not user:
        return False
    ut = getattr(user, "user_type", None)
    return ut in {UserType.ADMIN, UserType.EXTERNAL_RELATIONS}


def _staff_has_any_explicit_grants(user, department_id: int) -> bool:
    return StaffPermissionGrant.objects.filter(
        staff_user=user,
        department_id=department_id,
    ).exists()


def _staff_has_explicit_permission(user, code: str, department_id: int) -> bool:
    return StaffPermissionGrant.objects.filter(
        staff_user=user,
        department_id=department_id,
        permission__code=code,
        dept_admin__user_type=UserType.DEPT_ADMIN,
        dept_admin__department_id=department_id,
        dept_admin__department__access_enabled=True,
        dept_admin__department__department_type=DepartmentType.INTERNAL,
        dept_admin__dept_admin_permission_grants__department_id=department_id,
        dept_admin__dept_admin_permission_grants__permission__code=code,
    ).exists()


def user_has_permission(user, code: str, department_id: int | None = None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return True
    # External Relations: verification + external user ops (not full internal caps)
    if ut == UserType.EXTERNAL_RELATIONS:
        return code in {
            "users.manage",
            "reports.view",
            "external.org.verify",
            "external.users.manage",
        }
    # Org Admin: limited to own organization
    if ut == UserType.ORG_ADMIN:
        if not is_organization_admin(user):
            return False
        if department_id is not None and int(department_id) != int(user.department_id):
            return False
        return code in {
            "org.users.manage",
            "org.profile.manage",
            "bookings.manage",
            "reports.view",
        }

    scope_department_id = get_user_department_scope_id(user)
    if scope_department_id is None:
        return False
    if department_id is not None and int(department_id) != int(scope_department_id):
        return False

    # Admin Panel Access module grants imply legacy permission codes (e.g. bookings.manage).
    from iic_booking.users.admin_settings_registry import modules_grant_permission

    if user_has_admin_panel_access(user) and modules_grant_permission(
        list_effective_admin_module_keys(user), code
    ):
        return True

    if ut == UserType.DEPT_ADMIN:
        return DeptAdminPermissionGrant.objects.filter(
            dept_admin=user,
            department_id=scope_department_id,
            permission__code=code,
        ).exists()

    if ut in STAFF_ROLE_CODES:
        if _staff_has_any_explicit_grants(user, scope_department_id):
            return _staff_has_explicit_permission(user, code, scope_department_id)
        # Legacy fallback: role defaults until Dept Admin configures grants
        return code in ROLE_DEFAULT_PERMISSIONS.get(ut, frozenset())

    return False


def list_effective_permission_codes(user) -> list[str]:
    """Permission codes the current user may exercise (for UI gating)."""
    if not user or not getattr(user, "is_authenticated", False):
        return []
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        ensure_default_permission_definitions()
        return list(
            PermissionDefinition.objects.order_by("code").values_list("code", flat=True)
        ) + ["external.org.verify", "external.users.manage"]

    if ut == UserType.EXTERNAL_RELATIONS:
        return sorted(
            {
                "users.manage",
                "reports.view",
                "external.org.verify",
                "external.users.manage",
            }
        )

    if ut == UserType.ORG_ADMIN and is_organization_admin(user):
        return sorted(
            {
                "org.users.manage",
                "org.profile.manage",
                "bookings.manage",
                "reports.view",
            }
        )

    scope_department_id = get_user_department_scope_id(user)
    if scope_department_id is None:
        return []

    codes: set[str] = set()

    if ut == UserType.DEPT_ADMIN:
        codes.update(
            DeptAdminPermissionGrant.objects.filter(
                dept_admin=user,
                department_id=scope_department_id,
            ).values_list("permission__code", flat=True)
        )
    elif ut in STAFF_ROLE_CODES:
        if _staff_has_any_explicit_grants(user, scope_department_id):
            codes.update(
                StaffPermissionGrant.objects.filter(
                    staff_user=user,
                    department_id=scope_department_id,
                )
                .values_list("permission__code", flat=True)
                .distinct()
            )
        else:
            codes.update(ROLE_DEFAULT_PERMISSIONS.get(ut, frozenset()))

    # Union permissions implied by Admin Panel Access modules.
    if user_has_admin_panel_access(user):
        from iic_booking.users.admin_settings_registry import (
            PERMISSION_CODE_MODULE_KEYS,
            modules_grant_permission,
        )

        modules = list_effective_admin_module_keys(user)
        for code in PERMISSION_CODE_MODULE_KEYS:
            if modules_grant_permission(modules, code):
                codes.add(code)

    return sorted(codes)


def ensure_default_permission_definitions() -> None:
    for code, name, description in DEFAULT_PERMISSION_DEFINITIONS:
        PermissionDefinition.objects.get_or_create(
            code=code,
            defaults={"name": name, "description": description},
        )
    # Extra codes used by external org roles (may not be in DeptAdmin catalog UI)
    for code, name, description in (
        ("external.org.verify", "Verify external organizations", "Approve or reject external organization KYC."),
        ("external.users.manage", "Manage external users", "Approve and manage external organization users."),
        ("org.users.manage", "Manage organization users", "Add or remove users in own external organization."),
        ("org.profile.manage", "Manage organization profile", "Update organization profile and documents."),
    ):
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


def get_admin_panel_role_config(user_type: str, department_id: int | None):
    """Return AdminPanelRoleConfig for (user_type, department) or None."""
    if not user_type or department_id is None:
        return None
    from iic_booking.users.models.admin_panel_access import AdminPanelRoleConfig

    return (
        AdminPanelRoleConfig.objects.filter(
            user_type=user_type,
            department_id=department_id,
        ).first()
    )


def user_has_admin_panel_access(user) -> bool:
    """
    Whether the user may see/use the Admin Panel at all.
    Main Admin (and Django staff) always yes. Everyone else requires an
    explicit AdminPanelRoleConfig with admin_panel_enabled=True for their
    user_type + department (default: disabled).
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return True
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return True
    if ut == UserType.EXTERNAL_RELATIONS:
        return True
    department_id = getattr(user, "department_id", None)
    if department_id is None:
        return False
    if ut in {UserType.DEPT_ADMIN, *STAFF_ROLE_CODES}:
        if not is_internal_department_enabled(getattr(user, "department", None)):
            return False
    config = get_admin_panel_role_config(ut, department_id)
    return bool(config and config.admin_panel_enabled)


def list_effective_admin_module_keys(user) -> list[str]:
    """Expanded Admin Settings module keys the user may access."""
    from iic_booking.users.admin_settings_registry import (
        all_module_keys,
        expand_module_keys,
        flatten_admin_settings_modules,
        get_admin_settings_module_tree,
    )

    if not user or not getattr(user, "is_authenticated", False):
        return []
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN or getattr(user, "is_staff", False):
        return sorted(all_module_keys())
    if ut == UserType.EXTERNAL_RELATIONS:
        return [
            "user_management",
            "user_management.users",
        ]
    if not user_has_admin_panel_access(user):
        return []
    config = get_admin_panel_role_config(ut, getattr(user, "department_id", None))
    if not config:
        return []
    tree = get_admin_settings_module_tree(include_main_admin_only=False)
    allowed_non_main: set[str] = set()

    def collect(nodes):
        for n in nodes:
            allowed_non_main.add(n["key"])
            collect(n.get("children") or [])

    collect(tree)
    main_only = {r["key"] for r in flatten_admin_settings_modules() if r.get("main_admin_only")}
    raw = [k for k in (config.module_keys or []) if k in allowed_non_main and k not in main_only]
    return sorted(expand_module_keys(raw))


def user_can_access_admin_module(user, module_key: str) -> bool:
    if not module_key:
        return False
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "user_type", None) == UserType.ADMIN or getattr(user, "is_staff", False):
        return True
    if not user_has_admin_panel_access(user):
        return False
    keys = set(list_effective_admin_module_keys(user))
    if module_key in keys:
        return True
    # Parent grant (parent key present) covers descendants.
    if any(module_key.startswith(k + ".") for k in keys):
        return True
    # Hub pages: allow an ancestor key when any granted leaf sits under it.
    if any(k.startswith(module_key + ".") for k in keys):
        return True
    return False


def scope_queryset_to_department(queryset, user, lookup: str):
    """
    Restrict queryset to the user's department for anyone who is not Main Admin.
    Main Admin (and Django staff acting as institute admin) see all rows.
    """
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return queryset
    # External Relations: institute-wide verification tools (no internal dept scope).
    if ut == UserType.EXTERNAL_RELATIONS:
        return queryset
    department_id = get_user_department_scope_id(user)
    if department_id is None:
        return queryset.none()
    return queryset.filter(**{lookup: department_id})


def apply_equipment_department_scope(queryset, user):
    """Scope equipment (or related) rows by Equipment.internal_department_id."""
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return queryset
    if ut == UserType.EXTERNAL_RELATIONS:
        return queryset
    department_id = get_user_department_scope_id(user)
    if department_id is None:
        return queryset.none()
    return queryset.filter(internal_department_id=department_id)
