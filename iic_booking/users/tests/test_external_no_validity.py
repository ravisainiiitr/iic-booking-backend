"""External users register and complete their profile without an access validity date (9 Nov 2026)."""

import pytest

from iic_booking.users import onboarding
from iic_booking.users.api.auth_serializers import RegisterSerializer
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db

BASE = {"email": "a@college.edu.in", "password": "Str0ng-pass!x", "password_confirm": "Str0ng-pass!x", "name": "A User",
        "phone_number": "9876543210", "gender": "female"}


def _errors(user_type, **extra):
    s = RegisterSerializer(data={**BASE, "user_type": user_type, **extra})
    s.is_valid()
    return s.errors


def test_external_users_need_no_end_date():
    assert "program_end_date" not in _errors(UserType.EXTERNAL)
    assert "program_end_date" not in _errors(UserType.RND)


def test_iitr_users_still_give_one():
    assert "program_end_date" in _errors(UserType.STUDENT, emp_id="21123456", user_type_alias="IITR Students")


def test_profile_completion_does_not_ask_for_it():
    user = User.objects.create_user(email="x@lab.example.org", password=None, user_type=UserType.EXTERNAL)
    assert "programme" not in onboarding.missing_steps(user)
