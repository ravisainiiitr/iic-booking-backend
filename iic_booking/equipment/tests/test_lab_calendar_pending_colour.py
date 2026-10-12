"""Lab Operator / OIC calendars: open bookings whose sample the lab accepted get the editable Pending colour."""

from __future__ import annotations

import pytest

from iic_booking.equipment.models import BookingSampleTrace, DailySlot, SampleTraceStatus
from iic_booking.equipment.serializers import DailySlotSerializer
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

STAFF = {"include_booking_user_contact": True}


@pytest.fixture
def lab():
    f = _EgsFactory()
    eq = f.equipment()
    booking = f.booking(f.student(), eq, f.future(days=2))
    slot = DailySlot.objects.get(booking=booking)
    return f, eq, booking, slot


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], False),
        ([SampleTraceStatus.SAMPLE_SENT], False),
        ([SampleTraceStatus.SAMPLE_SENT, SampleTraceStatus.SAMPLE_ACCEPTED], True),
        ([SampleTraceStatus.PROCESSING], True),
    ],
)
def test_slot_reports_sample_accepted_for_staff(lab, statuses, expected):
    _f, _eq, booking, slot = lab
    for status in statuses:
        BookingSampleTrace.objects.create(booking=booking, status=status)

    assert DailySlotSerializer(slot, context=STAFF).data["booking_sample_accepted"] is expected


def test_slot_hides_sample_accepted_outside_staff_calendars(lab):
    _f, _eq, booking, slot = lab
    BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_ACCEPTED)

    assert DailySlotSerializer(slot).data["booking_sample_accepted"] is None


def test_pending_colour_is_editable_with_a_default(lab):
    f, eq, _booking, _slot = lab
    oic = UserFactory(user_type=UserType.ADMIN)
    client = f.client_for(oic)

    url = "/api/bookings/lab-dashboard-calendar-colors/"
    got = client.get(url, {"equipment_id": eq.pk})
    assert got.status_code == 200, got.data
    assert got.data["slot_colors"]["PENDING"] == "#f59e0b"

    saved = client.patch(url, {"equipment_id": eq.pk, "slot_colors": {"PENDING": "#a855f7"}}, format="json")
    assert saved.status_code == 200, saved.data
    again = client.get(url, {"equipment_id": eq.pk})
    assert again.data["slot_colors"]["PENDING"] == "#a855f7"
