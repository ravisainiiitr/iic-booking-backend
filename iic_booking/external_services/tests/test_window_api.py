"""Central external booking window: Main Administrator API, audit, booking guards and the preparation task."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.external_services.models import (
    ExternalBookingWindowChange,
    ExternalBookingWindowConfig,
    ExternalBookingWindowOpening,
)
from iic_booking.procurement_management.models import ImmutableRecordError
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, enable_window, main_admin, make_user, monday

pytestmark = pytest.mark.django_db
ADMIN = "/api/v1/admin/external-booking/window/"
REASON = "Initial external booking schedule approved by IIC"


def _configure_payload(**extra):
    first = monday(timezone.localdate()) + timedelta(days=7)
    body = {
        "initial_open_at": (timezone.localtime() - timedelta(days=1)).replace(microsecond=0).isoformat(),
        "first_week_start": first.isoformat(),
        "initial_weeks": 4,
        "enabled": True,
        "reason": REASON,
    }
    body.update(extra)
    return body


# --- permissions ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("user_type", [UserType.EXTERNAL, UserType.STUDENT, UserType.MANAGER, UserType.DEPT_ADMIN,
                                       UserType.FINANCE, UserType.OPERATOR])
def test_only_the_main_administrator_may_read_or_change_the_window(user_type):
    user = make_user(user_type)
    client = client_for(user)
    assert client.get(ADMIN).status_code == 403
    assert client.patch(ADMIN, _configure_payload(), format="json").status_code == 403
    for action in ("pause/", "resume/", "advance/", "preview/"):
        assert client.post(ADMIN + action, {"reason": REASON, "weeks": 1}, format="json").status_code == 403
    assert not ExternalBookingWindowChange.objects.exists()
    assert not ExternalBookingWindowConfig.load().enabled


def test_anonymous_users_are_refused():
    assert client_for().get("/api/v1/external-booking/window/").status_code in (401, 403)
    assert client_for().patch(ADMIN, _configure_payload(), format="json").status_code in (401, 403)


def test_disabled_by_default():
    body = client_for(make_user()).get("/api/v1/external-booking/window/").json()
    assert body["enabled"] is False and body["applies_to_you"] is False


# --- configuration and audit ---------------------------------------------------------------------------------------

def test_main_admin_configures_and_every_change_is_audited():
    admin = main_admin()
    r = client_for(admin).patch(ADMIN, _configure_payload(), format="json")
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["config"]["enabled"] is True
    assert data["window"]["is_open"] is True
    first = monday(timezone.localdate()) + timedelta(days=7)
    assert data["window"]["last_date"] == (first + timedelta(days=27)).isoformat()
    change = ExternalBookingWindowChange.objects.get()
    assert change.action == "enable" and change.actor == admin and change.reason == REASON
    assert change.range_before["enabled"] is False and change.range_after["is_open"] is True
    with pytest.raises(ImmutableRecordError):
        change.reason = "edited"
        change.save()
    with pytest.raises(ImmutableRecordError):
        ExternalBookingWindowChange.objects.all().delete()


def test_a_reason_is_required():
    r = client_for(main_admin()).patch(ADMIN, _configure_payload(reason="short"), format="json")
    assert r.status_code == 400 and r.json()["field"] == "reason"


def test_cannot_enable_without_dates():
    r = client_for(main_admin()).patch(ADMIN, {"enabled": True, "reason": REASON}, format="json")
    assert r.status_code == 400 and r.json()["field"] == "enabled"


@pytest.mark.parametrize("field, value", [
    ("first_week_start", "2026-11-10"),  # a Tuesday
    ("initial_weeks", 0),
    ("initial_weeks", 99),
    ("expansion_weekday", 7),
    ("expansion_time", "25:00"),
    ("min_lead_days", -1),
    ("materialized_expansions", 5),  # not editable
    ("manual_weeks", 3),  # not editable (use advance)
])
def test_invalid_or_protected_fields_are_refused(field, value):
    r = client_for(main_admin()).patch(ADMIN, _configure_payload(**{field: value}), format="json")
    assert r.status_code == 400, r.content
    assert not ExternalBookingWindowChange.objects.exists()


def test_shortening_needs_confirmation_and_is_blocked_by_existing_external_bookings(factory):
    admin = main_admin()
    client = client_for(admin)
    assert client.patch(ADMIN, _configure_payload(), format="json").status_code == 200
    r = client.patch(ADMIN, {"initial_weeks": 2, "reason": REASON}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "reduction_needs_confirmation"

    # An active external booking in week 3 blocks shortening to two weeks, even when confirmed.
    from iic_booking.equipment.models import Booking, BookingStatus

    eq = factory.equipment()
    first = monday(timezone.localdate()) + timedelta(days=7)
    booking = factory.booking(make_user(), eq, factory.at(first + timedelta(days=15)))
    r = client.patch(ADMIN, {"initial_weeks": 2, "reason": REASON, "confirm_reduction": True}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "reduction_blocked_by_bookings"
    assert ExternalBookingWindowConfig.load().initial_weeks == 4

    booking.status = BookingStatus.CANCELLED
    booking.save(update_fields=["status"])
    r = client.patch(ADMIN, {"initial_weeks": 2, "reason": REASON, "confirm_reduction": True}, format="json")
    assert r.status_code == 200
    assert ExternalBookingWindowConfig.load().initial_weeks == 2
    assert Booking.objects.get(pk=booking.pk).status == BookingStatus.CANCELLED  # nothing else touched


def test_pause_resume_and_advance_are_audited():
    admin = main_admin()
    client = client_for(admin)
    client.patch(ADMIN, _configure_payload(), format="json")
    before = client.get(ADMIN).json()["window"]["last_date"]
    r = client.post(ADMIN + "advance/", {"weeks": 2, "reason": REASON}, format="json")
    assert r.status_code == 200
    after = r.json()["window"]["last_date"]
    assert (timezone.datetime.fromisoformat(after) - timezone.datetime.fromisoformat(before)).days == 14
    assert client.post(ADMIN + "pause/", {"reason": REASON}, format="json").status_code == 200
    assert client.post(ADMIN + "pause/", {"reason": REASON}, format="json").json()["code"] == "already_paused"
    assert client.post(ADMIN + "resume/", {"reason": REASON}, format="json").status_code == 200
    actions = list(ExternalBookingWindowChange.objects.order_by("id").values_list("action", flat=True))
    assert actions == ["enable", "advance", "pause", "resume"]
    assert client.get(ADMIN + "history/").json()["history"][0]["action"] == "resume"


def test_preview_changes_nothing():
    admin = main_admin()
    r = client_for(admin).post(ADMIN + "preview/", _configure_payload(), format="json")
    assert r.status_code == 200
    assert r.json()["range_if_enabled"]["is_open"] is True
    assert not ExternalBookingWindowConfig.load().enabled
    assert not ExternalBookingWindowChange.objects.exists()


def test_user_endpoint_explains_a_date():
    enable_window()
    first = monday(timezone.localdate()) + timedelta(days=7)
    body = client_for(make_user()).get(
        "/api/v1/external-booking/window/", {"date": (first - timedelta(days=3)).isoformat()}
    ).json()
    assert body["applies_to_you"] is True and body["date"]["open"] is False
    inside = client_for(make_user()).get("/api/v1/external-booking/window/", {"date": first.isoformat()}).json()
    assert inside["date"]["open"] is True
    internal = client_for(make_user(UserType.STUDENT)).get("/api/v1/external-booking/window/").json()
    assert internal["applies_to_you"] is False


# --- the delegated bounds every slot view uses -----------------------------------------------------------------------

def test_existing_bounds_function_follows_the_central_window_only_when_enabled(factory):
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService

    eq = factory.equipment()
    legacy = ExternalSlotQuotaService.get_external_slot_window_date_bounds(eq)
    config = enable_window(weeks=4)
    lo, hi, _before = ExternalSlotQuotaService.get_external_slot_window_date_bounds(eq)
    assert (lo, hi) == (config.first_week_start, config.first_week_start + timedelta(days=27))
    config.enabled = False
    config.save()
    assert ExternalSlotQuotaService.get_external_slot_window_date_bounds(eq) == legacy


def test_closed_window_gives_an_empty_range(factory):
    eq = factory.equipment()
    enable_window(opened_days_ago=-2)  # opens in two days
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService

    lo, hi, _ = ExternalSlotQuotaService.get_external_slot_window_date_bounds(eq)
    assert lo > hi


def test_internal_bounds_are_untouched_by_the_central_window(factory):
    from iic_booking.equipment.api_views import get_internal_slot_window_date_bounds

    eq = factory.equipment()
    before = get_internal_slot_window_date_bounds(eq)
    enable_window()
    assert get_internal_slot_window_date_bounds(eq) == before


def test_template_window_and_mode_availability_use_the_central_window(factory):
    from iic_booking.equipment import mode_availability
    from iic_booking.equipment.template_slot_preference import booking_window

    eq = factory.equipment()
    config = enable_window()
    external = make_user()
    w = booking_window(eq, external)
    assert (w.min_date, w.max_date) == (config.first_week_start, config.first_week_start + timedelta(days=27))
    viewer = mode_availability.Viewer(mode_availability.VIEWER_EXTERNAL, signed_in=True)
    mw = mode_availability.booking_window(eq, viewer, None, timezone.now())
    assert (mw.min_date, mw.max_date) == (w.min_date, w.max_date)
    internal = booking_window(eq, make_user(UserType.STUDENT))
    assert internal.max_date != w.max_date or internal.min_date != w.min_date


# --- preparation task ----------------------------------------------------------------------------------------------

def test_prepare_task_records_each_open_week_once(factory, monkeypatch):
    from iic_booking.external_services import tasks

    calls = {"slots": [], "snapshots": []}
    monkeypatch.setattr(tasks, "_generate_slots", lambda week: calls["slots"].append(week) or 3)
    monkeypatch.setattr(tasks, "_ensure_snapshots", lambda week: calls["snapshots"].append(week) or 1)
    assert tasks.prepare_open_weeks() == {"skipped": "disabled"}
    config = enable_window(weeks=4)
    first = tasks.prepare_open_weeks()
    assert first["recorded"] == 4 and first["quota_prepared"] == 4
    weeks = list(ExternalBookingWindowOpening.objects.values_list("week_start", flat=True))
    assert weeks == [config.first_week_start + timedelta(days=7 * i) for i in range(4)]
    assert sorted(calls["slots"]) == weeks and sorted(calls["snapshots"]) == weeks
    second = tasks.prepare_open_weeks()  # repeated run: nothing to do
    assert second["recorded"] == 0 and second["quota_prepared"] == 0
    assert len(calls["slots"]) == 4


def test_prepare_task_snapshot_uses_generated_slots(factory):
    from iic_booking.equipment.models import ExternalWeeklySlotQuotaSnapshot
    from iic_booking.external_services import tasks

    eq = factory.equipment()
    config = enable_window(weeks=1)
    factory.slot(eq, factory.at(config.first_week_start + timedelta(days=1)))
    tasks.prepare_open_weeks()
    snap = ExternalWeeklySlotQuotaSnapshot.objects.get(equipment=eq, week_start=config.first_week_start)
    assert snap.total_bookable_slots >= 1
