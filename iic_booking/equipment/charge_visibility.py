"""Who may see internal (IIT Roorkee student / faculty) charge rates."""

from __future__ import annotations

from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType


def internal_rate_user_types() -> set[str]:
    return {str(c).lower() for c in UserType.get_internal_user_codes()}


def is_internal_rate_user_type(code) -> bool:
    return bool(code) and str(code).strip().lower() in internal_rate_user_types()


def viewer_may_see_internal_rates(user) -> bool:
    """Internal IITR users and staff see internal rates; anonymous and external users do not."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    user_type = str(getattr(user, "user_type", "") or "")
    if user_type in UserType.get_management_user_codes():
        return True
    if not is_internal_rate_user_type(user_type):
        return False
    department = getattr(user, "department", None)
    if department is not None and getattr(department, "department_type", None) == DepartmentType.EXTERNAL:
        return False
    return True


def request_may_see_internal_rates(request) -> bool:
    return viewer_may_see_internal_rates(getattr(request, "user", None) if request is not None else None)
