import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from iic_booking.courier_tracking import quota, services
from iic_booking.courier_tracking.constants import ArrivalMode, LookupOutcome, LookupTrigger, ShipmentStatus
from iic_booking.courier_tracking.models import (
    CourierTrackingAlert,
    CourierTrackingSettings,
    SampleArrival,
    TrackingLookup,
    TrackingQuotaMonth,
)
from iic_booking.users.models.user_type import UserType

from .conftest import (
    FakeResponse,
    client_for,
    error_payload,
    make_booking,
    make_equipment,
    make_user,
    set_tracking,
    track_payload,
)

pytestmark = pytest.mark.django_db


def _register(booking, user, number="D3010377227", slug="dtdc"):
    return services.register_arrival(user, booking, services.ArrivalInput(
        arrival_mode="COURIER", courier_slug=slug, tracking_number=number, email_updates=False))


@pytest.fixture
def shipment(catalog, external_booking, external_user, no_queue):
    return _register(external_booking, external_user)


def test_initial_lookup_applies_result_and_reconciles_provider_usage(provider, shipment):
    provider.queue(FakeResponse(200, track_payload("intransit", used=41, quota=100)))
    result = services.lookup(shipment.pk, LookupTrigger.INITIAL)
    assert result.performed and result.outcome == LookupOutcome.SUCCESS
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.IN_TRANSIT and shipment.provider_state == "intransit"
    assert shipment.latest_checkpoint["activity"] == "On the way" and len(shipment.checkpoints) == 2
    assert shipment.last_lookup_success_at and shipment.next_lookup_at > timezone.now()
    lookup = TrackingLookup.objects.get()
    assert lookup.consumed is True and lookup.provider_used == 41 and lookup.trigger == LookupTrigger.INITIAL
    status = quota.status()
    assert status.provider_used == 41 and status.quota == 100 and status.quota_source == "provider"
    assert status.used == 41  # provider figure is authoritative when higher than the local count


def test_repeat_lookup_replaces_checkpoints_without_duplicates(provider, shipment):
    provider.queue(FakeResponse(200, track_payload()), FakeResponse(200, track_payload()))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    SampleArrival.objects.filter(pk=shipment.pk).update(last_lookup_attempt_at=None, next_lookup_at=timezone.now())
    services.lookup(shipment.pk, LookupTrigger.SCHEDULED)
    shipment.refresh_from_db()
    assert len(shipment.checkpoints) == 2


def test_quota_exceeded_blocks_until_month_end_and_alerts(provider, shipment):
    provider.queue(FakeResponse(402, error_payload("QUOTA_EXCEEDED", used=100, quota=100)))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    assert TrackingLookup.objects.get().consumed is False
    assert quota.status().blocked_outcome == LookupOutcome.QUOTA_EXCEEDED
    assert CourierTrackingAlert.objects.filter(kind="QUOTA_EXHAUSTED").exists()
    SampleArrival.objects.filter(pk=shipment.pk).update(last_lookup_attempt_at=None)
    again = services.lookup(shipment.pk, LookupTrigger.STAFF)
    assert not again.performed and again.refused == "blocked:QUOTA_EXCEEDED"
    assert len(provider.track_calls) == 1


def test_local_budget_and_reserve(provider, catalog, external_user, iic, no_queue, settings):
    cfg = CourierTrackingSettings.load()
    cfg.monthly_quota_fallback = 3
    cfg.safety_reserve = 1
    cfg.save()
    arrivals = [_register(make_booking(external_user, make_equipment(iic)), external_user, number=f"D30103772{i}")
                for i in range(4)]
    for _ in range(3):
        provider.queue(FakeResponse(200, track_payload(used=None, quota=None) | {"usage": {}}))
    # Routine (user) refresh may not use the reserve: 3 quota - 1 reserve = 2 routine slots.
    assert services.lookup(arrivals[0].pk, LookupTrigger.INITIAL).performed
    assert services.lookup(arrivals[1].pk, LookupTrigger.INITIAL).performed
    SampleArrival.objects.filter(pk=arrivals[0].pk).update(last_lookup_attempt_at=None)
    refused = services.lookup(arrivals[0].pk, LookupTrigger.USER, actor=external_user)
    assert not refused.performed and refused.refused == "reserve"
    assert services.lookup(arrivals[2].pk, LookupTrigger.INITIAL).performed  # first lookups may use the reserve
    last = services.lookup(arrivals[3].pk, LookupTrigger.INITIAL)
    assert not last.performed and last.refused == "quota"
    assert len(provider.track_calls) == 3


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_cannot_overspend(catalog, iic):
    cfg = CourierTrackingSettings.load()
    cfg.monthly_quota_fallback = 3
    cfg.safety_reserve = 0
    cfg.save()
    barrier = threading.Barrier(8)
    granted = []

    def worker(i):
        try:
            barrier.wait()
            r = quota.reserve(trigger=LookupTrigger.INITIAL, courier_slug="dtdc", tracking_number=f"N{i}")
            granted.append(bool(r.lookup))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 3
    assert TrackingQuotaMonth.objects.get(period=quota.current_period()).in_flight == 3


