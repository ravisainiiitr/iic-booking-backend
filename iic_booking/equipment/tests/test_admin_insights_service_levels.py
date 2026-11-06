"""Service levels: access, scope, definitions, periods and the export."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/admin/insights/service-levels/"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _admin():
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


def _get(user, **params):
    resp = _client(user).get(URL, params)
    assert resp.status_code == 200, resp.content
    return resp.json()


@pytest.fixture
def lab():
    from iic_booking.equipment.models import Equipment

    Equipment.objects.all().delete()  # migration 0148 seeds a sample 3D printer
    factory = _EgsFactory()
    return factory, factory.equipment(), factory.student()


def _booking(factory, equipment, owner, *, days_ago=3, status=BookingStatus.COMPLETED, lead_days=2, results_after=None,
             **fields):
    start = (timezone.now() - timedelta(days=days_ago)).replace(minute=0, second=0, microsecond=0)
    booking = factory.booking(owner, equipment, start, **fields)
    Booking.objects.filter(pk=booking.pk).update(status=status, created_at=start - timedelta(days=lead_days))
    if results_after is not None:
        Booking.objects.filter(pk=booking.pk).update(
            results_available_notified_at=start + timedelta(minutes=60) + results_after
        )
    return booking


def _row(data, equipment):
    return next(r for r in data["results"] if r["equipment_id"] == equipment.pk)


@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.FACULTY, UserType.MANAGER, UserType.OPERATOR])
def test_only_dashboard_viewers_have_access(user_type):
    assert _client(UserFactory(user_type=user_type)).get(URL).status_code == 403


def test_anonymous_is_rejected():
    assert APIClient().get(URL).status_code in (401, 403)


def test_rates_follow_their_definitions(lab):
    factory, equipment, owner = lab
    _booking(factory, equipment, owner, status=BookingStatus.COMPLETED)
    _booking(factory, equipment, owner, days_ago=4, status=BookingStatus.COMPLETED)
    _booking(factory, equipment, owner, days_ago=5, status=BookingStatus.PROCESSING)
    _booking(factory, equipment, owner, days_ago=6, status=BookingStatus.BOOKING_NOT_UTILIZED)
    _booking(factory, equipment, owner, days_ago=7, status=BookingStatus.CANCELLED)
    _booking(factory, equipment, owner, days_ago=8, status=BookingStatus.ABSENT)
    _booking(factory, equipment, owner, days_ago=9, status=BookingStatus.WAITLISTED)  # not a booking yet

    row = _row(_get(_admin()), equipment)
    assert row["bookings"] == 6
    assert row["carried_out"] == 3
    assert row["no_show_rate"] == 0.25  # 1 of 3 carried out + 1 not utilized
    assert row["cancellation_rate"] == round(1 / 6, 4)
    assert row["lab_stopped_rate"] == round(1 / 6, 4)
    assert row["results_awaited"] == 1


def test_turnaround_and_lead_are_medians_and_only_rest_on_timed_results(lab):
    factory, equipment, owner = lab
    _booking(factory, equipment, owner, lead_days=1, results_after=timedelta(hours=2))
    _booking(factory, equipment, owner, days_ago=4, lead_days=3, results_after=timedelta(hours=4))
    _booking(factory, equipment, owner, days_ago=5, lead_days=8, results_after=timedelta(hours=30))
    _booking(factory, equipment, owner, days_ago=6, lead_days=3)  # results never notified

    row = _row(_get(_admin()), equipment)
    assert row["turnaround_hours"] == 4.0
    assert row["results_timed"] == 3
    assert row["lead_days"] == 3.0


def test_results_before_the_slot_ended_count_as_zero_and_no_results_is_no_figure(lab):
    factory, equipment, owner = lab
    other = factory.equipment()
    _booking(factory, equipment, owner, results_after=timedelta(hours=-1))
    _booking(factory, other, owner)

    data = _get(_admin())
    assert _row(data, equipment)["turnaround_hours"] == 0.0
    assert _row(data, other)["turnaround_hours"] is None
    assert _row(data, other)["results_timed"] == 0


def test_an_instrument_with_no_figure_never_sorts_as_best_or_worst(lab):
    factory, equipment, owner = lab
    other = factory.equipment()
    _booking(factory, equipment, owner, results_after=timedelta(hours=2))
    _booking(factory, other, owner)

    for sort in ("turnaround", "-turnaround"):
        assert _get(_admin(), sort=sort)["results"][-1]["equipment_id"] == other.pk


def test_removed_ratings_do_not_count(lab):
    factory, equipment, owner = lab
    _booking(factory, equipment, owner, rating=5)
    _booking(factory, equipment, owner, days_ago=4, rating=1, rating_removed=True)

    row = _row(_get(_admin()), equipment)
    assert row["rating"] == 5.0
    assert row["rated"] == 1


def test_a_booking_counts_in_the_period_of_its_first_slot_and_previous_is_the_same_length(lab):
    factory, equipment, owner = lab
    _booking(factory, equipment, owner, days_ago=3)
    _booking(factory, equipment, owner, days_ago=40, status=BookingStatus.BOOKING_NOT_UTILIZED)

    today = timezone.localdate()
    data = _get(_admin(), date_from=(today - timedelta(days=29)).isoformat(), date_to=today.isoformat())
    assert data["summary"]["bookings"] == 1
    assert data["summary"]["previous"]["bookings"] == 1
    assert data["summary"]["previous"]["not_utilized"] == 1
    assert data["previous_to"] == (today - timedelta(days=30)).isoformat()
    assert data["previous_from"] == (today - timedelta(days=59)).isoformat()


def test_department_admin_sees_only_their_department(lab):
    factory, equipment, owner = lab
    other_factory = _EgsFactory()
    other = other_factory.equipment()
    _booking(factory, equipment, owner)
    _booking(other_factory, other, other_factory.student())

    admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=factory.department, admin_approved=True)
    data = _get(admin)
    assert data["scope"] == "department"
    assert {r["equipment_id"] for r in data["results"]} == {equipment.pk}
    assert data["summary"]["bookings"] == 1


def test_modes_count_on_the_parent_row(lab):
    factory, equipment, owner = lab
    equipment.enable_multi_mode = True
    equipment.save()
    mode = factory.equipment()
    mode.parent_equipment = equipment
    mode.save()
    _booking(factory, mode, owner)

    data = _get(_admin())
    assert _row(data, equipment)["bookings"] == 1
    assert mode.pk not in {r["equipment_id"] for r in data["results"]}


def test_export_lists_the_instruments(lab):
    factory, equipment, owner = lab
    _booking(factory, equipment, owner, results_after=timedelta(hours=2))

    resp = _client(_admin()).get("/api/exports/admin-service-levels/", {"export_format": "csv"})
    assert resp.status_code == 200, resp.content
    body = b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content
    assert equipment.name.encode() in body
