"""Security hotfix: self-service profile edits, forgot-password OTP limits, sign-up limit, email uniqueness."""

from __future__ import annotations

import uuid

import pytest
from django.core.cache import cache
from rest_framework import serializers
from rest_framework.test import APIClient

from iic_booking.users.api.auth_serializers import RegisterSerializer
from iic_booking.users.api.auth_views import FORGOT_OTP_CACHE_PREFIX
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _external(**kwargs):
    defaults = {"user_type": UserType.EXTERNAL, "email_verified": True, "admin_approved": True, "phone_number": "9876500000"}
    return UserFactory(**{**defaults, **kwargs})


# --- self-service profile edits ------------------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("user_type", UserType.ADMIN),
    ("user_type", UserType.FACULTY),
    ("admin_approved", True),
    ("use_discounted_charge_profile", True),
    ("is_active", False),
    ("email_verified", False),
    ("force_inactive", True),
    ("emp_id", "EMP-SELF-1"),
])
def test_user_cannot_change_server_controlled_fields_on_own_account(field, value):
    user = _external(admin_approved=False) if field == "admin_approved" else _external()
    before = getattr(User.objects.get(pk=user.pk), field)
    response = _client(user).patch(f"/api/users/{user.pk}/", {field: value}, format="json")
    assert response.status_code == 400
    assert field in response.data["fields"]
    assert getattr(User.objects.get(pk=user.pk), field) == before


def test_user_cannot_change_own_department():
    tag = uuid.uuid4().hex[:6].upper()
    dept = Department.objects.create(name=f"Hotfix Physics {tag}", code=f"HP{tag}", department_type=DepartmentType.INTERNAL)
    user = _external()
    response = _client(user).patch(f"/api/users/{user.pk}/", {"department": dept.pk}, format="json")
    assert response.status_code == 400
    assert User.objects.get(pk=user.pk).department_id is None


def test_mixed_payload_is_refused_as_a_whole():
    user = _external(name="Asha Verma")
    response = _client(user).patch(f"/api/users/{user.pk}/", {"name": "Changed", "user_type": UserType.ADMIN}, format="json")
    assert response.status_code == 400
    user.refresh_from_db()
    assert (user.name, user.user_type) == ("Asha Verma", UserType.EXTERNAL)


def test_profile_page_fields_still_save():
    user = _external()
    payload = {"name": "Asha New", "phone_number": "9876543210", "wallet_low_balance_alert_enabled": False,
               "istem_portal_acknowledged": True}
    response = _client(user).patch(f"/api/users/{user.pk}/", payload, format="json")
    assert response.status_code == 200, response.data
    user.refresh_from_db()
    assert (user.name, user.phone_number, user.istem_portal_acknowledged) == ("Asha New", "9876543210", True)
    assert response.data["user_type"] == UserType.EXTERNAL


@pytest.mark.parametrize("staff_type", [UserType.MANAGER, UserType.OPERATOR, UserType.FINANCE, UserType.DEPT_ADMIN])
def test_non_admin_staff_cannot_edit_other_users(staff_type):
    staff = UserFactory(user_type=staff_type, email_verified=True, admin_approved=True)
    other = _external()
    response = _client(staff).patch(f"/api/users/{other.pk}/", {"name": "x", "user_type": UserType.ADMIN}, format="json")
    assert response.status_code == 403
    other.refresh_from_db()
    assert other.user_type == UserType.EXTERNAL


def test_admin_can_still_edit_other_users():
    admin = UserFactory(user_type=UserType.ADMIN, email_verified=True, admin_approved=True)
    other = UserFactory(user_type=UserType.STUDENT, email_verified=True, admin_approved=True)
    response = _client(admin).patch(f"/api/users/{other.pk}/", {"name": "Renamed", "user_type": UserType.OPERATOR}, format="json")
    assert response.status_code == 200, response.data
    other.refresh_from_db()
    assert (other.name, other.user_type) == ("Renamed", UserType.OPERATOR)


# --- forgot-password OTP ---------------------------------------------------------------------------------------------

def _password_user():
    user = _external(email="reset-me@example.org")
    user.set_password("an-old-password-123")
    user.save()
    return user


def _request_reset(client, email="reset-me@example.org"):
    return client.post("/api/auth/forgot-password/request-otp/", {"email": email}, format="json")


def _verify_reset(client, otp, email="reset-me@example.org"):
    return client.post("/api/auth/forgot-password/verify-otp-and-set-password/", {
        "email": email, "otp": otp, "new_password": "a-new-password-456", "new_password_confirm": "a-new-password-456",
    }, format="json")