def test_user_refresh_is_rate_limited(provider, shipment, external_user):
    provider.queue(FakeResponse(200, track_payload()))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    url = f"/api/v1/courier-tracking/shipments/{shipment.pk}/refresh/"
    response = client_for(external_user).post(url)
    assert response.status_code == 429 and response.data["checked"] is False
    assert len(provider.track_calls) == 1


def test_user_daily_refresh_limit(provider, catalog, external_user, iic, no_queue):
    cfg = CourierTrackingSettings.load()
    cfg.user_refresh_daily_limit = 1
    cfg.save()
    a = _register(make_booking(external_user, make_equipment(iic)), external_user, number="D301037701")
    b = _register(make_booking(external_user, make_equipment(iic)), external_user, number="D301037702")
    provider.queue(FakeResponse(200, track_payload()))
    assert client_for(external_user).post(f"/api/v1/courier-tracking/shipments/{a.pk}/refresh/").status_code == 200
    response = client_for(external_user).post(f"/api/v1/courier-tracking/shipments/{b.pk}/refresh/")
    assert response.status_code == 429 and len(provider.track_calls) == 1


def test_terminal_shipments_are_not_looked_up(provider, shipment):
    provider.queue(FakeResponse(200, track_payload("delivered", "Delivered")))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.DELIVERY_REPORTED and shipment.next_lookup_at is None
    SampleArrival.objects.filter(pk=shipment.pk).update(last_lookup_attempt_at=None)
    for trigger in (LookupTrigger.SCHEDULED, LookupTrigger.USER, LookupTrigger.STAFF):
        assert services.lookup(shipment.pk, trigger).refused == "terminal"
    assert len(provider.track_calls) == 1


def test_no_lookups_for_manual_internal_or_disabled(provider, catalog, iic, internal_user, external_user, no_queue):
    manual = services.register_arrival(external_user, make_booking(external_user, make_equipment(iic)),
                                       services.ArrivalInput(arrival_mode="MANUAL"))
    internal_manual = services.register_arrival(internal_user, make_booking(internal_user, make_equipment(iic)),
                                                services.ArrivalInput(arrival_mode="MANUAL"))
    courier = _register(make_booking(external_user, make_equipment(iic)), external_user)
    set_tracking(iic, False)
    for arrival in (manual, internal_manual):
        assert services.lookup(arrival.pk, LookupTrigger.STAFF).refused == "not_courier"
    assert services.lookup(courier.pk, LookupTrigger.STAFF).refused == "department_disabled"
    assert services.run_scheduled_checks()["allowed"] >= 0
    assert provider.track_calls == [] and not TrackingLookup.objects.exists()


def test_same_parcel_on_two_bookings_costs_one_request(provider, catalog, external_user, iic, no_queue):
    first = _register(make_booking(external_user, make_equipment(iic)), external_user)
    second = _register(make_booking(external_user, make_equipment(iic)), external_user)
    provider.queue(FakeResponse(200, track_payload("outfordelivery", "Out for delivery")))
    services.lookup(first.pk, LookupTrigger.INITIAL)
    second.refresh_from_db()
    assert second.status == ShipmentStatus.OUT_FOR_DELIVERY  # updated from the same answer
    result = services.lookup(second.pk, LookupTrigger.INITIAL)
    assert result.refused in ("shared_recent_result", "already_looked_up")
    assert len(provider.track_calls) == 1 and TrackingLookup.objects.get().shared_with == 1


