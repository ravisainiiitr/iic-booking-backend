"""Feature flags, equipment scope, audience and role checks for Training & Certification.

Module on   = Main Admin switch (``TrainingModuleSettings.module_enabled``) OR env ``TRAINING_MODULE_ENABLED``.
Equipment   = equipment the Main Admin enabled (``TrainingEquipmentSetting``) plus env
              ``TRAINING_PILOT_EQUIPMENT_CODES``. When neither names any equipment and the env switch is on,
              every equipment is in scope (the original env-only pilot behaviour); with only the DB switch
              on and nothing enabled, no equipment is in scope.
Audience    = ``TEST_ACCOUNTS`` (default): faculty/students must be flagged test accounts; OICs, operators,
              dept admins and admins are unaffected. ``EVERYONE``: everyone eligible.

OIC scope always goes through ``get_equipment_ids_managed_by_oic`` so temporary OICs are covered.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import DatabaseError, transaction

from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

DISABLED_CODE = "training_disabled"
AUDIENCE_CODE = "training_not_available"
STUDENT_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT)
STAFF_TYPES = (UserType.ADMIN, UserType.MANAGER, UserType.OPERATOR, UserType.DEPT_ADMIN)


def env_module_enabled() -> bool:
    return bool(getattr(settings, "TRAINING_MODULE_ENABLED", False))


def module_settings():
    """The Main Admin settings row; defaults (off, test accounts only) until migrated or first saved."""
    from .models import TrainingModuleSettings

    try:
        with transaction.atomic():
            # Deferred so the switch and audience keep working before the column's migration has run.
            row = TrainingModuleSettings.objects.defer("course_demos_free").filter(pk=TrainingModuleSettings.SINGLETON_PK).first()
            return row or TrainingModuleSettings(pk=TrainingModuleSettings.SINGLETON_PK)
    except DatabaseError:
        logger.warning("training module settings unavailable; using defaults", exc_info=True)
        return TrainingModuleSettings(pk=TrainingModuleSettings.SINGLETON_PK)


def db_module_enabled() -> bool:
    return bool(module_settings().module_enabled)


def module_enabled() -> bool:
    return env_module_enabled() or db_module_enabled()


def audience() -> str:
    return module_settings().audience


def test_accounts_only() -> bool:
    from .models import TrainingAudience

    return audience() != TrainingAudience.EVERYONE


def is_main_admin(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or user.user_type == UserType.ADMIN)
    )


def in_audience(user) -> bool:
    """Whether the user may see Training at all under the current audience setting and their department's switch.

    Staff are not limited by their own department: their roles follow the equipment scope, which already drops the
    equipment of departments where Training is off.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    from iic_booking.department_modules.access import user_department_allows
    from iic_booking.department_modules.constants import ModuleKey
    from iic_booking.users.test_accounts import is_test_user

    if getattr(user, "is_superuser", False) or user.user_type in STAFF_TYPES:
        return True
    if not user_department_allows(user, ModuleKey.TRAINING):
        return False
    if is_test_user(user):
        return True
    return not test_accounts_only()


def equipment_allows_user(equipment, user) -> bool:
    """On equipment of a test-users-only department only test accounts take part (staff manage it as usual)."""
    if user is not None and (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) in STAFF_TYPES):
        return True
    from iic_booking.department_modules.access import equipment_allows
    from iic_booking.department_modules.constants import ModuleKey

    return equipment_allows(equipment, ModuleKey.TRAINING, user)


def audience_users(qs, *, equipment=None):
    """Narrow a User queryset to the audience (used for broadcast notifications)."""
    from iic_booking.department_modules import access as dept_access
    from iic_booking.department_modules.constants import ModuleKey

    if test_accounts_only():
        qs = qs.filter(dept_access.test_user_q())
    allowed = dept_access.allowed_users_q(ModuleKey.TRAINING)
    if allowed is not None:
        qs = qs.filter(allowed)
    dept_id = getattr(equipment, "internal_department_id", None)
    if dept_id and dept_access.cell(dept_id, ModuleKey.TRAINING).test_users_only:
        qs = qs.filter(dept_access.test_user_q())
    return qs


def _csv(raw: str, *, lower: bool = False, upper: bool = False) -> set[str]:
    out = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.add(part.lower() if lower else part.upper() if upper else part)
    return out


