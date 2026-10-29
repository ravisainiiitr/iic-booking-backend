"""Pre-commit review blockers: A1 (a declaration after the lab has the sample), A2 (one user spending the shared
quota), A3 (reminder blast at go-live, pilot mode)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.courier_tracking import reminders, services
from iic_booking.courier_tracking.constants import (
    MAX_COURIER_REGISTRATIONS_PER_BOOKING,
    STAFF_REFRESH_DAILY_LIMIT,
    LookupOutcome,
    LookupTrigger,
)
from iic_booking.courier_tracking.models import CourierTrackingSettings, SampleArrival, TrackingLookup
from iic_booking.department_modules import services as module_services
from iic_booking.department_modules.constants import ModuleKey
from iic_booking.equipment.models import FABRICATION_PROFILE_TYPES, Booking, BookingSampleTrace, BookingStatus, SampleTraceStatus
from iic_booking.equipment.sample_trace_policy import latest_sample_trace
from iic_booking.users.models.user_type import UserType

from .conftest import (
    FakeResponse,
    make_booking,
    make_department,
    make_equipment,
    make_user,
    set_tracking,
    track_payload,
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


def test_reregistration_first_lookup_cannot_use_the_reserve(provider, catalog, external_booking, external_user, no_queue):
    cfg = CourierTrackingSettings.load()
    cfg.monthly_quota_fallback = 3
    cfg.safety_reserve = 2
    cfg.save()
    first = _courier(external_user, external_booking, NUMBERS[0])
    provider.queue(FakeResponse(200, track_payload(used=None, quota=None) | {"usage": {}}))
    assert services.lookup(first.pk, LookupTrigger.INITIAL).performed  # 1 of 3 used: within the routine part
    second = _courier(external_user, external_booking, NUMBERS[1])
    refused = services.lookup(second.pk, LookupTrigger.INITIAL)
    assert not refused.performed and refused.refused == "reserve"
    other = _courier(external_user, make_booking(external_user, external_booking.equipment), NUMBERS[2])
    provider.queue(FakeResponse(200, track_payload(used=None, quota=None) | {"usage": {}}))
    assert services.lookup(other.pk, LookupTrigger.INITIAL).performed  # another booking's first shipment may


@pytest.mark.parametrize("status", [BookingStatus.PENDING_PAYMENT, BookingStatus.HOLD, BookingStatus.CANCELLED,
                                    BookingStatus.COMPLETED])
def test_shipments_of_unconfirmed_or_closed_bookings_are_not_looked_up(provider, catalog, external_booking,
                                                                       external_user, no_queue, status):
    arrival = _courier(external_user, external_booking)
    Booking.objects.filter(pk=external_booking.pk).update(status=status)
    for trigger in (LookupTrigger.INITIAL, LookupTrigger.SCHEDULED, LookupTrigger.STAFF):
        result = services.lookup(arrival.pk, trigger)
        assert not result.performed and result.refused == "booking_not_confirmed"
    services.run_scheduled_checks()
    assert provider.track_calls == [] and not TrackingLookup.objects.exists()


def test_staff_refreshes_have_a_daily_limit(provider, catalog, external_user, iic, no_queue):
    admin = make_user(UserType.ADMIN, is_staff=True)
    equipment = make_equipment(iic)
    arrivals = []
    for i in range(STAFF_REFRESH_DAILY_LIMIT + 1):
        booking = make_booking(external_user, equipment)
        arrivals.append(SampleArrival.objects.create(
            booking=booking, user=external_user, equipment=equipment, department=iic, arrival_mode="COURIER",
            courier_slug="dtdc", courier_name="DTDC", tracking_number=f"D30103{i:05d}", tracking_number_raw="x",
            status="REGISTERED"))
    for arrival in arrivals[:STAFF_REFRESH_DAILY_LIMIT]:
        provider.queue(FakeResponse(200, track_payload()))
        assert services.lookup(arrival.pk, LookupTrigger.STAFF, actor=admin).outcome == LookupOutcome.SUCCESS
    refused = services.lookup(arrivals[-1].pk, LookupTrigger.STAFF, actor=admin)
    assert not refused.performed and refused.refused == "daily_limit"


# --- A3: reminders respect go-live, pilot mode and what the booking actually needs -----------------------------------

def test_bookings_made_before_tracking_was_switched_on_get_no_arrival_reminder(catalog, external_user, no_queue):
    from iic_booking.courier_tracking.models import ShipmentReminder

    from iic_booking.department_modules.models import DepartmentModuleSetting

    switched_on_now, switched_on_earlier = make_department(), make_department()
    set_tracking(switched_on_now, True)  # switched on today: the 5-day-old booking below predates it
    DepartmentModuleSetting.objects.create(department=switched_on_earlier, module_key=ModuleKey.COURIER_TRACKING.value,
                                           enabled=True)
    tracking_on_since(switched_on_earlier, timezone.now() - timedelta(days=10))
    late = make_booking(external_user, make_equipment(switched_on_now))
    early = make_booking(external_user, make_equipment(switched_on_earlier))
    for booking in (late, early):
        _age(booking, 5)
    assert reminders.run() == {"ARRIVAL_NOT_SELECTED": 1}
    assert list(ShipmentReminder.objects.values_list("booking_id", flat=True)) == [early.pk]


def test_tracking_enabled_since_uses_the_latest_switch_on(other_dept):
    from iic_booking.courier_tracking.eligibility import tracking_enabled_since

    assert tracking_enabled_since(other_dept.pk) is None  # never switched on: unknown, so no reminders
    earlier = timezone.now() - timedelta(days=10)
    tracking_on_since(other_dept, earlier)
    assert tracking_enabled_since(other_dept.pk) == earlier
    set_tracking(other_dept, True)  # switched on again now (it was off: no row)
    assert tracking_enabled_since(other_dept.pk) > earlier


def test_seeded_iic_switch_counts_from_the_seeding(catalog, external_user, no_queue):
    from iic_booking.department_modules.models import DepartmentModuleAuditLog, DepartmentModuleSetting

    dept = make_department()
    booking = make_booking(external_user, make_equipment(dept))
    _age(booking, 5)
    # What the seeding migration writes for IIC at go-live: an enabled row and a "seeded" audit entry.
    DepartmentModuleSetting.objects.create(department=dept, module_key=ModuleKey.COURIER_TRACKING.value, enabled=True,
                                           source="seed")
    DepartmentModuleAuditLog.objects.create(
        department=dept, module_key=ModuleKey.COURIER_TRACKING.value, action="module.seeded", old_value={},
        new_value={"enabled": True, "test_users_only": False, "configured": True}, created_at=timezone.now(),
    )
    assert reminders.run() == {}  # booked before go-live
    later = make_booking(external_user, make_equipment(dept))
    Booking.objects.filter(pk=later.pk).update(created_at=timezone.now() + timedelta(seconds=1))
    assert reminders.run() == {}  # made after go-live but too new to remind yet (the 2-day wait still applies)


def test_pilot_mode_reminds_test_users_only(catalog, iic, no_queue):
    module_services.set_module(make_user(UserType.ADMIN, is_staff=True), iic, ModuleKey.COURIER_TRACKING.value,
                               test_users_only=True, reason="pilot with test users")
    equipment = make_equipment(iic)
    ordinary = make_booking(make_user(UserType.EXTERNAL), equipment)
    tester = make_booking(make_user(UserType.EXTERNAL, is_test_account=True), equipment)
    for booking in (ordinary, tester):
        _age(booking, 3)
    assert reminders.run() == {"ARRIVAL_NOT_SELECTED": 1}
    from iic_booking.courier_tracking.models import ShipmentReminder

    assert list(ShipmentReminder.objects.values_list("booking_id", flat=True)) == [tester.pk]


@pytest.mark.parametrize("status", [BookingStatus.PENDING, BookingStatus.PENDING_PAYMENT, BookingStatus.HOLD])
def test_unconfirmed_bookings_get_no_arrival_reminder(catalog, iic_equipment, external_user, no_queue, status):
    _age(make_booking(external_user, iic_equipment, status=status), 3)
    assert reminders.run() == {}


def test_fabrication_and_walk_in_equipment_get_no_arrival_reminder(catalog, iic, external_user, no_queue):
    fabrication = make_equipment(iic)
    fabrication.profile_type = sorted(FABRICATION_PROFILE_TYPES)[0]
    fabrication.save(update_fields=["profile_type"])
    walk_in = make_equipment(iic)
    walk_in.sample_submission_lead_hours = 0
    walk_in.sample_collect_deadline_hours = 0
    walk_in.save(update_fields=["sample_submission_lead_hours", "sample_collect_deadline_hours"])
    for equipment in (fabrication, walk_in):
        _age(make_booking(external_user, equipment), 3)
    assert reminders.run() == {}


def test_sample_already_reported_sent_gets_no_arrival_reminder(catalog, external_booking, no_queue):
    BookingSampleTrace.objects.create(booking=external_booking, status=SampleTraceStatus.SAMPLE_SENT)
    _age(external_booking, 3)
    assert reminders.run() == {}
