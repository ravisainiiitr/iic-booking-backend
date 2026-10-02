"""Feature flags, pilot scope and role checks for Training & Certification.

OIC scope always goes through ``get_equipment_ids_managed_by_oic`` so temporary OICs are covered.
"""

from __future__ import annotations

from django.conf import settings

from iic_booking.users.models.user_type import UserType

DISABLED_CODE = "training_disabled"
STUDENT_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT)


def module_enabled() -> bool:
    return bool(getattr(settings, "TRAINING_MODULE_ENABLED", False))


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


def pilot_equipment_queryset():
    from iic_booking.equipment.models import Equipment

    qs = Equipment.objects.all()
    codes = pilot_equipment_codes()
    if codes:
        qs = qs.filter(code__in=codes)
    return qs


def equipment_in_pilot(equipment) -> bool:
    codes = pilot_equipment_codes()
    if not codes:
        return True
    return (getattr(equipment, "code", "") or "").upper() in codes


def pilot_equipment_ids() -> set[int] | None:
    """None when every equipment is in scope."""
    if not pilot_equipment_codes():
        return None
    return set(pilot_equipment_queryset().values_list("equipment_id", flat=True))


def _restrict(ids, pilot_ids: set[int] | None) -> set[int]:
    ids = set(ids or [])
    return ids if pilot_ids is None else ids & pilot_ids


def is_admin(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.user_type == UserType.ADMIN)


def is_faculty(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.user_type == UserType.FACULTY)


def is_student(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.user_type in STUDENT_TYPES)


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
    """Session data the frontend uses to show or hide Training menus."""
    enabled = module_enabled()
    roles = {
        "admin": is_admin(user),
        "faculty": is_faculty(user) and _internal_department(user),
        "student": is_student(user),
        "oic": bool(oic_equipment_ids(user)) if enabled else False,
        "operator": bool(operator_equipment_ids(user)) if enabled else False,
        "dept_admin": bool(dept_admin_department_id(user)) if enabled else False,
    }
    menus = {
        "training_events": enabled and roles["faculty"],
        "my_trainings": enabled and roles["student"],
        "training_workspace": enabled and (roles["admin"] or roles["oic"] or roles["dept_admin"]),
        "training_attendance": enabled and roles["operator"],
        "training_policy_settings": roles["admin"],
    }
    return {
        "enabled": enabled,
        "pilot": bool(pilot_equipment_codes() or pilot_oic_emails()),
        "pilot_equipment_count": len(pilot_equipment_codes()),
        "roles": roles,
        "menus": menus,
    }


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