def pilot_equipment_codes() -> set[str]:
    return _csv(getattr(settings, "TRAINING_PILOT_EQUIPMENT_CODES", "") or "", upper=True)


def pilot_oic_emails() -> set[str]:
    return _csv(getattr(settings, "TRAINING_PILOT_OIC_EMAILS", "") or "", lower=True)


def db_enabled_equipment_ids() -> set[int]:
    from .models import TrainingEquipmentSetting

    try:
        with transaction.atomic():
            return set(TrainingEquipmentSetting.objects.filter(enabled=True).values_list("equipment_id", flat=True))
    except DatabaseError:
        logger.warning("training equipment settings unavailable", exc_info=True)
        return set()


def env_pilot_equipment_ids() -> set[int]:
    from iic_booking.equipment.models import Equipment

    codes = pilot_equipment_codes()
    if not codes:
        return set()
    return set(Equipment.objects.filter(code__in=codes).values_list("equipment_id", flat=True))


def _base_pilot_equipment_ids() -> set[int] | None:
    ids = db_enabled_equipment_ids()
    if pilot_equipment_codes():
        return ids | env_pilot_equipment_ids()
    if not ids and env_module_enabled():
        return None
    return ids


def pilot_equipment_ids() -> set[int] | None:
    """Equipment with Training enabled. None when every equipment is in scope (env switch, no list anywhere).

    Equipment of departments where the Main Administrator switched Training off is never in scope.
    """
    from iic_booking.department_modules.access import blocked_department_ids
    from iic_booking.department_modules.constants import ModuleKey
    from iic_booking.equipment.models import Equipment

    ids = _base_pilot_equipment_ids()
    blocked = blocked_department_ids(ModuleKey.TRAINING)
    if not blocked:
        return ids
    if ids is None:
        return set(Equipment.objects.exclude(internal_department_id__in=blocked).values_list("equipment_id", flat=True))
    return ids - set(
        Equipment.objects.filter(equipment_id__in=ids, internal_department_id__in=blocked).values_list(
            "equipment_id", flat=True
        )
    )


def pilot_equipment_queryset():
    from iic_booking.equipment.models import Equipment

    qs = Equipment.objects.all()
    ids = pilot_equipment_ids()
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    return qs


def equipment_in_pilot(equipment) -> bool:
    ids = pilot_equipment_ids()
    return ids is None or getattr(equipment, "equipment_id", None) in ids


def _restrict(ids, pilot_ids: set[int] | None) -> set[int]:
    ids = set(ids or [])
    return ids if pilot_ids is None else ids & pilot_ids


def is_admin(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.user_type == UserType.ADMIN)


def is_faculty(user) -> bool:
    """Faculty inside the audience (test faculty only while the audience is test accounts)."""
    return bool(
        user and getattr(user, "is_authenticated", False) and user.user_type == UserType.FACULTY and in_audience(user)
    )


def is_student(user) -> bool:
    return bool(
        user and getattr(user, "is_authenticated", False) and user.user_type in STUDENT_TYPES and in_audience(user)
    )


def oic_pilot_allowed(user) -> bool:
    emails = pilot_oic_emails()
    return not emails or (getattr(user, "email", "") or "").strip().lower() in emails


def oic_equipment_ids(user) -> set[int]:
    """Equipment the user manages as OIC or active temporary OIC, inside the pilot."""
    if not user or not getattr(user, "is_authenticated", False):
        return set()
    if user.user_type != UserType.MANAGER or not oic_pilot_allowed(user):
        return set()
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

    return _restrict(get_equipment_ids_managed_by_oic(user.id), pilot_equipment_ids())


def operator_equipment_ids(user) -> set[int]:
    """Equipment where the user is a Lab Operator (primary/secondary, honouring coverage), inside the pilot."""
    if not user or not getattr(user, "is_authenticated", False) or user.user_type != UserType.OPERATOR:
        return set()
    from iic_booking.equipment.api_views import _get_equipment_ids_for_log_access

    return _restrict(_get_equipment_ids_for_log_access(user) or [], pilot_equipment_ids())


def dept_admin_department_id(user) -> int | None:
    """Department of a dept admin holding ``training.manage``."""
    if not user or not getattr(user, "is_authenticated", False) or user.user_type != UserType.DEPT_ADMIN:
        return None
    from iic_booking.users.rbac import is_department_admin, user_has_permission

    if not is_department_admin(user) or not user_has_permission(user, "training.manage"):
        return None
    return user.department_id


