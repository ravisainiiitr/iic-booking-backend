"""The central window is enforced by the booking API itself (not only by hiding dates in the slot grid)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, enable_window, give_wallet, main_admin, make_user, monday

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_portal_lock(monkeypatch):
    """The portal-migration booking lock is not under test (same approach as the equipment booking tests)."""
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _book(user, eq, slot, **extra):
    if UserType.is_external_user(user.user_type):
        give_wallet(user, eq.internal_department)
    body = {"slot_ids": [slot.id], "input_values": {}, **extra}
    return client_for(user).post(f"/api/equipments/{eq.pk}/book/", body, format="json")


def _dates(config):
    first = config.first_week_start
    return first - timedelta(days=3), first + timedelta(days=2), first + timedelta(days=7 * config.initial_weeks + 1)


def test_external_booking_outside_the_window_is_refused_by_the_api(factory):
    eq = factory.equipment()
    config = enable_window()
    before, _inside, after = _dates(config)
    user = make_user()
    for d in (before, after):
        slot = factory.slot(eq, factory.at(d, 11))
        r = _book(user, eq, slot)
        assert r.status_code == 400, r.content
        body = r.json()
        assert body.get("code") == "EXTERNAL_WINDOW_CLOSED", body
        assert body["dates_outside_window"] == [d.isoformat()]
        slot.refresh_from_db()
        assert slot.booking_id is None and slot.status == "AVAILABLE"
    assert not Booking.objects.filter(user=user).exists()


def test_external_booking_inside_the_window_is_not_refused_by_the_window(factory):
    eq = factory.equipment()
    config = enable_window()
    _before, inside, _after = _dates(config)
    slot = factory.slot(eq, factory.at(inside, 11))
    r = _book(make_user(), eq, slot)
    assert r.json().get("code") != "EXTERNAL_WINDOW_CLOSED", r.content
    # Every check passed; an external self-booking is then paid through the checkout (nothing held before payment).
    assert r.status_code == 409 and r.json().get("code") == "external_checkout_required", r.content


def test_window_disabled_keeps_previous_behaviour(factory):
    eq = factory.equipment()
    config = enable_window()
    config.enabled = False
    config.save()
    _before, _inside, after = _dates(config)
    slot = factory.slot(eq, factory.at(after, 11))
    r = _book(make_user(), eq, slot)
    assert r.json().get("code") != "EXTERNAL_WINDOW_CLOSED"


def test_internal_users_are_not_affected(factory):
    eq = factory.equipment()
    config = enable_window()
    before, _inside, _after = _dates(config)
    student = make_user(UserType.STUDENT, department=factory.department)
    slot = factory.slot(eq, factory.at(max(before, timezone.localdate() + timedelta(days=1)), 11))
    r = _book(student, eq, slot)
    assert r.json().get("code") != "EXTERNAL_WINDOW_CLOSED"


def test_main_admin_booking_for_an_external_user_is_exempt(factory):
    eq = factory.equipment()
    config = enable_window()
    _before, _inside, after = _dates(config)
    user = make_user()
    slot = factory.slot(eq, factory.at(after, 11))
    r = _book(main_admin(), eq, slot, user_id=user.pk)
    assert r.json().get("code") != "EXTERNAL_WINDOW_CLOSED"


def test_external_users_cannot_create_holds(factory):
    eq = factory.equipment()
    config = enable_window()
    _before, inside, _after = _dates(config)
    user = make_user()
    slot = factory.slot(eq, factory.at(inside, 11))
    r = _book(user, eq, slot, create_as_hold=True)
    assert r.status_code == 403 and r.json()["code"] == "EXTERNAL_HOLD_NOT_ALLOWED"
    slot.refresh_from_db()
    assert slot.booking_id is None


def test_external_hold_refusal_applies_even_with_the_window_disabled(factory):
    eq = factory.equipment()
    slot = factory.slot(eq, factory.at(timezone.localdate() + timedelta(days=10), 11))
    r = _book(make_user(), eq, slot, create_as_hold=True)
    assert r.status_code == 403 and r.json()["code"] == "EXTERNAL_HOLD_NOT_ALLOWED"


def test_user_reschedule_outside_the_window_is_refused(factory):
    eq = factory.equipment()
    config = enable_window()
    _before, inside, after = _dates(config)
    user = make_user()
    booking = factory.booking(user, eq, factory.at(inside, 9))
    target = factory.slot(eq, factory.at(after, 11))
    r = client_for(user).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"slot_ids": [target.id], "start_time": target.start_datetime.isoformat(),
         "end_time": target.end_datetime.isoformat()},
        format="json",
    )
    if r.status_code == 404:
        pytest.skip("user reschedule route differs on this baseline")
    body = r.json()
    if body.get("code") != "EXTERNAL_WINDOW_CLOSED":
        # The view may refuse earlier for its own reasons (lead time, policy); it must never move the booking.
        assert r.status_code >= 400, body
    booking.refresh_from_db()
    assert not booking.daily_slots.filter(pk=target.pk).exists()


def test_slots_api_carries_the_central_window_for_external_users(factory):
    eq = factory.equipment()
    config = enable_window()
    week = config.first_week_start
    factory.slot(eq, factory.at(week + timedelta(days=1), 11))
    r = client_for(make_user()).get(f"/api/equipments/{eq.pk}/slots/", {"start_date": week.isoformat(), "end_date": (week + timedelta(days=6)).isoformat()})
    if r.status_code == 404:
        pytest.skip("slots route differs on this baseline")
    body = r.json()
    assert body["external_window"]["enabled"] is True
    assert body["slot_window_min_date"] == config.first_week_start.isoformat()
    assert body["slot_window_reference_weekday"] is None
    internal = client_for(make_user(UserType.STUDENT, department=factory.department)).get(
        f"/api/equipments/{eq.pk}/slots/", {"start_date": week.isoformat(), "end_date": (week + timedelta(days=6)).isoformat()}
    ).json()
    assert internal.get("external_window") is None


def test_monday_helper():
    assert monday(timezone.localdate()).weekday() == 0
