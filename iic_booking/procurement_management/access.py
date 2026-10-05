"""Feature flag, role resolution and object-level scoping for Procurement & Assets.

Roles are resolved per department:

* Main Administrator  — ``UserType.ADMIN`` (or superuser); every department where the module is enabled.
* Officer in Charge   — ``EquipmentManager`` / active ``EquipmentTemporaryOIC`` for an equipment; department =
                        ``Equipment.internal_department``.
* Lab Operator        — ``EquipmentOperator`` for an equipment.
* HOD                 — active ``HeadOfDepartmentAssignment`` (falls back to ``Department.head``) or an explicit
                        HOD / Competent Authority ``ProcurementRoleAssignment``.
* OC Stores / Office / Auditor — ``ProcurementRoleAssignment`` rows (Office carries granular permissions).

Everything is filtered to departments whose ``ProcurementManagementConfiguration.module_enabled`` is true, so a
disabled department behaves as if the module did not exist. While a department is in pilot mode it only counts as
enabled for its pilot users, and a user on no pilot list is refused everywhere (see ``pilot_blocked``). All checks run server-side; the frontend only
receives the result for showing or hiding UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from django.db.models import Q

from iic_booking.users.models.user_type import UserType

from . import constants as c
from .errors import ProcurementError, forbidden, not_found
from .models import ProcurementManagementConfiguration, ProcurementRoleAssignment

R = c.ModuleRole
P = c.OfficePermission
DEPT_WIDE_ROLES = frozenset({R.OC_STORES, R.OFFICE, R.HOD, R.AUDITOR, R.MAIN_ADMIN})


def is_main_admin(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)
    )


def get_config(department_or_id) -> ProcurementManagementConfiguration | None:
    dept_id = getattr(department_or_id, "pk", department_or_id)
    if not dept_id:
        return None
    return ProcurementManagementConfiguration.objects.filter(department_id=dept_id).first()


def enabled_department_ids(user=None) -> set[int]:
    """Departments with the module on. With ``user``: only those where pilot mode is off or the user is a pilot user."""
    qs = ProcurementManagementConfiguration.objects.filter(module_enabled=True)
    if user is not None:
        uid = getattr(user, "pk", None)
        qs = qs.filter(Q(pilot_mode=False) | Q(pilot_users__pk=uid) if uid else Q(pilot_mode=False))
    return set(qs.values_list("department_id", flat=True))


def pilot_blocked(user) -> bool:
    """True while the module is in pilot everywhere it is on and ``user`` is on no pilot list.

    Such users (Main Administrators included) get exactly the "module disabled" answer from every endpoint. Once a
    department leaves pilot mode, the module is generally available and normal role scoping applies."""
    uid = getattr(user, "pk", None)
    if not uid:
        return True
    configs = ProcurementManagementConfiguration.objects
    if configs.filter(module_enabled=True, pilot_mode=False).exists():
        return False
    return not configs.filter(pilot_mode=True, pilot_users__pk=uid).exists()


def pilot_audience(department_id, users) -> list:
    """Drop users who cannot see the module in this department (used before notifying anyone)."""
    cfg = get_config(department_id)
    users = [u for u in users if u is not None]
    if cfg is None or not cfg.module_enabled:
        return []
    if not cfg.pilot_mode:
        return users
    allowed = set(cfg.pilot_users.values_list("pk", flat=True))
    return [u for u in users if u.pk in allowed]


def module_enabled(department_or_id) -> bool:
    cfg = get_config(department_or_id)
    return bool(cfg and cfg.module_enabled)


def require_config(department) -> ProcurementManagementConfiguration:
    cfg = get_config(department)
    if not cfg or not cfg.module_enabled:
        raise ProcurementError(
            "Procurement & Assets is not enabled for this department.", status=403, code=c.DISABLED_CODE
        )
    return cfg


@dataclass
class UserScope:
    """Everything needed to authorise one user, computed once per request."""

    user: object
    enabled: set[int] = field(default_factory=set)
    blocked: bool = False

    @cached_property
    def admin(self) -> bool:
        return not self.blocked and is_main_admin(self.user)

    @cached_property
    def oic_equipment(self) -> dict[int, int | None]:
        from iic_booking.equipment.models import Equipment
        from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

        ids = get_equipment_ids_managed_by_oic(self.user.id)
        return dict(Equipment.objects.filter(equipment_id__in=ids).values_list("equipment_id", "internal_department_id"))

    @cached_property
    def operator_equipment(self) -> dict[int, int | None]:
        from iic_booking.equipment.models import Equipment, EquipmentOperator

        ids = EquipmentOperator.objects.filter(operator=self.user).values_list("equipment_id", flat=True)
        return dict(Equipment.objects.filter(equipment_id__in=ids).values_list("equipment_id", "internal_department_id"))

    @cached_property
    def assignments(self) -> dict[int, dict[str, set[str]]]:
        out: dict[int, dict[str, set[str]]] = {}
        for row in ProcurementRoleAssignment.objects.filter(user=self.user, active=True):
            out.setdefault(row.department_id, {})[row.role] = set(row.permissions or [])
        return out

    @cached_property
    def hod_departments(self) -> set[int]:
        from iic_booking.users.models import Department
        from iic_booking.users.models.channel_i_identity import HeadOfDepartmentAssignment

        ids = set(
            HeadOfDepartmentAssignment.objects.filter(user=self.user, active=True).values_list("department_id", flat=True)
        )
        ids |= set(Department.objects.filter(head=self.user).values_list("id", flat=True))
        return ids

    @cached_property
    def roles_by_department(self) -> dict[int, set[str]]:
        roles: dict[int, set[str]] = {}

        def add(dept_id, role):
            if dept_id and dept_id in self.enabled:
                roles.setdefault(dept_id, set()).add(role)

        if self.admin:
            for dept_id in self.enabled:
                add(dept_id, R.MAIN_ADMIN)
        for dept_id in self.oic_equipment.values():
            add(dept_id, R.OIC)
        for dept_id in self.operator_equipment.values():
            add(dept_id, R.LAB_OPERATOR)
        for dept_id in self.hod_departments:
            add(dept_id, R.HOD)
        for dept_id, by_role in self.assignments.items():
            for role in by_role:
                add(dept_id, role)
        return roles

    # -- queries ---------------------------------------------------------
    def department_ids(self) -> set[int]:
        return set(self.roles_by_department)

    def roles(self, dept_id) -> set[str]:
        return self.roles_by_department.get(dept_id, set())

    def has_role(self, dept_id, *roles) -> bool:
        return bool(self.roles(dept_id) & set(roles))

    def dept_wide(self, dept_id) -> bool:
        return bool(self.roles(dept_id) & DEPT_WIDE_ROLES)

    def permissions(self, dept_id) -> set[str]:
        roles = self.roles(dept_id)
        if R.MAIN_ADMIN in roles:
            return set(c.ALL_OFFICE_PERMISSIONS)
        perms: set[str] = set()
        by_role = self.assignments.get(dept_id, {})
        if R.OFFICE in roles:
            perms |= by_role.get(R.OFFICE, set())
        if R.OC_STORES in roles:
            perms |= set(c.STORES_PERMISSIONS) | by_role.get(R.OC_STORES, set())
        if roles & {R.HOD, R.AUDITOR}:
            perms.add(P.REPORTS)
        return perms & set(c.ALL_OFFICE_PERMISSIONS)

    def has_perm(self, dept_id, perm: str) -> bool:
        return perm in self.permissions(dept_id)

    def is_oic_for(self, equipment_id) -> bool:
        return bool(equipment_id) and equipment_id in self.oic_equipment

    def is_operator_for(self, equipment_id) -> bool:
        return bool(equipment_id) and equipment_id in self.operator_equipment

    def require_any_department(self) -> None:
        if not self.department_ids():
            raise ProcurementError(
                "Procurement & Assets is not enabled for your account.", status=403, code=c.DISABLED_CODE
            )

    def require_perm(self, dept_id, perm: str) -> None:
        if not self.has_perm(dept_id, perm):
            raise forbidden()

    def require_dept_wide(self, dept_id) -> None:
        if not self.dept_wide(dept_id):
            raise forbidden()


def scope_for(user) -> UserScope:
    if pilot_blocked(user):
        return UserScope(user=user, enabled=set(), blocked=True)
    return UserScope(user=user, enabled=enabled_department_ids(user))


# ---------------------------------------------------------------------------
# Object visibility (IDOR protection: invisible objects are reported as 404)
# ---------------------------------------------------------------------------
def visible_requests_q(scope: UserScope) -> Q:
    wide = [d for d in scope.department_ids() if scope.dept_wide(d)]
    q = Q(pk__in=[])
    if wide:
        q |= Q(department_id__in=wide)
    q |= Q(requested_by=scope.user, department_id__in=scope.department_ids())
    if scope.oic_equipment:
        q |= Q(equipment_id__in=list(scope.oic_equipment), department_id__in=scope.department_ids())
    return q


def visible_department_wide_q(scope: UserScope, *, extra: Q | None = None) -> Q:
    """For records only department-wide roles (Stores/Office/HOD/Auditor/Admin) see in full.

    ``extra`` widens visibility for lab staff (e.g. assets of equipment they operate or manage)."""
    wide = [d for d in scope.department_ids() if scope.dept_wide(d)]
    q = Q(department_id__in=wide) if wide else Q(pk__in=[])
    if extra is not None:
        q |= extra & Q(department_id__in=scope.department_ids())
    return q


def lab_staff_equipment_q(scope: UserScope, field_name: str = "equipment_id") -> Q | None:
    ids = list(set(scope.oic_equipment) | set(scope.operator_equipment))
    if not ids:
        return None
    return Q(**{f"{field_name}__in": ids})


def get_visible(queryset, scope: UserScope, pk, q: Q):
    obj = queryset.filter(q).filter(pk=pk).first()
    if obj is None:
        raise not_found()
    return obj


# ---------------------------------------------------------------------------
# Department derivation — users never choose a department directly
# ---------------------------------------------------------------------------
def resolve_department(scope: UserScope, *, equipment=None, laboratory=None, department_id=None):
    """Department from equipment, else laboratory, else an explicit ``department_id``, else the user's only one."""
    from iic_booking.users.models import Department

    if equipment is not None:
        dept_id = equipment.internal_department_id
        if not dept_id:
            raise ProcurementError("This equipment is not mapped to a department.", code="equipment_no_department")
        if laboratory is not None and laboratory.department_id != dept_id:
            raise ProcurementError("The laboratory and equipment belong to different departments.", code="lab_mismatch")
    elif laboratory is not None:
        dept_id = laboratory.department_id
    elif department_id not in (None, ""):
        dept_id = pick_department(scope, department_id).pk
    else:
        ids = scope.department_ids()
        if len(ids) != 1:
            raise ProcurementError(
                "Choose the equipment or laboratory this is for; the department is taken from it.",
                code="department_ambiguous",
            )
        dept_id = next(iter(ids))
    if dept_id not in scope.enabled:
        raise ProcurementError(
            "Procurement & Assets is not enabled for this department.", status=403, code=c.DISABLED_CODE
        )
    if dept_id not in scope.department_ids():
        raise forbidden("You have no role in this department.")
    return Department.objects.get(pk=dept_id)


