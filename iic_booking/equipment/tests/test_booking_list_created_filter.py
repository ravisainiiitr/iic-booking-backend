"""View Booking created_from / created_to: bookings by the local day they were made (dashboard "Bookings made today")."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking
from iic_booking.facility_groups.tests.conftest import client_for, make_booking, make_department, make_equipment, make_user
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db

LIST = "/api/bookings/"


def _ids(res):
    return {b["virtual_booking_id"] for b in res.data["bookings"]}


@pytest.fixture
def world():
    equipment = make_equipment(make_department("Physics"))
    student = make_user(user_type=UserType.STUDENT)
    today = make_booking(student, equipment)
    older = make_booking(student, equipment)
    Booking.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=3))
    return {"today": today, "older": older}


def test_made_today_lists_only_todays_bookings(world):
    day = timezone.localdate().isoformat()
    res = client_for(make_user(user_type=UserType.ADMIN)).get(
        LIST, {"created_from": day, "created_to": day, "list_view": "true"}
    )
    assert res.status_code == 200
    assert _ids(res) == {world["today"].virtual_booking_id}


def test_created_from_alone_includes_later_days(world):
    since = (timezone.localdate() - timedelta(days=5)).isoformat()
    res = client_for(make_user(user_type=UserType.ADMIN)).get(LIST, {"created_from": since, "list_view": "true"})
    assert _ids(res) == {world["today"].virtual_booking_id, world["older"].virtual_booking_id}


def test_invalid_created_date_is_refused(world):
    res = client_for(make_user(user_type=UserType.ADMIN)).get(LIST, {"created_from": "yesterday"})
    assert res.status_code == 400


def test_exclude_test_leaves_out_test_accounts(world):
    tester = make_user(user_type=UserType.STUDENT, is_test_account=True)
    test_booking = make_booking(tester, world["today"].equipment)
    day = timezone.localdate().isoformat()
    admin = client_for(make_user(user_type=UserType.ADMIN))
    everything = _ids(admin.get(LIST, {"created_from": day, "list_view": "true"}))
    real = _ids(admin.get(LIST, {"created_from": day, "exclude_test": "1", "list_view": "true"}))
    assert test_booking.virtual_booking_id in everything
    assert real == {world["today"].virtual_booking_id}
