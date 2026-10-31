"""Review blockers that still apply: a declaration after the lab has the sample, limits on changing courier details."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.courier_tracking import services
from iic_booking.courier_tracking.constants import (
    MAX_COURIER_REGISTRATIONS_PER_BOOKING,
)
from iic_booking.courier_tracking.models import SampleArrival
from iic_booking.equipment.models import Booking, BookingSampleTrace, SampleTraceStatus
from iic_booking.equipment.sample_trace_policy import latest_sample_trace

from .conftest import (
    make_booking,
    make_equipment,
    set_tracking,
    tracking_on_since,
)

pytestmark = pytest.mark.django_db

NUMBERS = ["D3010377221", "D3010377222", "D3010377223", "D3010377224", "D3010377225", "D3010377226"]


def _courier(user, booking, number=NUMBERS[0]):
    return services.register_arrival(user, booking, services.ArrivalInput(
        arrival_mode="COURIER", courier_slug="dtdc", tracking_number=number))


def _age(booking, days):
    Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(days=days))
    booking.refresh_from_db()


# --- A1: no declaration change once the lab has the sample; one "Sample Sent" per booking -----------------------------

@pytest.mark.parametrize("lab_status", [SampleTraceStatus.HELD_AT_OFFICE, SampleTraceStatus.FORWARDED_TO_LAB,
                                        SampleTraceStatus.SAMPLE_ACCEPTED, SampleTraceStatus.PROCESSING])
def test_declaration_is_frozen_once_the_lab_records_the_sample(catalog, external_booking, external_user, no_queue, lab_status):
    _courier(external_user, external_booking)
    BookingSampleTrace.objects.create(booking=external_booking, status=lab_status)
    for mode, number in (("COURIER", NUMBERS[1]), ("MANUAL", "")):
        with pytest.raises(services.ArrivalError) as exc:
            services.register_arrival(external_user, external_booking, services.ArrivalInput(
                arrival_mode=mode, courier_slug="dtdc" if number else "", tracking_number=number))
        assert exc.value.status == 403 and "already recorded" in exc.value.message
    # The lab's event stays the latest one, so the operator-unavailable auto refund and staff summaries still see it.
    assert latest_sample_trace(external_booking.pk).status == lab_status
    assert SampleArrival.objects.filter(booking=external_booking).count() == 1


def test_arrival_api_explains_why_the_declaration_is_frozen(catalog, external_booking, external_user, no_queue):
    from .conftest import client_for

    BookingSampleTrace.objects.create(booking=external_booking, status=SampleTraceStatus.HELD_AT_OFFICE)
    data = client_for(external_user).get(f"/api/v1/courier-tracking/bookings/{external_booking.pk}/arrival/").data
    assert str(data).count("already recorded") >= 1
    response = client_for(external_user).post(f"/api/v1/courier-tracking/bookings/{external_booking.pk}/arrival/",
                                              {"arrival_mode": "MANUAL"}, format="json")
    assert response.status_code == 403


def test_changing_courier_details_adds_no_second_sample_sent_event(catalog, external_booking, external_user, no_queue):
    _courier(external_user, external_booking, NUMBERS[0])
    _courier(external_user, external_booking, NUMBERS[1])
    events = BookingSampleTrace.objects.filter(booking=external_booking)
    assert list(events.values_list("status", flat=True)) == [SampleTraceStatus.SAMPLE_SENT]
    assert SampleArrival.objects.filter(booking=external_booking).count() == 2  # the change is in the arrival history


def test_existing_sample_sent_from_the_older_dialog_is_not_duplicated(catalog, external_booking, external_user, no_queue):
    BookingSampleTrace.objects.create(booking=external_booking, status=SampleTraceStatus.SAMPLE_SENT, tracking_id="old")
    _courier(external_user, external_booking)
    assert BookingSampleTrace.objects.filter(booking=external_booking).count() == 1


# --- A2: one user cannot spend the shared monthly quota --------------------------------------------------------------

def test_courier_changes_per_booking_are_limited(catalog, external_booking, external_user, no_queue):
    for number in NUMBERS[:MAX_COURIER_REGISTRATIONS_PER_BOOKING]:
        _courier(external_user, external_booking, number)
    with pytest.raises(services.ArrivalError) as exc:
        _courier(external_user, external_booking, NUMBERS[MAX_COURIER_REGISTRATIONS_PER_BOOKING])
    assert exc.value.status == 429 and exc.value.code == "too_many_changes"
    # Switching to manual delivery is always possible, and a double submit of the active one is not a change.
    _courier(external_user, external_booking, NUMBERS[MAX_COURIER_REGISTRATIONS_PER_BOOKING - 1])
    services.switch_to_manual(external_user, external_booking)


def test_courier_registrations_per_user_per_day_are_limited(catalog, external_user, iic, no_queue):
    equipment = make_equipment(iic)
    bookings = [make_booking(external_user, equipment) for _ in range(6)]
    for booking, number in zip(bookings[:5], NUMBERS):
        _courier(external_user, booking, number)
    with pytest.raises(services.ArrivalError) as exc:
        _courier(external_user, bookings[5], NUMBERS[5])
    assert exc.value.code == "daily_registration_limit"
    SampleArrival.objects.update(created_at=timezone.now() - timedelta(days=2))
    _courier(external_user, bookings[5], NUMBERS[5])  # a day later it is accepted


def test_tracking_enabled_since_uses_the_latest_switch_on(other_dept):
    from iic_booking.courier_tracking.eligibility import tracking_enabled_since

    assert tracking_enabled_since(other_dept.pk) is None  # never switched on: unknown, so no reminders
    earlier = timezone.now() - timedelta(days=10)
    tracking_on_since(other_dept, earlier)
    assert tracking_enabled_since(other_dept.pk) == earlier
    set_tracking(other_dept, True)  # switched on again now (it was off: no row)
    assert tracking_enabled_since(other_dept.pk) > earlier
