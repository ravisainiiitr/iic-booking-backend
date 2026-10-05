"""Read side of the per-department module switches, used by each module's existing server-side gates.

Rule for work that started at ``t`` (for bookings: ``Booking.created_at``) in a department:

    allowed = (enabled or t < disabled_at) and (not test_users_only or is_test(user) or t < test_only_since)

New work uses ``t = None`` (now), i.e. ``enabled and (not test_users_only or is_test(user))``. So switching a module
off, or limiting it to test users, never stops work that was already booked or running.

Equipment without a department and users without a department are not restricted (the switches only ever narrow what
the global and per-equipment flags already allow). A department without a row is off if it was created after the
switches were installed (``DepartmentModulesInstallation``) and otherwise not restricted. Any database error (e.g. the
table not migrated yet) falls back to "not restricted" so the gates can never take a working module down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import DatabaseError, transaction
from django.db.models import Q

from .constants import LOCAL_MODULES, ModuleKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cell:
    enabled: bool = True
    test_users_only: bool = False
    disabled_at: datetime | None = None
    test_only_since: datetime | None = None
    configured: bool = False

    @property
    def restricted(self) -> bool:
        return not self.enabled or self.test_users_only

    def allows(self, *, is_test: bool = False, started_at: datetime | None = None) -> bool:
        on = self.enabled or bool(started_at and self.disabled_at and started_at < self.disabled_at)
        if not on:
            return False
        if not self.test_users_only or is_test:
            return True
        return bool(started_at and self.test_only_since and started_at < self.test_only_since)


UNCONFIGURED = Cell()
NEW_DEPARTMENT_OFF = Cell(enabled=False)


def _cell_from_row(row) -> Cell:
    return Cell(
        enabled=bool(row.enabled),
        test_users_only=bool(row.test_users_only),
        disabled_at=row.disabled_at,
        test_only_since=row.test_only_since,
        configured=True,
    )


def _rows(**filters):
    from .models import DepartmentModuleSetting

    try:
        with transaction.atomic():
            return list(DepartmentModuleSetting.objects.filter(**filters))
    except DatabaseError:
        logger.warning("department module settings unavailable; treating as not configured", exc_info=True)
        return []


def installed_at() -> datetime | None:
    """When the switches were installed (the seeding migration), or None while they are not installed."""
    from .models import DepartmentModulesInstallation

    try:
        with transaction.atomic():
            return DepartmentModulesInstallation.objects.filter(pk=1).values_list("installed_at", flat=True).first()
    except DatabaseError:
        logger.warning("department modules installation record unavailable; treating as not installed", exc_info=True)
        return None


def new_department_ids(department_ids=None) -> set[int]:
    """Departments created after the switches were installed (they start off until the Main Administrator acts)."""
    from iic_booking.users.models import Department

    since = installed_at()
    if since is None:
        return set()
    qs = Department.objects.filter(created_at__gte=since)
    if department_ids is not None:
        qs = qs.filter(pk__in=list(department_ids))
    try:
        with transaction.atomic():
            return set(qs.values_list("pk", flat=True))
    except DatabaseError:
        logger.warning("department lookup failed; treating departments as pre-existing", exc_info=True)
        return set()


def default_cell(department_id) -> Cell:
    """Cell of a department that has no row for a module."""
    return NEW_DEPARTMENT_OFF if department_id and department_id in new_department_ids([department_id]) else UNCONFIGURED


def cell(department_id, module_key: str) -> Cell:
    if not department_id or module_key not in LOCAL_MODULES:
        return UNCONFIGURED
    rows = _rows(department_id=department_id, module_key=module_key)
    return _cell_from_row(rows[0]) if rows else default_cell(department_id)


def cells(module_key: str) -> dict[int, Cell]:
    out = {r.department_id: _cell_from_row(r) for r in _rows(module_key=module_key)}
    for dept_id in new_department_ids():
        out.setdefault(dept_id, NEW_DEPARTMENT_OFF)
    return out


def restricted_cells(module_key: str) -> dict[int, Cell]:
    return {dept_id: c for dept_id, c in cells(module_key).items() if c.restricted}


def blocked_department_ids(module_key: str) -> set[int]:
    """Departments whose switch is off (test-users-only departments are not blocked)."""
    return {dept_id for dept_id, c in cells(module_key).items() if not c.enabled}


def refused_department_ids(module_key: str, user=None, *, started_at: datetime | None = None) -> set[int]:
    """Departments whose switch refuses this user's work started at ``started_at`` (None: new work)."""
    test = is_test(user)
    return {
        dept_id
        for dept_id, c in restricted_cells(module_key).items()
        if not c.allows(is_test=test, started_at=started_at)
    }


def is_test(user) -> bool:
    from iic_booking.users.test_accounts import is_test_user

    return bool(user is not None and is_test_user(user))


def department_allows(department_id, module_key: str, user=None, *, started_at: datetime | None = None) -> bool:
    if not department_id:
        return True
    return cell(department_id, module_key).allows(is_test=is_test(user), started_at=started_at)


def equipment_allows(equipment, module_key: str, user=None, *, started_at: datetime | None = None) -> bool:
    return department_allows(getattr(equipment, "internal_department_id", None), module_key, user, started_at=started_at)


def booking_allows(booking, module_key: str) -> bool:
    """Whether the booking's department allows the module for this booking (owner + booking time)."""
    return equipment_allows(
        getattr(booking, "equipment", None),
        module_key,
        getattr(booking, "user", None),
        started_at=getattr(booking, "created_at", None),
    )


