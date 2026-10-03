"""IIC Booking app: audience gating (sign-in, device sessions), OTP limits and APK distribution."""

from __future__ import annotations

import hashlib
import re

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.deployment.models import MobileAppRelease, MobileAppSettings
from iic_booking.users.api.auth_views import _regenerate_auth_token
from iic_booking.users.models import MobileDeviceSession, User, UserType

pytestmark = pytest.mark.django_db

PASSWORD = "Known-pass-7731!"
ENROLL_URL = "/api/auth/mobile/enroll/"
REFRESH_URL = "/api/auth/mobile/refresh/"
LOGIN_URL = "/api/auth/login/"
OTP_REQUEST_URL = "/api/auth/login/request-otp/"
OTP_VERIFY_URL = "/api/auth/login/verify-otp/"
SETTINGS_URL = "/api/v1/deployment/mobile-app/settings/"
LATEST_URL = "/api/v1/deployment/mobile-app/latest/"
TICKET_URL = "/api/v1/deployment/mobile-app/latest/download-ticket/"
APK_BYTES = b"PK\x03\x04" + b"fake-apk-body" * 64


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.STORAGES = {
        **getattr(settings, "STORAGES", {}),
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    return tmp_path


def _department():
    from iic_booking.users.models import Department

    dept, _ = Department.objects.get_or_create(
        code="APPT",
        defaults={"name": "App Test Dept", "equipment_booking_enabled": True, "equipment_visibility_enabled": True},
    )
    return dept


def make_user(user_type, email=None) -> User:
    email = email or f"{user_type.lower()}.{User.objects.count()}@example.com"
    user = User.objects.create_user(email=email, password=PASSWORD, name="App User", user_type=user_type)
    user.email_verified = True
    user.admin_approved = True
    user.email_login_enabled = True
    user.department = _department()
    user.last_login = timezone.now()
    user.save()
    return user


def web_client(user) -> APIClient:
    token = _regenerate_auth_token(user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def enroll(user, device_id="device-abc-123"):
    return web_client(user).post(ENROLL_URL, {"device_id": device_id, "platform": "android"}, format="json")


# --- audience: device sessions ----------------------------------------------------------------------------------


@pytest.mark.parametrize("user_type", [UserType.MANAGER, UserType.OPERATOR, UserType.ADMIN])
def test_default_audience_can_enroll(user_type):
    resp = enroll(make_user(user_type))
    assert resp.status_code == 201, resp.data
    assert resp.data["require_biometric"] is (user_type == UserType.ADMIN)


@pytest.mark.parametrize("user_type", [UserType.FACULTY, UserType.STUDENT, UserType.EXTERNAL, UserType.DEPT_ADMIN])
def test_outside_audience_cannot_enroll(user_type):
    user = make_user(user_type)
    resp = enroll(user)
    assert resp.status_code == 403
    assert resp.data["code"] == "APP_AUDIENCE"
    assert resp.data["error"] == (
        "The IIC Booking app is currently available to Officers In Charge and Lab Operators. "
        "Please use https://equip.iitr.ac.in in your browser."
    )
    assert not MobileDeviceSession.objects.filter(user=user).exists()


def test_narrowing_audience_signs_out_on_next_refresh():
    operator = make_user(UserType.OPERATOR)
    issued = enroll(operator).data
    admin = make_user(UserType.ADMIN)

    resp = web_client(admin).patch(SETTINGS_URL, {"audience_user_types": ["manager"]}, format="json")
    assert resp.status_code == 200, resp.data

    refreshed = APIClient().post(
        REFRESH_URL, {"device_id": "device-abc-123", "refresh_token": issued["refresh_token"]}, format="json"
    )
    assert refreshed.status_code == 401
    assert refreshed.data["code"] == "APP_AUDIENCE"
    session = MobileDeviceSession.objects.get(pk=issued["device_session_id"])
    assert session.revoked_at is not None
    assert session.revoke_reason == "app_audience"


def test_widening_audience_allows_department_admin():
    admin = make_user(UserType.ADMIN)
    web_client(admin).patch(
        SETTINGS_URL, {"audience_user_types": ["manager", "operator", "admin", "dept_admin"]}, format="json"
    )
    assert enroll(make_user(UserType.DEPT_ADMIN)).status_code == 201


# --- audience setting -------------------------------------------------------------------------------------------


def test_settings_main_admin_only():
    for user_type in (UserType.MANAGER, UserType.OPERATOR, UserType.DEPT_ADMIN, UserType.FACULTY):
        assert web_client(make_user(user_type)).get(SETTINGS_URL).status_code == 403
    resp = web_client(make_user(UserType.ADMIN)).get(SETTINGS_URL)
    assert resp.status_code == 200
    assert resp.data["audience_user_types"] == ["manager", "operator", "admin"]
    assert {c["code"] for c in resp.data["choices"]} >= {"manager", "operator", "admin", "dept_admin"}


@pytest.mark.parametrize("bad", [[], ["superhero"], "manager", None])
def test_settings_rejects_invalid_audience(bad):
    client = web_client(make_user(UserType.ADMIN))
    resp = client.patch(SETTINGS_URL, {"audience_user_types": bad}, format="json")
    assert resp.status_code == 400
    assert MobileAppSettings.get_singleton().audience_user_types == ["manager", "operator", "admin"]


def test_settings_patch_saves_and_dedupes():
    admin = make_user(UserType.ADMIN)
    resp = web_client(admin).patch(
        SETTINGS_URL, {"audience_user_types": ["operator", "operator", "manager"]}, format="json"
    )
    assert resp.status_code == 200
    obj = MobileAppSettings.get_singleton()
    assert obj.audience_user_types == ["operator", "manager"]
    assert obj.updated_by == admin


# --- audience: app sign-in (no token for refused roles) --------------------------------------------------------


def test_app_password_login_refused_outside_audience_without_issuing_token():
    faculty = make_user(UserType.FACULTY)
    resp = APIClient().post(
        LOGIN_URL, {"email": faculty.email, "password": PASSWORD, "client": "iic_app"}, format="json"
    )
    assert resp.status_code == 403
    assert resp.data["code"] == "APP_AUDIENCE"
    assert not Token.objects.filter(user=faculty).exists()


def test_app_password_login_refusal_needs_correct_password():
    faculty = make_user(UserType.FACULTY)
    resp = APIClient().post(
        LOGIN_URL, {"email": faculty.email, "password": "wrong-password", "client": "iic_app"}, format="json"
    )
    assert resp.status_code == 401


def test_browser_login_unaffected_by_audience():
    faculty = make_user(UserType.FACULTY)
    resp = APIClient().post(LOGIN_URL, {"email": faculty.email, "password": PASSWORD}, format="json")
    assert resp.status_code == 200
    assert resp.data["token"]


def test_app_password_login_allowed_for_operator():
    operator = make_user(UserType.OPERATOR)
    resp = APIClient().post(
        LOGIN_URL, {"email": operator.email, "password": PASSWORD, "client": "iic_app"}, format="json"
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["token"]


def _otp_from_mail() -> str:
    match = re.search(r"\b(\d{6})\b", mail.outbox[-1].body)
    assert match
    return match.group(1)


def test_app_otp_sign_in_for_oic():
    oic = make_user(UserType.MANAGER)
    client = APIClient()
    resp = client.post(OTP_REQUEST_URL, {"email": oic.email, "client": "iic_app"}, format="json")
    assert resp.status_code == 200, resp.data
    code = _otp_from_mail()
    resp = client.post(OTP_VERIFY_URL, {"email": oic.email, "otp": code, "client": "iic_app"}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["token"] == Token.objects.get(user=oic).key
    # The fresh web token can then register the phone.
    enrolled = APIClient()
    enrolled.credentials(HTTP_AUTHORIZATION=f"Token {resp.data['token']}")
    assert enrolled.post(ENROLL_URL, {"device_id": "phone-oic-1", "platform": "android"}, format="json").status_code == 201


def test_app_otp_refused_outside_audience_before_sending_mail():
    student = make_user(UserType.STUDENT)
    resp = APIClient().post(OTP_REQUEST_URL, {"email": student.email, "client": "iic_app"}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == "APP_AUDIENCE"
    assert mail.outbox == []


def test_otp_wrong_codes_invalidate_the_otp():
    operator = make_user(UserType.OPERATOR)
    client = APIClient()
    client.post(OTP_REQUEST_URL, {"email": operator.email}, format="json")
    code = _otp_from_mail()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(4):
        resp = client.post(OTP_VERIFY_URL, {"email": operator.email, "otp": wrong}, format="json")
        assert resp.status_code == 400
        assert "code" not in resp.data
    resp = client.post(OTP_VERIFY_URL, {"email": operator.email, "otp": wrong}, format="json")
    assert resp.data["code"] == "OTP_ATTEMPTS_EXCEEDED"
    resp = client.post(OTP_VERIFY_URL, {"email": operator.email, "otp": code}, format="json")
    assert resp.status_code == 400


def test_otp_still_valid_after_a_few_wrong_codes():
    operator = make_user(UserType.OPERATOR)
    client = APIClient()
    client.post(OTP_REQUEST_URL, {"email": operator.email}, format="json")
    code = _otp_from_mail()
    wrong = "000000" if code != "000000" else "111111"
    client.post(OTP_VERIFY_URL, {"email": operator.email, "otp": wrong}, format="json")
    resp = client.post(OTP_VERIFY_URL, {"email": operator.email, "otp": code}, format="json")
    assert resp.status_code == 200


def test_otp_requests_are_rate_limited_per_email():
    operator = make_user(UserType.OPERATOR)
    client = APIClient()
    for _ in range(5):
        assert client.post(OTP_REQUEST_URL, {"email": operator.email}, format="json").status_code == 200
    resp = client.post(OTP_REQUEST_URL, {"email": operator.email}, format="json")
    assert resp.status_code == 429
    assert resp.data["code"] == "OTP_RATE_LIMITED"
    assert len(mail.outbox) == 5


# --- APK distribution -------------------------------------------------------------------------------------------


def _publish(tmp_path, version_name="1.0.0", version_code=1, body=APK_BYTES):
    apk = tmp_path / f"in-{version_code}.apk"
    apk.write_bytes(body)
    call_command("publish_mobile_app", str(apk), version_name=version_name, version_code=version_code, notes="First")
    return hashlib.sha256(body).hexdigest()


def test_latest_without_release(media):
    resp = web_client(make_user(UserType.OPERATOR)).get(LATEST_URL)
    assert resp.status_code == 200
    assert resp.data["release"] is None


def test_latest_and_download_for_audience(media):
    digest = _publish(media)
    for user_type in (UserType.OPERATOR, UserType.MANAGER, UserType.ADMIN):
        client = web_client(make_user(user_type))
        latest = client.get(LATEST_URL)
        assert latest.status_code == 200
        rel = latest.data["release"]
        assert rel["version_name"] == "1.0.0"
        assert rel["version_code"] == 1
        assert rel["sha256"] == digest
        assert rel["size_bytes"] == len(APK_BYTES)
        ticket = client.post(TICKET_URL)
        assert ticket.status_code == 200, ticket.data
        assert ticket.data["sha256"] == digest
        assert ticket.data["filename"] == "IIC-Booking-1.0.0.apk"

    path = ticket.data["url"].split("testserver", 1)[-1]
    download = APIClient().get(path)
    assert download.status_code == 200
    assert download["Content-Type"] == "application/vnd.android.package-archive"
    assert b"".join(download.streaming_content) == APK_BYTES
    assert MobileAppRelease.objects.get().download_count == 1


@pytest.mark.parametrize("user_type", [UserType.FACULTY, UserType.STUDENT, UserType.DEPT_ADMIN, UserType.FINANCE])
def test_apk_not_offered_outside_audience(media, user_type):
    _publish(media)
    client = web_client(make_user(user_type))
    assert client.get(LATEST_URL).status_code == 403
    assert client.post(TICKET_URL).status_code == 403


def test_apk_requires_sign_in(media):
    _publish(media)
    assert APIClient().get(LATEST_URL).status_code in (401, 403)
    assert APIClient().post(TICKET_URL).status_code in (401, 403)


def test_download_rejects_forged_or_foreign_tickets(media):
    from iic_booking.installer_download_tickets import issue_ticket

    _publish(media)
    rel = MobileAppRelease.objects.get()
    assert APIClient().get("/api/v1/deployment/mobile-app/download/not-a-ticket/").status_code == 403
    foreign = issue_ticket(product="eq_wizard", release_id=str(rel.id), offline=False)
    assert APIClient().get(f"/api/v1/deployment/mobile-app/download/{foreign}/").status_code == 403


def test_publish_marks_latest_and_is_idempotent(media):
    _publish(media)
    _publish(media)
    assert MobileAppRelease.objects.count() == 1
    newer = APK_BYTES + b"v2"
    _publish(media, version_name="1.0.1", version_code=2, body=newer)
    latest = MobileAppRelease.objects.get(is_latest=True)
    assert (latest.version_name, latest.version_code) == ("1.0.1", 2)
    assert MobileAppRelease.objects.filter(is_latest=True).count() == 1
    with pytest.raises(CommandError):
        _publish(media, version_name="1.0.1", version_code=2, body=newer + b"different")


def test_publish_rejects_non_apk(media):
    bad = media / "notes.txt"
    bad.write_text("hello")
    with pytest.raises(CommandError):
        call_command("publish_mobile_app", str(bad), version_name="1.0.0", version_code=1)
    zipless = media / "fake.apk"
    zipless.write_bytes(b"MZ not a zip")
    with pytest.raises(CommandError):
        call_command("publish_mobile_app", str(zipless), version_name="1.0.0", version_code=1)