def pick_department(scope: UserScope, raw_id=None):
    """Department named by an explicit ``department_id`` (must be one of the user's enabled departments), or the
    user's only department. Used by department-wide screens (masters, workspace, reports)."""
    from iic_booking.users.models import Department

    ids = scope.department_ids()
    if raw_id in (None, ""):
        if len(ids) != 1:
            raise ProcurementError("department_id is required.", code="department_ambiguous", field="department_id")
        dept_id = next(iter(ids))
    else:
        try:
            dept_id = int(str(raw_id))
        except (TypeError, ValueError):
            raise ProcurementError("department_id must be an id.", code="invalid_id", field="department_id")
        if dept_id not in ids:
            raise not_found("Department not found.")
    return Department.objects.get(pk=dept_id)


def raising_role(scope: UserScope, dept_id: int, equipment=None) -> str:
    """The role a user acts in when raising a request (determines whether OIC approval applies)."""
    eid = getattr(equipment, "pk", None)
    if eid and scope.is_oic_for(eid):
        return R.OIC
    if eid and scope.is_operator_for(eid):
        return R.LAB_OPERATOR
    roles = scope.roles(dept_id)
    for role in (R.OFFICE, R.OC_STORES, R.HOD, R.MAIN_ADMIN, R.OIC, R.LAB_OPERATOR):
        if role in roles:
            return role
    raise forbidden("You cannot raise requests in this department.")


def can_raise_for_equipment(scope: UserScope, dept_id: int, equipment) -> bool:
    if equipment is None:
        return True
    eid = equipment.pk
    return scope.is_oic_for(eid) or scope.is_operator_for(eid) or scope.dept_wide(dept_id)
