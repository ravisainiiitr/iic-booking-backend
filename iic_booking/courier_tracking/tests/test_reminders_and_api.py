from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.courier_tracking import reminders, services
from iic_booking.courier_tracking.constants import ShipmentStatus
from iic_booking.courier_tracking.models import CourierTrackingSettings, SampleArrival, ShipmentReminder
from iic_booking.department_modules.models import DepartmentModuleAuditLog
from iic_booking.equipment.models import Booking, BookingSampleTrace, BookingStatus, EquipmentManager, SampleTraceStatus
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_booking, make_equipment, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/courier-tracking"


def _age_booking(booking, days):
    Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(days=days))


def _courier(user, booking, number="D3010377227", email=False):
    return services.register_arrival(user, booking, services.ArrivalInput(
        arrival_mode="COURIER", courier_slug="dtdc", tracking_number=number, email_updates=email))


# --- reminders --------------------------------------------------------------------------------------------------------

def test_arrival_reminder_limits_interval_and_resolution(catalog, external_booking, external_user, no_queue):
    cfg = CourierTrackingSettings.load()
    cfg.reminder_max_count = 2
    cfg.reminder_interval_days = 3
    cfg.save()
    assert reminders.run() == {}  # booking too new
    _age_booking(external_booking, 3)
    assert reminders.run() == {"ARRIVAL_NOT_SELECTED": 1}
    assert reminders.run() == {}  # interval not reached
    ShipmentReminder.objects.update(created_at=timezone.now() - timedelta(days=4))
    assert reminders.run() == {"ARRIVAL_NOT_SELECTED": 1}
    ShipmentReminder.objects.update(created_at=timezone.now() - timedelta(days=10))
    assert reminders.run() == {}  # max count reached
    assert ShipmentReminder.objects.filter(kind="ARRIVAL_NOT_SELECTED").count() == 2
    assert ShipmentReminder.objects.filter(status="SENT").count() == 2


def test_choosing_an_arrival_method_stops_the_reminder(catalog, external_booking, external_user, no_queue):
    _age_booking(external_booking, 3)
    services.register_arrival(external_user, external_booking, services.ArrivalInput(arrival_mode="MANUAL"))
    assert reminders.run() == {}  # manual delivery: no courier reminders


def test_courier_problem_reminders_from_local_data_only(catalog, external_booking, external_user, no_queue, provider):
    arrival = _courier(external_user, external_booking)
    SampleArrival.objects.filter(pk=arrival.pk).update(status=ShipmentStatus.ATTEMPT_FAILED)
    assert reminders.run() == {"DELIVERY_PROBLEM": 1}
    SampleArrival.objects.filter(pk=arrival.pk).update(status=ShipmentStatus.IN_TRANSIT, last_problem="",
                                                       status_changed_at=timezone.now() - timedelta(days=9),
                                                       last_lookup_success_at=timezone.now())
    assert reminders.run() == {"NOT_PROGRESSING": 1}
    SampleArrival.objects.filter(pk=arrival.pk).update(last_problem="INVALID_TRACKING_NUMBER")
    assert reminders.run() == {"DETAILS_INCOMPLETE": 1}
    assert provider.calls == []  # deciding reminders never calls the provider


def test_no_reminders_for_internal_cancelled_or_received(catalog, iic_equipment, external_user, internal_user, no_queue):
    internal = make_booking(internal_user, iic_equipment)
    cancelled = make_booking(external_user, iic_equipment, status=BookingStatus.CANCELLED)
    received = make_booking(external_user, iic_equipment)
    BookingSampleTrace.objects.create(booking=received, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    for booking in (internal, cancelled, received):
        _age_booking(booking, 5)
    assert reminders.run() == {}


def test_reminder_wording_never_claims_loss():
    text = " ".join(str(m) for m in reminders.MESSAGES.values()).lower()
    assert "lost" not in text and "your fault" not in text
    assert "delayed" in text


# --- API access -------------------------------------------------------------------------------------------------------

def test_users_see_only_their_own_shipments(catalog, external_booking, external_user, iic_equipment, no_queue):
    mine = _courier(external_user, external_booking)
    other_user = make_user(UserType.EXTERNAL)
    theirs = _courier(other_user, make_booking(other_user, iic_equipment), number="D3010377228")
    listing = client_for(external_user).get(f"{API}/shipments/").data
    assert [r["id"] for r in listing["results"]] == [mine.pk]
    assert client_for(external_user).get(f"{API}/shipments/{theirs.pk}/").status_code == 404
    assert client_for(external_user).post(f"{API}/shipments/{theirs.pk}/refresh/").status_code == 404
    assert client_for(external_user).get(f"{API}/bookings/{theirs.booking_id}/arrival/").status_code == 404
    # Asking for the staff view as a non-staff user still returns only their own records.
    assert [r["id"] for r in client_for(external_user).get(f"{API}/shipments/?view=staff").data["results"]] == [mine.pk]


def test_staff_scope_and_filters(catalog, iic, external_user, no_queue):
    eq_a, eq_b = make_equipment(iic), make_equipment(iic)
    a = _courier(external_user, make_booking(external_user, eq_a), number="D301037701")
    b = _courier(external_user, make_booking(external_user, eq_b), number="D301037702")
    oic = make_user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq_a, manager=oic)
    rows = client_for(oic).get(f"{API}/shipments/?view=staff").data["results"]
    assert [r["id"] for r in rows] == [a.pk] and rows[0]["submitted_by"]["id"] == external_user.pk
    assert client_for(oic).get(f"{API}/shipments/{b.pk}/").status_code == 404
    admin = make_user(UserType.ADMIN, is_staff=True)
    assert client_for(admin).get(f"{API}/shipments/?view=staff").data["count"] == 2
    filtered = client_for(admin).get(f"{API}/shipments/?view=staff&equipment={eq_b.pk}&courier=dtdc&status=REGISTERED")
    assert [r["id"] for r in filtered.data["results"]] == [b.pk]
    finance = make_user(UserType.FINANCE)
    assert client_for(finance).get(f"{API}/shipments/?view=staff").data["count"] == 2  # external bookings