def dept_admin_equipment_ids(user) -> set[int]:
    dept_id = dept_admin_department_id(user)
    if not dept_id:
        return set()
    from iic_booking.equipment.models import Equipment

    return _restrict(
        Equipment.objects.filter(internal_department_id=dept_id).values_list("equipment_id", flat=True),
        pilot_equipment_ids(),
    )


def managed_equipment_ids(user) -> set[int] | None:
    """Equipment the user may manage training for. None = all (Main Admin)."""
    if is_admin(user):
        return None
    return oic_equipment_ids(user)


def can_manage_equipment(user, equipment_id: int) -> bool:
    if is_admin(user):
        return True
    return equipment_id in oic_equipment_ids(user)


def can_mark_attendance(user, equipment_id: int | None) -> bool:
    if equipment_id is None:
        return is_admin(user)
    return can_manage_equipment(user, equipment_id) or equipment_id in operator_equipment_ids(user)


def can_decide_appeal(user, equipment_id: int) -> bool:
    return can_manage_equipment(user, equipment_id) or equipment_id in dept_admin_equipment_ids(user)


def can_view_equipment(user, equipment_id: int) -> bool:
    return can_decide_appeal(user, equipment_id) or equipment_id in operator_equipment_ids(user)


def availability(user) -> dict:
    """Session data the frontend uses to show or hide Training menus.

    ``enabled`` is per user: false for faculty/students outside the audience, so they see nothing.
    """
    enabled = module_enabled() and in_audience(user)
    scope_ids = pilot_equipment_ids() if enabled else set()
    roles = {
        "admin": is_admin(user),
        "faculty": is_faculty(user) and _internal_department(user),
        "student": is_student(user),
        "oic": bool(oic_equipment_ids(user)) if enabled else False,
        "operator": bool(operator_equipment_ids(user)) if enabled else False,
        "dept_admin": bool(dept_admin_department_id(user)) if enabled else False,
    }
    roles["duty_operator"] = enabled and roles["student"] and has_duty_role(user)
    menus = {
        "training_events": enabled and roles["faculty"],
        "my_trainings": enabled and roles["student"],
        "training_workspace": enabled and (roles["admin"] or roles["oic"] or roles["dept_admin"]),
        "training_attendance": enabled and roles["operator"],
        "training_policy_settings": roles["admin"],
        "operator_duty": enabled and (roles["admin"] or roles["oic"] or roles["dept_admin"]),
        "my_duty": roles["duty_operator"],
    }
    return {
        "enabled": enabled,
        "audience": audience(),
        "pilot": scope_ids is not None or bool(pilot_oic_emails()),
        "pilot_equipment_count": len(scope_ids) if scope_ids is not None else 0,
        "can_manage_module": is_main_admin(user),
        "roles": roles,
        "menus": menus,
    }


def has_duty_role(user) -> bool:
    """On an operator roster or ever allocated duty (False until the duty tables are migrated)."""
    from django.db import DatabaseError, transaction

    from .models import DutyAllocation, OperatorRosterEntry, RosterStatus

    try:
        with transaction.atomic():
            return (
                OperatorRosterEntry.objects.filter(user=user, status=RosterStatus.ACTIVE).exists()
                or DutyAllocation.objects.filter(operator=user).exists()
            )
    except DatabaseError:
        return False


def _internal_department(user) -> bool:
    from iic_booking.users.models.department import DepartmentType

    department = getattr(user, "department", None)
    return bool(department and department.department_type == DepartmentType.INTERNAL)


def faculty_group_student_ids(faculty) -> set[int]:
    """Students the faculty may nominate: supervised users or approved wallet joiners."""
    from iic_booking.users.models import User
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    supervised = set(User.objects.filter(supervisor=faculty).values_list("id", flat=True))
    joined = set(
        WalletJoinRequest.objects.filter(faculty=faculty, status=WalletJoinRequestStatus.APPROVED).values_list(
            "student_id", flat=True
        )
    )
    return supervised | joined


def is_valid_nominator(faculty, student) -> bool:
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    if not faculty or not student:
        return False
    if student.supervisor_id == faculty.id:
        return True
    return WalletJoinRequest.objects.filter(
        student=student, faculty=faculty, status=WalletJoinRequestStatus.APPROVED
    ).exists()
