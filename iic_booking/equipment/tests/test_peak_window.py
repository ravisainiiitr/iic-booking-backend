"""Peak booking window: computation, external-user pause, status/admin API, task deferral."""

from __future__ import annotations

from datetime import datetime, time, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.equipment import peak_window
from iic_booking.equipment.models import PeakWindowSetting
from iic_booking.equipment.peak_window import (
    PEAK_EXTERNAL_PAUSED_CODE,
    PeakSettings,
    compute_peak_state,
    defer_during_peak,
    external_paused_message,
    get_opening_schedules,
    get_peak_settings,
    invalidate_peak_window_cache,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType

IST = ZoneInfo("Asia/Kolkata")
WEDNESDAY_9PM = ((2, time(21, 0)),)
DEFAULTS = PeakSettings()
PASSWORD = "Known-pass-7731"
EXPECTED_MESSAGE = (
    "To give IIT Roorkee users a fair chance when new slots open, external access is paused "
    "from 8:55 pm to 9:15 pm on Wednesdays. Please come back after 9:15 pm."
)


def ist(*parts):
    return datetime(*parts, tzinfo=IST)


# --------------------------------------------------------------------------------------
# Pure computation
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "at, active",
    [
        (ist(2026, 10, 7, 20, 54, 59), False),
        (ist(2026, 10, 7, 20, 55), True),
        (ist(2026, 10, 7, 21, 0), True),
        (ist(2026, 10, 7, 21, 14, 59), True),
        (ist(2026, 10, 7, 21, 15), False),
        (ist(2026, 10, 8, 20, 58), False),
        (ist(2026, 10, 14, 21, 5), True),
    ],
)
def test_window_edges_minus5_plus15(at, active):
    state = compute_peak_state(at, schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert state["peak_window_active"] is active
    assert state["external_access_paused"] is active


def test_window_is_computed_in_ist_for_utc_input():
    at_utc = datetime(2026, 10, 7, 15, 30, tzinfo=dt_timezone.utc)  # 21:00 IST
    state = compute_peak_state(at_utc, schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert state["peak_window_active"]
    assert state["starts_at"] == "2026-10-07T20:55:00+05:30"
    assert state["ends_at"] == "2026-10-07T21:15:00+05:30"
    assert state["opening_at"] == "2026-10-07T21:00:00+05:30"


def test_next_window_and_external_notice():
    friday = compute_peak_state(ist(2026, 10, 2, 14, 0), schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert not friday["peak_window_active"]
    assert friday["next_window"]["starts_at"] == "2026-10-07T20:55:00+05:30"
    assert friday["next_window"]["ends_at"] == "2026-10-07T21:15:00+05:30"
    assert not friday["external_notice_active"]

    before_notice = compute_peak_state(ist(2026, 10, 7, 20, 24, 59), schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert not before_notice["external_notice_active"]
    notice = compute_peak_state(ist(2026, 10, 7, 20, 25), schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert notice["external_notice_active"]
    assert not notice["peak_window_active"]
    assert notice["external_notice_starts_at"] == "2026-10-07T20:25:00+05:30"

    during = compute_peak_state(ist(2026, 10, 7, 21, 1), schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert not during["external_notice_active"]
    assert during["next_window"]["starts_at"] == "2026-10-14T20:55:00+05:30"


def test_per_equipment_schedules():
    schedules = ((2, time(21, 0)), (4, time(10, 0)))
    assert compute_peak_state(ist(2026, 10, 9, 9, 56), schedules=schedules, settings=DEFAULTS)["peak_window_active"]
    assert compute_peak_state(ist(2026, 10, 7, 21, 10), schedules=schedules, settings=DEFAULTS)["peak_window_active"]
    assert not compute_peak_state(ist(2026, 10, 9, 21, 0), schedules=schedules, settings=DEFAULTS)["peak_window_active"]


def test_overlapping_windows_merge():
    schedules = ((2, time(21, 0)), (2, time(21, 10)))
    state = compute_peak_state(ist(2026, 10, 7, 21, 20), schedules=schedules, settings=DEFAULTS)
    assert state["peak_window_active"]
    assert state["starts_at"] == "2026-10-07T20:55:00+05:30"
    assert state["ends_at"] == "2026-10-07T21:25:00+05:30"


def test_custom_lead_trail_and_disabled():
    wide = PeakSettings(lead_minutes=10, trail_minutes=30)
    assert compute_peak_state(ist(2026, 10, 7, 20, 50), schedules=WEDNESDAY_9PM, settings=wide)["peak_window_active"]
    assert compute_peak_state(ist(2026, 10, 7, 21, 29), schedules=WEDNESDAY_9PM, settings=wide)["peak_window_active"]
    off = PeakSettings(enabled=False)
    assert not compute_peak_state(ist(2026, 10, 7, 21, 0), schedules=WEDNESDAY_9PM, settings=off)["peak_window_active"]
    no_block = PeakSettings(block_external_users=False)
    state = compute_peak_state(ist(2026, 10, 7, 21, 0), schedules=WEDNESDAY_9PM, settings=no_block)
    assert state["peak_window_active"] and not state["external_access_paused"]


def test_paused_message_uses_actual_times():
    state = compute_peak_state(ist(2026, 10, 7, 21, 0), schedules=WEDNESDAY_9PM, settings=DEFAULTS)
    assert external_paused_message(state["_current"]) == EXPECTED_MESSAGE


# --------------------------------------------------------------------------------------
# Schedules / settings from the database, cache invalidation
# --------------------------------------------------------------------------------------

@pytest.fixture
def clean_peak_cache():
    cache.clear()
    invalidate_peak_window_cache()
    yield
    invalidate_peak_window_cache()


@pytest.mark.django_db
def test_schedules_come_from_active_equipment(egs_factory, clean_peak_cache):
    egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    egs_factory.equipment(status="REPAIR", slot_window_reference_weekday=4, slot_window_reference_time=time(9, 0))
    assert get_opening_schedules() == ((2, time(21, 0)),)


@pytest.mark.django_db
def test_schedule_cache_invalidated_on_equipment_edit(egs_factory, clean_peak_cache):
    eq = egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    assert get_opening_schedules() == ((2, time(21, 0)),)
    eq.slot_window_reference_time = time(18, 30)
    eq.save()
    assert get_opening_schedules() == ((2, time(18, 30)),)


@pytest.mark.django_db
def test_settings_default_on_and_cache_invalidated_on_edit(clean_peak_cache):
    assert get_peak_settings() == PeakSettings()
    row = PeakWindowSetting.objects.create()
    assert (row.enabled, row.lead_minutes, row.trail_minutes, row.block_external_users) == (True, 5, 15, True)
    row.lead_minutes = 10
    row.save()
    assert get_peak_settings().lead_minutes == 10


# --------------------------------------------------------------------------------------
# External pause (login + API), status endpoint, admin API
# --------------------------------------------------------------------------------------

def _user(user_type, *, is_staff=False):
    user = User.objects.create_user(
        email=f"{user_type.lower()}.{User.objects.count()}@peak.test",
        password=PASSWORD,
        name="Peak User",
        user_type=user_type,
    )
    user.email_verified = True
    user.admin_approved = True
    user.is_staff = is_staff
    user.last_login = timezone.now()
    user.save()
    return user


def _token_client(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
    return client


@pytest.fixture
def at_peak(egs_factory, clean_peak_cache, monkeypatch):
    egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    monkeypatch.setattr(peak_window, "_now", lambda: ist(2026, 10, 7, 21, 2))
    return egs_factory


@pytest.fixture
def off_peak(egs_factory, clean_peak_cache, monkeypatch):
    egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    monkeypatch.setattr(peak_window, "_now", lambda: ist(2026, 10, 7, 21, 15))
    return egs_factory


@pytest.mark.django_db
@pytest.mark.parametrize("user_type", sorted(UserType.get_external_user_codes()))
def test_login_refused_for_external_users_during_peak(at_peak, user_type):
    user = _user(user_type)
    resp = APIClient().post("/api/auth/login/", {"email": user.email, "password": PASSWORD}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == PEAK_EXTERNAL_PAUSED_CODE
    assert resp.data["message"] == EXPECTED_MESSAGE
    assert not Token.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_login_otp_refused_for_external_during_peak(at_peak, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    user = _user(UserType.INSTITUTE)
    resp = APIClient().post("/api/auth/login/request-otp/", {"email": user.email}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == PEAK_EXTERNAL_PAUSED_CODE


@pytest.mark.django_db
def test_login_allowed_for_external_off_peak_and_for_staff_during_peak(off_peak, monkeypatch):
    external = _user(UserType.EXTERNAL)
    resp = APIClient().post("/api/auth/login/", {"email": external.email, "password": PASSWORD}, format="json")
    assert resp.status_code == 200, resp.data

    monkeypatch.setattr(peak_window, "_now", lambda: ist(2026, 10, 7, 21, 2))
    admin = _user(UserType.ADMIN)
    resp = APIClient().post("/api/auth/login/", {"email": admin.email, "password": PASSWORD}, format="json")
    assert resp.status_code == 200, resp.data


@pytest.mark.django_db
def test_api_403_for_signed_in_external_user_during_peak(at_peak):
    client = _token_client(_user(UserType.RND))
    resp = client.get("/api/equipments/")
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == PEAK_EXTERNAL_PAUSED_CODE
    assert body["detail"] == EXPECTED_MESSAGE
    assert body["peak_window"]["ends_at"] == "2026-10-07T21:15:00+05:30"


@pytest.mark.django_db
def test_exempt_endpoints_stay_reachable_for_paused_user(at_peak):
    client = _token_client(_user(UserType.EXTERNAL))
    assert client.get("/api/auth/user/").status_code == 200
    assert client.get("/api/profiles/me/").status_code != 403
    assert client.get("/api/peak-window/status/").status_code == 200
    assert client.patch("/api/profiles/me/", {"name": "x"}, format="json").status_code == 403
    assert client.post("/api/auth/logout/").status_code != 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    "user_type, is_staff",
    [
        (UserType.STUDENT, False),
        (UserType.FACULTY, False),
        (UserType.ADMIN, False),
        (UserType.MANAGER, False),
        (UserType.OPERATOR, False),
        (UserType.EXTERNAL, True),
    ],
)
def test_internal_and_staff_users_never_blocked(at_peak, user_type, is_staff):
    client = _token_client(_user(user_type, is_staff=is_staff))
    assert client.get("/api/equipments/").status_code == 200


@pytest.mark.django_db
def test_external_not_blocked_off_peak_or_when_switched_off(off_peak, monkeypatch):
    client = _token_client(_user(UserType.EXTERNAL))
    assert client.get("/api/equipments/").status_code == 200

    monkeypatch.setattr(peak_window, "_now", lambda: ist(2026, 10, 7, 21, 2))
    PeakWindowSetting.objects.create(block_external_users=False)
    assert client.get("/api/equipments/").status_code == 200


@pytest.mark.django_db
def test_status_endpoint_is_public_and_cacheable(at_peak):
    resp = APIClient().get("/api/peak-window/status/")
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "public, max-age=5"
    data = resp.json()
    assert data["peak_window_active"] is True
    assert data["external_access_paused"] is True
    assert data["starts_at"] == "2026-10-07T20:55:00+05:30"
    assert data["ends_at"] == "2026-10-07T21:15:00+05:30"
    assert data["next_window"]["starts_at"] == "2026-10-14T20:55:00+05:30"
    assert data["external_paused_message"] == EXPECTED_MESSAGE


@pytest.mark.django_db
def test_admin_settings_api_main_admin_only_and_invalidates(off_peak):
    student = _token_client(_user(UserType.STUDENT))
    assert student.get("/api/admin/peak-window-settings/").status_code == 403

    admin = APIClient()
    admin.force_authenticate(_user(UserType.ADMIN))
    got = admin.get("/api/admin/peak-window-settings/")
    assert got.status_code == 200
    assert got.data["enabled"] is True and got.data["lead_minutes"] == 5 and got.data["trail_minutes"] == 15

    assert admin.patch("/api/admin/peak-window-settings/", {"lead_minutes": 500}, format="json").status_code == 400
    resp = admin.patch("/api/admin/peak-window-settings/", {"trail_minutes": 30}, format="json")
    assert resp.status_code == 200
    # 21:15 is now inside the (20:55, 21:30) window.
    assert APIClient().get("/api/peak-window/status/").json()["peak_window_active"] is True


# --------------------------------------------------------------------------------------
# Background task deferral
# --------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_defer_during_peak_requeues_once(at_peak, monkeypatch):
    from celery import current_app

    sent = []
    monkeypatch.setattr(
        current_app,
        "send_task",
        lambda name, args=None, kwargs=None, countdown=None: sent.append((name, args, kwargs, countdown)),
    )
    calls = []

    @defer_during_peak("tests.peak_task")
    def body(a, b=1):
        calls.append((a, b))
        return "ran"

    out = body(5, b=2)
    assert out["deferred"] == "peak_window"
    assert calls == []
    name, args, kwargs, countdown = sent[0]
    assert (name, args, kwargs) == ("tests.peak_task", [5], {"b": 2})
    assert 13 * 60 - 1 <= countdown <= 13 * 60 + 120
    body(5, b=2)
    assert len(sent) == 1


@pytest.mark.django_db
def test_defer_during_peak_runs_normally_off_peak(off_peak):
    @defer_during_peak("tests.peak_task")
    def body():
        return "ran"

    assert body() == "ran"
