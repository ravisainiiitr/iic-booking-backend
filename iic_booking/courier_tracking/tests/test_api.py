from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.courier_tracking import services
from iic_booking.courier_tracking.models import SampleArrival
from iic_booking.equipment.models import Booking, EquipmentManager
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_booking, make_equipment, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/courier-tracking"


def _age_booking(booking, days):
    Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(days=days))


def _courier(user, booking, number="D3010377227"):
    return services.register_arrival(user, booking, services.ArrivalInput(
        arrival_mode="COURIER", courier_slug="dtdc", tracking_number=number))


# --- shipments --------------------------------------------------------------------------------------------------------

def test_users_see_only_their_own_shipments(catalog, external_booking, external_user, iic_equipment, no_queue):
    mine = _courier(external_user, external_booking)
    other_user = make_user(UserType.EXTERNAL)
    theirs = _courier(other_user, make_booking(other_user, iic_equipment), number="D3010377228")
    listing = client_for(external_user).get(f"{API}/shipments/").data
    assert [r["id"] for r in listing["results"]] == [mine.pk]
    assert client_for(external_user).get(f"{API}/shipments/{theirs.pk}/").status_code == 404
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


def test_the_built_in_courier_list_lets_users_enter_details(external_booking, external_user, no_queue):
    from iic_booking.courier_tracking.models import Courier

    Courier.objects.all().delete()
    response = client_for(external_user).get(f"{API}/couriers/")
    assert response.status_code == 200 and response.data["count"] == 9
    assert Courier.objects.filter(slug="professional", is_active=True).exists()
    response = client_for(external_user).post(
        f"{API}/bookings/{external_booking.pk}/arrival/",
        {"arrival_mode": "COURIER", "courier_slug": "speedpost", "tracking_number": "EE123456789IN"},
        format="json",
    )
    assert response.status_code in (200, 201), response.data
    assert SampleArrival.objects.filter(booking=external_booking, courier_slug="speedpost").exists()


def test_staff_search_covers_booking_equipment_user_and_number(catalog, iic, external_user, no_queue):
    eq = make_equipment(iic)
    arrival = _courier(external_user, make_booking(external_user, eq), number="D301037799")
    other = make_user(UserType.EXTERNAL)
    _courier(other, make_booking(other, make_equipment(iic)), number="D301037700")
    admin = make_user(UserType.ADMIN, is_staff=True)
    for text in (eq.code, external_user.email, "d3010 37799", arrival.booking.virtual_booking_id[-6:]):
        rows = client_for(admin).get(f"{API}/shipments/", {"view": "staff", "q": text}).data["results"]
        assert [r["id"] for r in rows] == [arrival.pk], text


def test_provider_endpoints_are_gone(catalog, external_booking, external_user, no_queue):
    arrival = _courier(external_user, external_booking)
    admin = make_user(UserType.ADMIN, is_staff=True)
    assert client_for(external_user).post(f"{API}/shipments/{arrival.pk}/refresh/").status_code == 404
    assert client_for(admin).get(f"{API}/admin/overview/").status_code == 404
    assert client_for().post(f"{API}/webhooks/trackcourier/", {}, format="json").status_code == 404
    body = client_for(external_user).get(f"{API}/shipments/{arrival.pk}/").data
    assert body["tracking_number"] == "D3010377227" and "status" not in body and "next_check_at" not in body


def test_retired_tasks_do_nothing():
    from iic_booking.courier_tracking import tasks

    for task in (tasks.lookup_task, tasks.scheduled_checks_task, tasks.sweep_task, tasks.reminders_task,
                 tasks.refresh_catalog_task, tasks.send_notifications_task, tasks.process_webhook_task):
        assert task.run(1, "x").startswith("retired")