def test_staff_refresh_uses_staff_trigger_and_owner_cannot_see_problem_codes(catalog, external_booking, external_user,
                                                                            no_queue, provider):
    from .conftest import FakeResponse, error_payload

    arrival = _courier(external_user, external_booking)
    admin = make_user(UserType.ADMIN, is_staff=True)
    provider.queue(FakeResponse(400, error_payload("TRACKING_ERROR")))
    response = client_for(admin).post(f"{API}/shipments/{arrival.pk}/refresh/")
    assert response.status_code == 200 and response.data["checked"] is True
    assert response.data["arrival"]["problem"] == "TRACKING_ERROR"
    owner_view = client_for(external_user).get(f"{API}/shipments/{arrival.pk}/").data
    assert owner_view["problem"] == "details" and "TRACKING_ERROR" not in str(owner_view)


def test_admin_overview_and_settings_are_main_admin_only_and_audited(catalog):
    admin = make_user(UserType.ADMIN, is_staff=True)
    for user_type in (UserType.DEPT_ADMIN, UserType.MANAGER, UserType.FINANCE, UserType.EXTERNAL):
        user = make_user(user_type)
        assert client_for(user).get(f"{API}/admin/overview/").status_code == 403
        assert client_for(user).patch(f"{API}/admin/settings/", {"safety_reserve": 0, "reason": "abcde"},
                                      format="json").status_code == 403
    overview = client_for(admin).get(f"{API}/admin/overview/").data
    assert overview["quota"]["quota"] == 100 and overview["configured"] is False
    assert client_for(admin).patch(f"{API}/admin/settings/", {"safety_reserve": 5}, format="json").status_code == 400
    response = client_for(admin).patch(f"{API}/admin/settings/", {"safety_reserve": 5, "reason": "more headroom"},
                                       format="json")
    assert response.status_code == 200 and response.data["settings"]["safety_reserve"] == 5
    log = DepartmentModuleAuditLog.objects.get(action="courier.settings")
    assert log.old_value == {"safety_reserve": 10} and log.new_value == {"safety_reserve": 5} and log.actor == admin


def test_courier_list_endpoint_and_unavailable_catalog(provider, external_user, settings):
    from iic_booking.courier_tracking.models import Courier

    response = client_for(external_user).get(f"{API}/couriers/")  # empty catalog: refreshed (not quota-counted)
    assert response.status_code == 200 and response.data["count"] == 8
    assert provider.track_calls == []
    Courier.objects.all().delete()
    CourierTrackingSettings.objects.update(catalog_refreshed_at=None)
    from .conftest import FakeResponse

    provider.couriers_response = FakeResponse(503, None)
    response = client_for(external_user).get(f"{API}/couriers/")
    assert response.status_code == 503 and response.data["code"] == "catalog_unavailable"


def test_staff_search_covers_booking_equipment_user_and_number(catalog, iic, external_user, no_queue):
    eq = make_equipment(iic)
    arrival = _courier(external_user, make_booking(external_user, eq), number="D301037799")
    other = make_user(UserType.EXTERNAL)
    _courier(other, make_booking(other, make_equipment(iic)), number="D301037700")
    admin = make_user(UserType.ADMIN, is_staff=True)
    for text in (eq.code, external_user.email, "d3010 37799", arrival.booking.virtual_booking_id[-6:]):
        rows = client_for(admin).get(f"{API}/shipments/", {"view": "staff", "q": text}).data["results"]
        assert [r["id"] for r in rows] == [arrival.pk], text
