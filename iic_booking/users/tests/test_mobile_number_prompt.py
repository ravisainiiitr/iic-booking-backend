"""Post-login "Complete your profile" prompt: mobile number rule, needs_mobile_number flag and profile save."""

from __future__ import annotations

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.api.auth_views import _regenerate_auth_token
from iic_booking.users.mobile_number import is_valid_mobile_number
from iic_booking.users.mobile_number import normalize_indian_mobile
from iic_booking.users.mobile_number import user_needs_mobile_number
from iic_booking.users.models import User, UserType

USER_URL = "/api/auth/user/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9876543210", "9876543210"),
        ("+91 98765 43210", "9876543210"),
        ("+91-9876543210", "9876543210"),
        ("919876543210", "9876543210"),
        ("09876543210", "9876543210"),
        ("(987) 654-3210", "9876543210"),
        ("6000000000", "6000000000"),
        ("", None),
        (None, None),
        ("   ", None),
        ("0000000000", None),
        ("12345", None),
        ("98765", None),
        ("5876543210", None),
        ("98765432101", None),
        ("+1 415 555 0100", None),
        ("N/A", None),
    ],
)
def test_normalize_indian_mobile(value, expected):
    assert normalize_indian_mobile(value) == expected
    assert is_valid_mobile_number(value) is (expected is not None)


class _U:
    def __init__(self, user_type, phone_number):
        self.user_type = user_type
        self.phone_number = phone_number


@pytest.mark.parametrize(
    "user_type",
    [
        UserType.STUDENT,
        UserType.INDIVIDUAL_STUDENT,
        UserType.EXTERNAL,
        UserType.RND,
        UserType.INSTITUTE,
        UserType.STARTUP_INCUBATED_IITR,
        UserType.EXTERNAL_STARTUP_MSME,
        UserType.OTHER,
        UserType.OPERATOR,
        UserType.FINANCE,
        UserType.DEPT_ADMIN,
        UserType.ADMIN,
        UserType.ORG_ADMIN,
        UserType.EXTERNAL_RELATIONS,
        None,
    ],
)
def test_non_faculty_needs_mobile_until_valid(user_type):
    for missing in ("", None, "0000000000", "12345"):
        assert user_needs_mobile_number(_U(user_type, missing)) is True
    assert user_needs_mobile_number(_U(user_type, "9876543210")) is False


@pytest.mark.parametrize("user_type", [UserType.FACULTY, UserType.MANAGER, "Faculty"])
def test_faculty_and_oic_accounts_are_never_prompted(user_type):
    assert user_needs_mobile_number(_U(user_type, None)) is False
    assert user_needs_mobile_number(_U(user_type, "0000000000")) is False


def _client(user) -> APIClient:
    token = _regenerate_auth_token(user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _user(email, user_type, phone=None) -> User:
    user = User.objects.create_user(
        email=email, password="Known-pass-7731!", name="Prompt Tester", user_type=user_type, phone_number=phone
    )
    user.email_verified = True
    user.admin_approved = True
    user.last_login = timezone.now()
    user.save()
    return user


@pytest.mark.django_db
class TestProfileMobileApi:
    def test_current_user_reports_needs_mobile_number(self):
        student = _user("s.prompt@example.com", UserType.STUDENT, phone="0000000000")
        faculty = _user("f.prompt@example.com", UserType.FACULTY)
        assert _client(student).get(USER_URL).data["needs_mobile_number"] is True
        assert _client(faculty).get(USER_URL).data["needs_mobile_number"] is False

    def test_saving_valid_number_clears_flag_and_stores_ten_digits(self):
        user = _user("ext.prompt@example.com", UserType.EXTERNAL)
        client = _client(user)
        resp = client.patch(f"/api/users/{user.pk}/", {"phone_number": "+91 98765 43210"}, format="json")
        assert resp.status_code == 200, resp.data
        assert resp.data["phone_number"] == "9876543210"
        assert resp.data["needs_mobile_number"] is False
        user.refresh_from_db()
        assert user.phone_number == "9876543210"
        assert client.get(USER_URL).data["needs_mobile_number"] is False

    @pytest.mark.parametrize("bad", ["0000000000", "12345", "5876543210", "+1 415 555 0100"])
    def test_invalid_new_number_is_refused(self, bad):
        user = _user(f"op.{len(bad)}@example.com", UserType.OPERATOR)
        resp = _client(user).patch(f"/api/users/{user.pk}/", {"phone_number": bad}, format="json")
        assert resp.status_code == 400
        assert "phone_number" in resp.data
        user.refresh_from_db()
        assert not user.phone_number

    def test_resaving_existing_value_still_works(self):
        user = _user("legacy.prompt@example.com", UserType.OTHER, phone="+44 20 7946 0000")
        resp = _client(user).patch(
            f"/api/users/{user.pk}/", {"name": "Renamed", "phone_number": "+44 20 7946 0000"}, format="json"
        )
        assert resp.status_code == 200, resp.data
        user.refresh_from_db()
        assert user.name == "Renamed"
        assert user.phone_number == "+44 20 7946 0000"
        assert resp.data["needs_mobile_number"] is True
