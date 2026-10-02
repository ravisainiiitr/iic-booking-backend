"""Mobile app device sessions: enroll, refresh rotation, revocation, and coexistence with the web Token."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib import admin
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.communication.consumers import NotificationConsumer
from iic_booking.users.api.auth_views import _regenerate_auth_token
from iic_booking.users.api.mobile_session_views import (
    MobileRefreshDeviceThrottle,
    MobileRefreshIPThrottle,
    mobile_refresh,
)
from iic_booking.users.api.token_auth import resolve_request_user
from iic_booking.users.mobile_sessions import hash_token
from iic_booking.users.models import MobileDeviceSession, User, UserType

pytestmark = pytest.mark.django_db

ENROLL_URL = "/api/auth/mobile/enroll/"
REFRESH_URL = "/api/auth/mobile/refresh/"
MOBILE_LOGOUT_URL = "/api/auth/mobile/logout/"
DEVICES_URL = "/api/auth/mobile/devices/"
REVOKE_ALL_URL = "/api/auth/mobile/devices/revoke-all/"
LOGOUT_URL = "/api/auth/logout/"
USER_URL = "/api/auth/user/"
PASSWORD_URL = "/api/auth/password/"
FORGOT_OTP_URL = "/api/auth/forgot-password/request-otp/"
FORGOT_SET_URL = "/api/auth/forgot-password/verify-otp-and-set-password/"

PASSWORD = "Known-pass-7731!"
NEW_PASSWORD = "Rk7!quartz-lattice"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def make_user(email="app.user@example.com", user_type=UserType.EXTERNAL, **extra) -> User:
    user = User.objects.create_user(email=email, password=PASSWORD, name="App User", user_type=user_type, **extra)
    user.email_verified = True
    user.admin_approved = True
    user.last_login = timezone.now()
    user.save()
    return user


def web_client(user) -> tuple[APIClient, Token]:
    token = _regenerate_auth_token(user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client, token


def token_client(key: str) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {key}")
    return client


def enroll(user, device_id="device-abc-123", platform="android", **body):
    client, _ = web_client(user)
    resp = client.post(
        ENROLL_URL, {"device_id": device_id, "platform": platform, "device_name": "Pixel 8", **body}, format="json"
    )
    assert resp.status_code == 201, resp.data
    return resp.data


def refresh(device_id, refresh_token):
    return APIClient().post(REFRESH_URL, {"device_id": device_id, "refresh_token": refresh_token}, format="json")


# --- enroll -----------------------------------------------------------------------------------


def test_enroll_returns_tokens_and_stores_only_hashes():
    user = make_user()
    client, web_token = web_client(user)
    resp = client.post(
        ENROLL_URL,
        {"device_id": "device-abc-123", "device_name": "  Pixel 8  ", "platform": "android", "app_version": "1.0.3"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp["Cache-Control"] == "no-store"
    data = resp.data
    assert set(data) == {
        "device_session_id",
        "access_token",
        "access_expires_at",
        "refresh_token",
        "refresh_expires_at",
        "require_biometric",
    }
    assert data["access_token"].startswith("iicm_")
    assert data["refresh_token"].startswith("iicr_")
    assert data["require_biometric"] is False

    session = MobileDeviceSession.objects.get(pk=data["device_session_id"])
    assert session.user == user
    assert session.device_name == "Pixel 8"
    assert session.platform == "android"
    assert session.app_version == "1.0.3"
    assert session.access_hash == hash_token(data["access_token"])
    assert session.refresh_hash == hash_token(data["refresh_token"])
    assert session.access_expires_at.isoformat() == data["access_expires_at"]
    stored = [str(getattr(session, f.attname)) for f in MobileDeviceSession._meta.concrete_fields]
    assert not any(data["access_token"] in v or data["refresh_token"] in v for v in stored)

    # The web Token is untouched by enrolment.
    assert Token.objects.get(user=user).key == web_token.key


def test_enroll_rejected_with_mobile_access_token():
    user = make_user()
    data = enroll(user)
    resp = token_client(data["access_token"]).post(
        ENROLL_URL, {"device_id": "device-other-1", "platform": "ios"}, format="json"
    )
    assert resp.status_code == 403
    assert resp.data["code"] == "ENROLL_REQUIRES_LOGIN"


def test_enroll_rejected_when_disabled(settings):
    settings.MOBILE_DEVICE_SESSIONS_ENABLED = False
    client, _ = web_client(make_user())
    resp = client.post(ENROLL_URL, {"device_id": "device-abc-123", "platform": "android"}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == "MOBILE_SESSIONS_DISABLED"
    assert not MobileDeviceSession.objects.exists()


def test_enroll_rejected_when_web_login_too_old(settings):
    settings.MOBILE_ENROLL_MAX_TOKEN_AGE_HOURS = 12
    user = make_user()
    client, token = web_client(user)
    Token.objects.filter(pk=token.pk).update(created=timezone.now() - timedelta(hours=13))
    resp = client.post(ENROLL_URL, {"device_id": "device-abc-123", "platform": "android"}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == "LOGIN_TOO_OLD"


@pytest.mark.parametrize(
    "body",
    [
        {"device_id": "short", "platform": "android"},
        {"device_id": "bad id with spaces", "platform": "android"},
        {"device_id": "device-abc-123", "platform": "windows"},
        {"device_id": "device-abc-123", "platform": "ios", "device_name": "x" * 101},
        {"device_id": "device-abc-123", "platform": "ios", "app_version": "1" * 33},
    ],
)
def test_enroll_validates_body(body):
    client, _ = web_client(make_user())
    resp = client.post(ENROLL_URL, body, format="json")
    assert resp.status_code == 400
    assert resp.data["code"] == "INVALID_REQUEST"


def test_re_enroll_same_device_revokes_previous_session():
    user = make_user()
    first = enroll(user)
    second = enroll(user)
    old = MobileDeviceSession.objects.get(pk=first["device_session_id"])
    assert old.revoked_at is not None and old.revoke_reason == "re_enrolled"
    assert token_client(first["access_token"]).get(USER_URL).status_code == 401
    assert token_client(second["access_token"]).get(USER_URL).status_code == 200


def test_device_limit_revokes_least_recently_used(settings):
    settings.MOBILE_SESSION_MAX_DEVICES = 2
    user = make_user()
    a = enroll(user, device_id="device-aaa-111")
    b = enroll(user, device_id="device-bbb-222")
    # "a" was used more recently than "b", so "b" is the one to go.
    MobileDeviceSession.objects.filter(pk=a["device_session_id"]).update(last_used_at=timezone.now() + timedelta(seconds=5))
    c = enroll(user, device_id="device-ccc-333")

    sessions = {s.pk: s for s in MobileDeviceSession.objects.filter(user=user)}
    assert sessions[b["device_session_id"]].revoke_reason == "device_limit"
    assert sessions[a["device_session_id"]].revoked_at is None
    assert sessions[c["device_session_id"]].revoked_at is None


def test_admin_role_gets_biometric_and_short_lifetimes(settings):
    settings.MOBILE_ADMIN_REFRESH_TOKEN_LIFETIME_DAYS = 14
    settings.MOBILE_ADMIN_SESSION_ABSOLUTE_MAX_DAYS = 30
    for i, extra in enumerate(({"is_staff": True}, {"user_type": UserType.ADMIN}, {"is_superuser": True})):
        kwargs = {"user_type": UserType.EXTERNAL, **extra}
        user = make_user(email=f"admin{i}@example.com", **kwargs)
        data = enroll(user)
        session = MobileDeviceSession.objects.get(pk=data["device_session_id"])
        assert data["require_biometric"] is True
        assert abs((session.refresh_expires_at - session.created_at) - timedelta(days=14)) < timedelta(minutes=1)
        assert abs((session.absolute_expires_at - session.created_at) - timedelta(days=30)) < timedelta(minutes=1)


def test_regular_user_gets_default_lifetimes(settings):
    settings.MOBILE_REFRESH_TOKEN_LIFETIME_DAYS = 60
    settings.MOBILE_SESSION_ABSOLUTE_MAX_DAYS = 180
    settings.MOBILE_ACCESS_TOKEN_LIFETIME_HOURS = 24
    session = MobileDeviceSession.objects.get(pk=enroll(make_user(user_type=UserType.MANAGER))["device_session_id"])
    assert session.require_biometric is False
    assert abs((session.refresh_expires_at - session.created_at) - timedelta(days=60)) < timedelta(minutes=1)
    assert abs((session.absolute_expires_at - session.created_at) - timedelta(days=180)) < timedelta(minutes=1)
    assert abs((session.access_expires_at - session.created_at) - timedelta(hours=24)) < timedelta(minutes=1)


# --- authentication ---------------------------------------------------------------------------


def test_access_token_authenticates_api():
    user = make_user()
    data = enroll(user)
    resp = token_client(data["access_token"]).get(USER_URL)
    assert resp.status_code == 200
    assert resp.data["email"] == user.email


def test_unknown_mobile_key_is_rejected():
    assert token_client("iicm_not-a-real-token").get(USER_URL).status_code == 401


def test_web_login_elsewhere_does_not_invalidate_mobile_session():
    user = make_user()
    data = enroll(user)
    old_web = Token.objects.get(user=user)
    new_web = _regenerate_auth_token(user)

    assert token_client(old_web.key).get(USER_URL).status_code == 401
    assert token_client(new_web.key).get(USER_URL).status_code == 200
    assert token_client(data["access_token"]).get(USER_URL).status_code == 200


def test_last_used_is_updated_at_most_every_five_minutes():
    user = make_user()
    data = enroll(user)
    session = MobileDeviceSession.objects.get(pk=data["device_session_id"])
    recent = timezone.now() - timedelta(minutes=1)
    MobileDeviceSession.objects.filter(pk=session.pk).update(last_used_at=recent)
    token_client(data["access_token"]).get(USER_URL)
    session.refresh_from_db()
    assert session.last_used_at == recent

    stale = timezone.now() - timedelta(minutes=10)
    MobileDeviceSession.objects.filter(pk=session.pk).update(last_used_at=stale)
    token_client(data["access_token"]).get(USER_URL, REMOTE_ADDR="10.1.2.3")
    session.refresh_from_db()
    assert session.last_used_at > stale
    assert session.last_ip == "10.1.2.3"


def test_resolve_request_user_accepts_mobile_key_in_query_and_header():
    user = make_user()
    data = enroll(user)
    rf = RequestFactory()
    assert resolve_request_user(rf.get("/media/x", {"token": data["access_token"]})) == user
    assert resolve_request_user(rf.get("/media/x", HTTP_AUTHORIZATION=f"Token {data['access_token']}")) == user
    assert resolve_request_user(rf.get("/media/x", {"token": "iicm_bogus"})) is None


def test_websocket_authenticate_user_accepts_mobile_key():
    user = make_user()
    data = enroll(user)
    authenticate = NotificationConsumer.__dict__["authenticate_user"].func
    assert authenticate(None, data["access_token"]) == user
    assert authenticate(None, "iicm_bogus") is None
    assert authenticate(None, Token.objects.get(user=user).key) == user


def test_peak_window_middleware_resolves_mobile_key():
    from iic_booking.equipment import peak_window_middleware as pwm

    pwm._token_blockable.clear()
    external = make_user(email="ext.peak@example.com", user_type=UserType.EXTERNAL)
    staff = make_user(email="staff.peak@example.com", user_type=UserType.EXTERNAL, is_staff=True)
    assert pwm._blockable_from_token(enroll(external)["access_token"]) is True
    assert pwm._blockable_from_token(enroll(staff)["access_token"]) is False
    pwm._token_blockable.clear()


# --- refresh ----------------------------------------------------------------------------------


def test_refresh_rotates_and_old_access_has_short_grace():
    user = make_user()
    first = enroll(user)
    resp = refresh("device-abc-123", first["refresh_token"])
    assert resp.status_code == 200, resp.data
    assert resp["Cache-Control"] == "no-store"
    second = resp.data
    assert second["device_session_id"] == first["device_session_id"]
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]

    session = MobileDeviceSession.objects.get(pk=first["device_session_id"])
    assert session.prev_refresh_hash == hash_token(first["refresh_token"])
    assert session.prev_access_hash == hash_token(first["access_token"])
    assert session.refreshed_at is not None

    # Old access still works during the grace window, new one works too.
    assert token_client(first["access_token"]).get(USER_URL).status_code == 200
    assert token_client(second["access_token"]).get(USER_URL).status_code == 200

    MobileDeviceSession.objects.filter(pk=session.pk).update(prev_access_valid_until=timezone.now() - timedelta(seconds=1))
    assert token_client(first["access_token"]).get(USER_URL).status_code == 401
    assert token_client(second["access_token"]).get(USER_URL).status_code == 200


def test_old_refresh_within_grace_is_treated_as_retry():
    user = make_user()
    first = enroll(user)
    second = refresh("device-abc-123", first["refresh_token"]).data
    retry = refresh("device-abc-123", first["refresh_token"])
    assert retry.status_code == 200, retry.data
    third = retry.data
    assert third["refresh_token"] not in (first["refresh_token"], second["refresh_token"])
    assert token_client(third["access_token"]).get(USER_URL).status_code == 200
    # The very first refresh token is now two generations old and unknown.
    assert refresh("device-abc-123", first["refresh_token"]).data["code"] == "INVALID_REFRESH"


def test_old_refresh_after_grace_revokes_session(settings):
    settings.MOBILE_REFRESH_REUSE_GRACE_SECONDS = 60
    user = make_user()
    first = enroll(user)
    second = refresh("device-abc-123", first["refresh_token"]).data
    MobileDeviceSession.objects.filter(pk=first["device_session_id"]).update(
        refreshed_at=timezone.now() - timedelta(seconds=61)
    )

    resp = refresh("device-abc-123", first["refresh_token"])
    assert resp.status_code == 401
    assert resp.data["code"] == "REFRESH_REUSED"
    session = MobileDeviceSession.objects.get(pk=first["device_session_id"])
    assert session.revoked_at is not None and session.revoke_reason == "refresh_reuse"

    assert refresh("device-abc-123", second["refresh_token"]).data["code"] == "SESSION_REVOKED"
    assert token_client(second["access_token"]).get(USER_URL).status_code == 401


@pytest.mark.parametrize("field", ["refresh_expires_at", "absolute_expires_at"])
def test_refresh_rejects_expired_session(field):
    user = make_user()
    data = enroll(user)
    MobileDeviceSession.objects.filter(pk=data["device_session_id"]).update(**{field: timezone.now() - timedelta(seconds=1)})
    resp = refresh("device-abc-123", data["refresh_token"])
    assert resp.status_code == 401
    assert resp.data["code"] == "SESSION_EXPIRED"
    assert token_client(data["access_token"]).get(USER_URL).status_code == 401


def test_refresh_rejects_revoked_session():
    user = make_user()
    data = enroll(user)
    MobileDeviceSession.objects.filter(pk=data["device_session_id"]).update(revoked_at=timezone.now())
    resp = refresh("device-abc-123", data["refresh_token"])
    assert resp.status_code == 401
    assert resp.data["code"] == "SESSION_REVOKED"


def test_refresh_rejects_inactive_user():
    user = make_user()
    data = enroll(user)
    User.objects.filter(pk=user.pk).update(is_active=False)
    resp = refresh("device-abc-123", data["refresh_token"])
    assert resp.status_code == 401
    assert resp.data["code"] == "SESSION_REVOKED"
    assert token_client(data["access_token"]).get(USER_URL).status_code == 401


def test_refresh_rejects_device_mismatch_and_garbage():
    user = make_user()
    data = enroll(user)
    assert refresh("device-zzz-999", data["refresh_token"]).data["code"] == "INVALID_REFRESH"
    assert refresh("device-abc-123", "iicr_unknown").data["code"] == "INVALID_REFRESH"
    assert refresh("device-abc-123", data["access_token"]).data["code"] == "INVALID_REFRESH"
    assert refresh("", "").status_code == 401
    # A mismatched device id must not burn the token.
    assert refresh("device-abc-123", data["refresh_token"]).status_code == 200


def test_admin_refresh_is_capped_by_absolute_expiry(settings):
    settings.MOBILE_ACCESS_TOKEN_LIFETIME_HOURS = 24
    user = make_user(is_staff=True)
    data = enroll(user)
    soon = timezone.now() + timedelta(hours=2)
    MobileDeviceSession.objects.filter(pk=data["device_session_id"]).update(absolute_expires_at=soon)
    resp = refresh("device-abc-123", data["refresh_token"])
    assert resp.status_code == 200
    session = MobileDeviceSession.objects.get(pk=data["device_session_id"])
    assert session.access_expires_at == soon
    assert session.refresh_expires_at == soon
    assert resp.data["require_biometric"] is True


def test_refresh_throttles_are_wired(monkeypatch):
    assert mobile_refresh.cls.throttle_classes == [MobileRefreshDeviceThrottle, MobileRefreshIPThrottle]
    rates = {"mobile_refresh_device": "2/hour", "mobile_refresh_ip": "1000/hour"}
    monkeypatch.setattr(MobileRefreshDeviceThrottle, "THROTTLE_RATES", rates)
    monkeypatch.setattr(MobileRefreshIPThrottle, "THROTTLE_RATES", rates)
    for _ in range(2):
        assert refresh("device-thr-001", "iicr_x").status_code == 401
    assert refresh("device-thr-001", "iicr_x").status_code == 429
    # Another device from the same IP is not affected by the per-device budget.
    assert refresh("device-thr-002", "iicr_x").status_code == 401


# --- logout and revocation --------------------------------------------------------------------


def test_web_logout_endpoint_with_mobile_token_revokes_only_that_session():
    user = make_user()
    data = enroll(user)
    other = enroll(user, device_id="device-other-22")
    web_key = Token.objects.get(user=user).key

    resp = token_client(data["access_token"]).post(LOGOUT_URL)
    assert resp.status_code == 200
    assert resp.data["message"] == "Successfully logged out"

    assert MobileDeviceSession.objects.get(pk=data["device_session_id"]).revoke_reason == "logout"
    assert MobileDeviceSession.objects.get(pk=other["device_session_id"]).revoked_at is None
    assert Token.objects.filter(key=web_key).exists()
    assert token_client(data["access_token"]).get(USER_URL).status_code == 401
    assert token_client(web_key).get(USER_URL).status_code == 200


def test_web_logout_with_web_token_keeps_mobile_sessions():
    user = make_user()
    data = enroll(user)
    client, _ = web_client(user)
    assert client.post(LOGOUT_URL).status_code == 200
    assert not Token.objects.filter(user=user).exists()
    assert token_client(data["access_token"]).get(USER_URL).status_code == 200


def test_mobile_logout_endpoint():
    user = make_user()
    data = enroll(user)
    resp = token_client(data["access_token"]).post(MOBILE_LOGOUT_URL)
    assert resp.status_code == 200 and "message" in resp.data
    assert MobileDeviceSession.objects.get(pk=data["device_session_id"]).revoke_reason == "logout"

    client, _ = web_client(user)
    assert client.post(MOBILE_LOGOUT_URL).status_code == 200
    assert Token.objects.filter(user=user).exists()


def test_devices_list_marks_current():
    user = make_user()
    a = enroll(user, device_id="device-aaa-111")
    b = enroll(user, device_id="device-bbb-222", platform="ios")
    MobileDeviceSession.objects.filter(pk=a["device_session_id"]).update(last_used_at=timezone.now() + timedelta(seconds=5))

    resp = token_client(b["access_token"]).get(DEVICES_URL)
    assert resp.status_code == 200
    results = resp.data["results"]
    assert [r["id"] for r in results] == [a["device_session_id"], b["device_session_id"]]
    assert set(results[0]) == {
        "id",
        "device_name",
        "platform",
        "app_version",
        "created_at",
        "last_used_at",
        "refresh_expires_at",
        "require_biometric",
        "is_current",
    }
    assert [r["is_current"] for r in results] == [False, True]

    web, _ = web_client(user)
    assert not any(r["is_current"] for r in web.get(DEVICES_URL).data["results"])


def test_revoke_own_device_and_not_someone_elses():
    user = make_user()
    stranger = make_user(email="stranger@example.com")
    mine = enroll(user)
    theirs = enroll(stranger)
    client, _ = web_client(user)

    url = f"/api/auth/mobile/devices/{theirs['device_session_id']}/revoke/"
    assert client.post(url).status_code == 404
    assert MobileDeviceSession.objects.get(pk=theirs["device_session_id"]).revoked_at is None

    url = f"/api/auth/mobile/devices/{mine['device_session_id']}/revoke/"
    assert client.post(url).status_code == 200
    assert MobileDeviceSession.objects.get(pk=mine["device_session_id"]).revoke_reason == "user_revoked"
    assert client.post(url).status_code == 404


def test_revoke_all_keeps_current_session():
    user = make_user()
    a = enroll(user, device_id="device-aaa-111")
    b = enroll(user, device_id="device-bbb-222")
    c = enroll(user, device_id="device-ccc-333")
    resp = token_client(b["access_token"]).post(REVOKE_ALL_URL)
    assert resp.status_code == 200
    assert resp.data["revoked"] == 2
    reasons = dict(MobileDeviceSession.objects.filter(user=user).values_list("pk", "revoke_reason"))
    assert reasons[a["device_session_id"]] == "user_revoked_all"
    assert reasons[c["device_session_id"]] == "user_revoked_all"
    assert reasons[b["device_session_id"]] == ""


def test_password_change_revokes_other_sessions_but_keeps_current_mobile():
    user = make_user()
    current = enroll(user, device_id="device-aaa-111")
    other = enroll(user, device_id="device-bbb-222")
    resp = token_client(current["access_token"]).post(
        PASSWORD_URL,
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert MobileDeviceSession.objects.get(pk=other["device_session_id"]).revoke_reason == "password_changed"
    assert MobileDeviceSession.objects.get(pk=current["device_session_id"]).revoked_at is None


def test_password_change_from_web_revokes_all_mobile_sessions():
    user = make_user()
    data = enroll(user)
    client, _ = web_client(user)
    resp = client.post(
        PASSWORD_URL,
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert MobileDeviceSession.objects.get(pk=data["device_session_id"]).revoke_reason == "password_changed"


def test_forgot_password_reset_revokes_all_mobile_sessions(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    user = make_user()
    data = enroll(user)
    client = APIClient()
    assert client.post(FORGOT_OTP_URL, {"email": user.email}, format="json").status_code == 200
    otp = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
    resp = client.post(
        FORGOT_SET_URL,
        {"email": user.email, "otp": otp, "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert MobileDeviceSession.objects.get(pk=data["device_session_id"]).revoke_reason == "password_changed"
    assert token_client(data["access_token"]).get(USER_URL).status_code == 401


def test_admin_force_logout_revokes_mobile_sessions(monkeypatch):
    user = make_user()
    data = enroll(user)
    model_admin = admin.site._registry[User]
    monkeypatch.setattr(model_admin, "message_user", lambda *a, **k: None)
    model_admin.force_logout_users(RequestFactory().post("/admin/"), User.objects.filter(pk=user.pk))
    assert not Token.objects.filter(user=user).exists()
    assert MobileDeviceSession.objects.get(pk=data["device_session_id"]).revoke_reason == "admin_force_logout"


def test_admin_revoke_selected_sessions_action(monkeypatch):
    user = make_user()
    data = enroll(user)
    model_admin = admin.site._registry[MobileDeviceSession]
    monkeypatch.setattr(model_admin, "message_user", lambda *a, **k: None)
    model_admin.revoke_selected_sessions(RequestFactory().post("/admin/"), MobileDeviceSession.objects.all())
    assert MobileDeviceSession.objects.get(pk=data["device_session_id"]).revoke_reason == "admin_revoked"
    assert "access_hash" not in model_admin.fields and "refresh_hash" not in model_admin.fields