def user_department_allows(user, module_key: str) -> bool:
    """New-work check for the user's own department (used for showing a module to its users)."""
    return department_allows(getattr(user, "department_id", None), module_key, user)


def test_user_q(prefix: str = "") -> Q:
    from iic_booking.users.test_accounts import FORCE_EMAIL_REDIRECT_ADDRESSES

    q = Q(**{f"{prefix}is_test_account": True})
    for email in FORCE_EMAIL_REDIRECT_ADDRESSES:
        q |= Q(**{f"{prefix}email__iexact": email})
    return q


_NOTHING = Q(pk__in=[])


def allowed_bookings_q(module_key: str, *, equipment_field: str = "equipment", user_field: str = "user") -> Q | None:
    """Q selecting bookings the department switches allow, or None when no department is restricted."""
    restricted = restricted_cells(module_key)
    if not restricted:
        return None
    dept = f"{equipment_field}__internal_department_id"
    q = Q(**{f"{dept}__isnull": True}) | ~Q(**{f"{dept}__in": list(restricted)})
    tests = test_user_q(f"{user_field}__")
    for dept_id, c in restricted.items():
        on_q = Q() if c.enabled else (Q(created_at__lt=c.disabled_at) if c.disabled_at else _NOTHING)
        if c.test_users_only:
            test_q = tests | (Q(created_at__lt=c.test_only_since) if c.test_only_since else _NOTHING)
        else:
            test_q = Q()
        q |= Q(**{dept: dept_id}) & on_q & test_q
    return q


def allowed_users_q(module_key: str, *, prefix: str = "") -> Q | None:
    """Q selecting users whose own department allows the module, or None when no department is restricted."""
    restricted = restricted_cells(module_key)
    if not restricted:
        return None
    dept = f"{prefix}department_id"
    q = Q(**{f"{dept}__isnull": True}) | ~Q(**{f"{dept}__in": list(restricted)})
    test_only = [d for d, c in restricted.items() if c.enabled and c.test_users_only]
    if test_only:
        q |= Q(**{f"{dept}__in": test_only}) & test_user_q(prefix)
    return q


# ---------------------------------------------------------------------------
# Per-user availability (for /users/me/ and the frontend)
# ---------------------------------------------------------------------------
def is_main_admin(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)
    )


def _equipment_department_ids(user) -> set[int]:
    """Departments of equipment the user manages (OIC incl. temporary) or operates."""
    try:
        from iic_booking.equipment.models import Equipment, EquipmentOperator
        from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

        ids = set(get_equipment_ids_managed_by_oic(user.id) or [])
        ids |= set(EquipmentOperator.objects.filter(operator=user).values_list("equipment_id", flat=True))
        if not ids:
            return set()
        return set(
            Equipment.objects.filter(equipment_id__in=ids, internal_department_id__isnull=False).values_list(
                "internal_department_id", flat=True
            )
        )
    except Exception:  # noqa: BLE001 - availability is advisory; never break the caller
        logger.exception("department module availability: equipment lookup failed")
        return set()


def _training_enabled(user) -> bool:
    try:
        from iic_booking.training import access as training_access

        return bool(training_access.availability(user)["enabled"])
    except Exception:  # noqa: BLE001
        logger.exception("department module availability: training lookup failed")
        return False


def _procurement_enabled(user) -> bool:
    try:
        from iic_booking.procurement_management.access import scope_for

        return bool(scope_for(user).department_ids())
    except Exception:  # noqa: BLE001
        logger.exception("department module availability: procurement lookup failed")
        return False


def user_availability(user) -> dict:
    """Effective module availability for one user.

    ``available`` drives showing the module's entry points. Staff who manage or operate equipment of a department
    where a module is on keep it even if their own department has it off. The Main Administrator always sees DSA,
    Remote Analysis and Training and may configure every module (``can_configure``); Procurement availability is
    Procurement's own answer (its pilot list applies to Main Administrators too).
    """
    admin = is_main_admin(user)
    dept_id = getattr(user, "department_id", None)
    test = is_test(user)
    equipment_depts = _equipment_department_ids(user) if not admin else set()
    modules: dict[str, dict] = {}
    for key in (ModuleKey.DSA, ModuleKey.REMOTE_ANALYSIS):
        own = cell(dept_id, key) if dept_id else UNCONFIGURED
        via_equipment = any(department_allows(d, key, user) for d in equipment_depts)
        modules[key] = {
            "available": admin or own.allows(is_test=test) or via_equipment,
            "department_enabled": own.enabled,
            "test_users_only": own.test_users_only,
            "configured": own.configured,
        }
    own_training = cell(dept_id, ModuleKey.TRAINING) if dept_id else UNCONFIGURED
    modules[ModuleKey.TRAINING] = {
        "available": admin or _training_enabled(user),
        "department_enabled": own_training.enabled,
        "test_users_only": own_training.test_users_only,
        "configured": own_training.configured,
    }
    # Procurement answers for itself: in pilot mode even a Main Administrator needs to be on a pilot list.
    procurement = _procurement_enabled(user)
    modules[ModuleKey.PROCUREMENT] = {
        "available": procurement,
        "department_enabled": procurement,
        "test_users_only": False,
        "configured": True,
    }
    return {"department_id": dept_id, "is_test_account": test, "can_configure": admin, "modules": modules}


def availability_block(user) -> dict | None:
    """``user_availability`` for embedding in other responses; never lets a failure break the host endpoint."""
    try:
        return user_availability(user)
    except Exception:  # noqa: BLE001
        logger.exception("department module availability failed")
        return None