def test_invalid_number_is_not_counted_and_needs_user_fix(provider, shipment):
    provider.queue(FakeResponse(400, error_payload("INVALID_TRACKING_NUMBER")))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.TRACKING_PROBLEM and shipment.next_lookup_at is None
    assert TrackingLookup.objects.get().consumed is False
    assert quota.status().local_consumed == 0
    SampleArrival.objects.filter(pk=shipment.pk).update(last_lookup_attempt_at=None)
    assert services.lookup(shipment.pk, LookupTrigger.USER).refused == "details_need_attention"


def test_transient_failure_is_not_retried_blindly(provider, shipment):
    provider.queue(FakeResponse(504, error_payload("TIMEOUT")))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    shipment.refresh_from_db()
    assert shipment.last_problem == LookupOutcome.TIMEOUT
    assert shipment.next_lookup_at > timezone.now() + timedelta(hours=11)
    assert services.lookup(shipment.pk, LookupTrigger.SCHEDULED).refused == "not_due"
    assert len(provider.track_calls) == 1


def test_invalid_key_blocks_lookups_and_alerts_admins(provider, shipment):
    provider.queue(FakeResponse(401, error_payload("INVALID_API_KEY")))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    assert quota.status().blocked_outcome == LookupOutcome.AUTH_ERROR
    assert CourierTrackingAlert.objects.filter(kind="ACCOUNT_PROBLEM").exists()
    admin = make_user(UserType.ADMIN, is_staff=True)
    response = client_for(admin).post("/api/v1/courier-tracking/admin/clear-block/", {"reason": "key rotated"},
                                      format="json")
    assert response.data["cleared"] is True and quota.status().blocked_outcome == ""


def test_stale_reservation_is_counted(catalog):
    reservation = quota.reserve(trigger=LookupTrigger.INITIAL, courier_slug="dtdc", tracking_number="X1")
    TrackingLookup.objects.filter(pk=reservation.lookup.pk).update(reserved_at=timezone.now() - timedelta(hours=1))
    assert quota.release_stale_reservations() == 1
    status = quota.status()
    assert status.in_flight == 0 and status.local_consumed == 1
    assert TrackingLookup.objects.get().outcome == LookupOutcome.ABANDONED


def test_month_boundary_starts_a_new_period(catalog):
    from datetime import datetime, timezone as tz

    assert quota.current_period(datetime(2026, 10, 31, 23, 59, tzinfo=tz.utc)) == "2026-10"
    assert quota.current_period(datetime(2026, 11, 1, 0, 0, tzinfo=tz.utc)) == "2026-11"
    TrackingQuotaMonth.objects.create(period="2000-01", provider_used=100, provider_quota=100, blocked_outcome="QUOTA_EXCEEDED")
    assert quota.status().blocked_outcome == "" and quota.status().used == 0


def test_scheduled_checks_prioritise_new_shipments_and_respect_budget(provider, catalog, external_user, iic, no_queue):
    cfg = CourierTrackingSettings.load()
    cfg.monthly_quota_fallback = 100
    cfg.save()
    new = _register(make_booking(external_user, make_equipment(iic)), external_user, number="D301037711")
    provider.queue(FakeResponse(200, track_payload()))
    counts = services.run_scheduled_checks()
    assert counts.get(LookupOutcome.SUCCESS, 0) == 1 and len(provider.track_calls) == 1
    new.refresh_from_db()
    assert new.lookup_count == 1
    # Nothing else is due: a second run makes no request.
    services.run_scheduled_checks()
    assert len(provider.track_calls) == 1


def test_page_views_never_call_the_provider(provider, shipment, external_user):
    client_for(external_user).get("/api/v1/courier-tracking/shipments/")
    client_for(external_user).get(f"/api/v1/courier-tracking/shipments/{shipment.pk}/")
    client_for(external_user).get(f"/api/v1/courier-tracking/bookings/{shipment.booking_id}/arrival/")
    assert provider.track_calls == []
    assert SampleArrival.objects.get(pk=shipment.pk).arrival_mode == ArrivalMode.COURIER


def test_without_api_key_nothing_is_reserved_blocked_or_alerted(settings, catalog, external_booking, external_user, no_queue):
    settings.TRACKCOURIER_API_KEY = ""
    arrival = _register(external_booking, external_user)
    result = services.lookup(arrival.pk, LookupTrigger.INITIAL)
    assert not result.performed and result.refused == "not_configured"
    assert not TrackingLookup.objects.exists() and quota.status().blocked_outcome == ""
    assert not CourierTrackingAlert.objects.exists()