def test_forgot_password_otp_is_invalidated_after_five_wrong_codes():
    user = _password_user()
    client = APIClient()
    assert _request_reset(client).status_code == 200
    real_otp = cache.get(f"{FORGOT_OTP_CACHE_PREFIX}{user.email}")["otp"]
    wrong = "000000" if real_otp != "000000" else "111111"
    for _ in range(4):
        response = _verify_reset(client, wrong)
        assert response.status_code == 400 and "code" not in response.data
    assert _verify_reset(client, wrong).data["code"] == "OTP_ATTEMPTS_EXCEEDED"
    assert _verify_reset(client, real_otp).status_code == 400  # the OTP is gone; guessing cannot continue
    user.refresh_from_db()
    assert user.check_password("an-old-password-123")


def test_forgot_password_correct_code_still_works():
    user = _password_user()
    client = APIClient()
    _request_reset(client)
    real_otp = cache.get(f"{FORGOT_OTP_CACHE_PREFIX}{user.email}")["otp"]
    assert _verify_reset(client, real_otp).status_code == 200
    user.refresh_from_db()
    assert user.check_password("a-new-password-456")


def test_forgot_password_otp_requests_are_limited():
    _password_user()
    client = APIClient()
    for _ in range(5):
        assert _request_reset(client).status_code == 200
    response = _request_reset(client)
    assert response.status_code == 429 and response.data["code"] == "OTP_RATE_LIMITED"


# --- sign-up -------------------------------------------------------------------------------------------------------

def test_sign_up_attempts_are_limited_per_ip_when_enabled(settings):
    settings.REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR = 2
    client = APIClient()
    assert client.post("/api/auth/register/", {}, REMOTE_ADDR="203.0.113.7").status_code == 400
    assert client.post("/api/auth/register/", {}, REMOTE_ADDR="203.0.113.7").status_code == 400
    response = client.post("/api/auth/register/", {}, REMOTE_ADDR="203.0.113.7")
    assert response.status_code == 429 and response.data["code"] == "REGISTRATION_RATE_LIMITED"
    assert client.post("/api/auth/register/", {}, REMOTE_ADDR="198.51.100.9").status_code == 400  # other IPs unaffected


def test_sign_up_limit_ignores_forged_forwarding_headers_without_trusted_proxies(settings):
    settings.REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR = 2
    settings.REGISTRATION_TRUSTED_PROXY_HOPS = 0
    client = APIClient()
    for forged in ("1.1.1.1", "2.2.2.2"):
        client.post("/api/auth/register/", {}, REMOTE_ADDR="203.0.113.7", HTTP_X_FORWARDED_FOR=forged)
    response = client.post("/api/auth/register/", {}, REMOTE_ADDR="203.0.113.7", HTTP_X_FORWARDED_FOR="3.3.3.3")
    assert response.status_code == 429  # rotating X-Forwarded-For does not reset the limit


def test_sign_up_limit_uses_the_address_added_by_our_proxy(settings):
    settings.REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR = 2
    settings.REGISTRATION_TRUSTED_PROXY_HOPS = 1  # one proxy (e.g. Traefik) appends the real client address
    client = APIClient()
    proxy = "10.0.0.5"
    for forged in ("1.1.1.1", "2.2.2.2"):
        client.post("/api/auth/register/", {}, REMOTE_ADDR=proxy, HTTP_X_FORWARDED_FOR=f"{forged}, 203.0.113.7")
    response = client.post("/api/auth/register/", {}, REMOTE_ADDR=proxy, HTTP_X_FORWARDED_FOR="3.3.3.3, 203.0.113.7")
    assert response.status_code == 429
    other = client.post("/api/auth/register/", {}, REMOTE_ADDR=proxy, HTTP_X_FORWARDED_FOR="198.51.100.9")
    assert other.status_code == 400  # a different real client behind the same proxy is not affected


def test_sign_up_limit_and_proxy_trust_are_off_unless_configured():
    import os

    from config.settings import base

    if "REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR" not in os.environ:
        assert base.REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR == 0
    if "REGISTRATION_TRUSTED_PROXY_HOPS" not in os.environ:
        assert base.REGISTRATION_TRUSTED_PROXY_HOPS == 0


def test_sign_up_limit_off_when_zero(settings):
    settings.REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR = 0
    client = APIClient()
    for _ in range(5):
        assert client.post("/api/auth/register/", {}, REMOTE_ADDR="203.0.113.7").status_code == 400


def test_email_uniqueness_ignores_case():
    UserFactory(email="Researcher@Example.org")
    with pytest.raises(serializers.ValidationError):
        RegisterSerializer().validate_email("researcher@example.org")
    assert RegisterSerializer().validate_email("someone-else@example.org") == "someone-else@example.org"
