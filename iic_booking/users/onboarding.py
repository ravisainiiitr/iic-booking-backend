"""Completing the profile after signing up with an I-STEM account (1 Nov 2026).

An account created directly from I-STEM (``istem.direct_signup``) has no password and may lack what the registration
form normally asks for. The dashboard asks for each missing item one by one and, last, a password for the next sign-in.
Only external user types are asked (internal users sign in with Channel i and keep no portal password).

Steps, in order: ``organisation`` (State/UT and organisation), ``gender``, ``mobile``, ``programme`` (validity) and
``password``.
"""

from __future__ import annotations

from datetime import date

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

STEPS = ("organisation", "gender", "mobile", "programme", "password")


class OnboardingError(Exception):
    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.message, self.field = message, field


def _applies(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return str(getattr(user, "user_type", "") or "") in UserType.get_external_user_codes()


def missing_steps(user) -> list[str]:
    if not _applies(user):
        return []
    missing = []
    if not user.department_id and not getattr(user, "organization_request_id", None):
        missing.append("organisation")
    if not (user.gender or "").strip():
        missing.append("gender")
    if len("".join(ch for ch in (user.phone_number or "") if ch.isdigit())) < 10:
        missing.append("mobile")
    if not user.program_end_date:
        missing.append("programme")
    if not user.has_usable_password():
        missing.append("password")
    return missing


def status(user) -> dict:
    """What the dashboard asks next, with what I-STEM already told us (State/UT, organisation name)."""
    from iic_booking.istem.direct_signup import SUBCATEGORY_BY_USER_TYPE, match_state

    steps = missing_steps(user)
    hints = {}
    link = getattr(user, "istem_link", None)
    profile = getattr(link, "istem_profile", None) or {}
    if "organisation" in steps:
        hints = {
            "state": match_state(str(profile.get("state") or "")) or "",
            "organisation": str(profile.get("organisation") or ""),
            "subcategory": SUBCATEGORY_BY_USER_TYPE.get(str(user.user_type or ""), ""),
        }
    return {
        "required": bool(steps),
        "steps": steps,
        "hints": hints,
        "values": {
            "gender": user.gender or "",
            "mobile": user.phone_number or "",
            "program_start_date": user.program_start_date.isoformat() if user.program_start_date else "",
            "program_end_date": user.program_end_date.isoformat() if user.program_end_date else "",
        },
    }


def _parse_date(value, field: str) -> date:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        raise OnboardingError("Enter a valid date.", field)


def save_step(user, step: str, data: dict) -> None:
    from iic_booking.users.models import Department
    from iic_booking.users.models.department import DepartmentType
    from iic_booking.users.models.user import Gender

    if step not in STEPS or not _applies(user):
        raise OnboardingError("Unknown step.")
    if step == "organisation":
        try:
            dept = Department.objects.get(pk=int(data.get("department")), department_type=DepartmentType.EXTERNAL)
        except (Department.DoesNotExist, TypeError, ValueError):
            raise OnboardingError("Choose your organisation from the list.", "department")
        user.department = dept
        user.save(update_fields=["department"])
    elif step == "gender":
        value = str(data.get("gender") or "").strip().lower()
        if value not in {c for c, _ in Gender.get_choices()}:
            raise OnboardingError("Choose an option.", "gender")
        user.gender = value
        user.save(update_fields=["gender"])
    elif step == "mobile":
        digits = "".join(ch for ch in str(data.get("mobile") or "") if ch.isdigit())[-10:]
        if len(digits) != 10:
            raise OnboardingError("Enter a 10-digit mobile number.", "mobile")
        user.phone_number = digits
        user.save(update_fields=["phone_number"])
    elif step == "programme":
        end = _parse_date(data.get("program_end_date"), "program_end_date")
        start = _parse_date(data.get("program_start_date"), "program_start_date") if data.get("program_start_date") else None
        today = timezone.localdate()
        if end <= today:
            raise OnboardingError("The end date must be in the future.", "program_end_date")
        if start and start > end:
            raise OnboardingError("The start date must be before the end date.", "program_start_date")
        user.program_end_date = end
        user.program_start_date = start
        # Same rule as registration: validity beyond a year puts access on hold until revalidated with documents.
        user.access_on_hold = (end - (start or today)).days > 365
        user.save(update_fields=["program_end_date", "program_start_date", "access_on_hold"])
    else:  # password
        password = str(data.get("password") or "")
        if password != str(data.get("password_confirm") or ""):
            raise OnboardingError("The two passwords do not match.", "password_confirm")
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise OnboardingError(" ".join(exc.messages), "password")
        user.set_password(password)
        user.save(update_fields=["password"])
