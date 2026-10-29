import threading

import pytest
from django.apps import apps as django_apps
from django.db import connection

from iic_booking.courier_tracking.constants import ArrivalMode, ShipmentStatus
from iic_booking.courier_tracking.models import SampleArrival, TrackingLookup
from iic_booking.department_modules import access as module_access
from iic_booking.department_modules.constants import ModuleKey
from iic_booking.department_modules.models import DepartmentModuleSetting
from iic_booking.equipment.models import BookingSampleTrace, BookingStatus, SampleTraceStatus
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_booking, make_department, make_equipment, make_user, set_tracking

pytestmark = pytest.mark.django_db
MODULE = ModuleKey.COURIER_TRACKING.value


def _url(booking):
    return f"/api/v1/courier-tracking/bookings/{booking.pk}/arrival/"


def _courier(**extra):
    return {"arrival_mode": "COURIER", "courier_slug": "dtdc", "tracking_number": "d 3010 377227",
            "email_updates": True, **extra}


# --- department enablement --------------------------------------------------------------------------------------------

def test_seed_turns_iic_on_and_every_other_department_off():
    from importlib import import_module

    migration = import_module("iic_booking.department_modules.migrations.0003_courier_tracking_module")
    iic = make_department(iic=True)
    chem = make_department()
    DepartmentModuleSetting._base_manager.filter(module_key=MODULE, department__in=[iic, chem]).delete()
    migration.seed_courier_tracking(django_apps, None)
    rows = {r.department_id: r for r in DepartmentModuleSetting.objects.filter(module_key=MODULE, department__in=[iic, chem])}
    assert rows[iic.pk].enabled is True and rows[chem.pk].enabled is False
    migration.seed_courier_tracking(django_apps, None)  # idempotent
    assert DepartmentModuleSetting.objects.filter(module_key=MODULE, department=iic).count() == 1


def test_department_without_a_row_is_off_unless_iic():
    iic = make_department(iic=True)
    chem = make_department()
    DepartmentModuleSetting._base_manager.filter(module_key=MODULE, department__in=[iic, chem]).delete()
    assert module_access.cell(iic.pk, MODULE).enabled is True
    assert module_access.cell(chem.pk, MODULE).enabled is False
    # The pre-switch default (on) still applies to the other modules.
    DepartmentModuleSetting._base_manager.filter(module_key=ModuleKey.DSA, department=chem).delete()


def test_main_admin_toggles_and_others_cannot(other_dept):
    admin = make_user(UserType.ADMIN, is_staff=True)
    url = f"/api/v1/admin/department-modules/{other_dept.pk}/{MODULE}/"
    assert client_for(admin).patch(url, {"enabled": True, "reason": "pilot start"}, format="json").status_code == 200
    assert module_access.cell(other_dept.pk, MODULE).enabled is True
    for user_type in (UserType.DEPT_ADMIN, UserType.MANAGER, UserType.FINANCE, UserType.EXTERNAL):
        response = client_for(make_user(user_type)).patch(url, {"enabled": False, "reason": "nope nope"}, format="json")
        assert response.status_code == 403
    assert module_access.cell(other_dept.pk, MODULE).enabled is True


# --- eligibility ------------------------------------------------------------------------------------------------------

def test_external_user_registers_courier_when_enabled(catalog, external_booking, external_user, no_queue):
    response = client_for(external_user).post(_url(external_booking), _courier(), format="json")
    assert response.status_code == 201, response.data
    arrival = SampleArrival.objects.get(booking=external_booking, is_active=True)
    assert arrival.arrival_mode == ArrivalMode.COURIER and arrival.courier_name == "DTDC"
    assert arrival.tracking_number_raw == "d 3010 377227" and arrival.tracking_number == "D3010377227"
    assert arrival.status == ShipmentStatus.REGISTERED and arrival.next_lookup_at is not None
    trace = BookingSampleTrace.objects.get(booking=external_booking)
    assert trace.status == SampleTraceStatus.SAMPLE_SENT and "D3010377227" in trace.tracking_id
    assert not TrackingLookup.objects.exists()  # registration never calls the provider in the request


def test_external_user_cannot_register_courier_when_department_disabled(catalog, external_user, other_dept):
    booking = make_booking(external_user, make_equipment(other_dept))
    get = client_for(external_user).get(_url(booking))
    assert get.data["eligibility"]["courier_allowed"] is False and get.data["eligibility"]["default_mode"] == "MANUAL"
    response = client_for(external_user).post(_url(booking), _courier(), format="json")
    assert response.status_code == 403 and response.data["code"] == "courier_not_allowed"
    manual = client_for(external_user).post(_url(booking), {"arrival_mode": "MANUAL"}, format="json")
    assert manual.status_code == 201


@pytest.mark.parametrize("user_type", [UserType.FACULTY, UserType.STUDENT, UserType.INDIVIDUAL_STUDENT,
                                       UserType.STARTUP_INCUBATED_IITR])
def test_internal_users_are_manual_only_even_in_iic(catalog, iic_equipment, provider, user_type):
    user = make_user(user_type)
    booking = make_booking(user, iic_equipment)
    elig = client_for(user).get(_url(booking)).data["eligibility"]
    assert elig["courier_allowed"] is False and elig["default_mode"] == "MANUAL" and elig["is_external"] is False
    assert client_for(user).post(_url(booking), _courier(), format="json").status_code == 403
    response = client_for(user).post(_url(booking), {"arrival_mode": "MANUAL", "notes": "bringing it"}, format="json")
    assert response.status_code == 201
    arrival = SampleArrival.objects.get(booking=booking)
    assert arrival.arrival_mode == ArrivalMode.MANUAL and arrival.courier_slug == "" and arrival.status == ""
    assert not BookingSampleTrace.objects.filter(booking=booking).exists()  # intent, not dispatch
    assert provider.calls == [] and not TrackingLookup.objects.exists()


def test_snapshot_and_current_type_must_both_be_external(catalog, iic_equipment):
    user = make_user(UserType.EXTERNAL)
    booking = make_booking(user, iic_equipment, snapshot=UserType.FACULTY)
    assert client_for(user).post(_url(booking), _courier(), format="json").status_code == 403


def test_other_users_and_staff_cannot_declare(catalog, external_booking):
    stranger = make_user(UserType.EXTERNAL)
    assert client_for(stranger).post(_url(external_booking), _courier(), format="json").status_code == 404
    admin = make_user(UserType.ADMIN, is_staff=True)
    assert client_for(admin).post(_url(external_booking), _courier(), format="json").status_code == 403


def test_closed_booking_cannot_declare(catalog, external_user, iic_equipment):
    booking = make_booking(external_user, iic_equipment, status=BookingStatus.CANCELLED)
    assert client_for(external_user).post(_url(booking), _courier(), format="json").status_code == 403


@pytest.mark.parametrize("payload,field", [
    ({"courier_slug": "my-own-courier"}, "courier_slug"),
    ({"courier_slug": ""}, "courier_slug"),
    ({"courier_slug": "speedpost", "tracking_number": "EE12345678IN"}, "tracking_number"),
    ({"tracking_number": ""}, "tracking_number"),
])
def test_unsupported_courier_or_bad_number_rejected(catalog, external_booking, external_user, payload, field):
    response = client_for(external_user).post(_url(external_booking), _courier(**payload), format="json")
    assert response.status_code == 400 and field in response.data["fields"]
    assert not SampleArrival.objects.exists()


def test_inactive_courier_cannot_be_chosen(catalog, external_booking, external_user):
    from iic_booking.courier_tracking.models import Courier

    Courier.objects.filter(slug="dtdc").update(is_active=False)
    assert client_for(external_user).post(_url(external_booking), _courier(), format="json").status_code == 400


# --- mode changes and duplicates --------------------------------------------------------------------------------------

def test_double_submit_keeps_one_active_record(catalog, external_booking, external_user, no_queue):
    for _ in range(2):
        assert client_for(external_user).post(_url(external_booking), _courier(), format="json").status_code == 201
    assert SampleArrival.objects.filter(booking=external_booking).count() == 1
    assert BookingSampleTrace.objects.filter(booking=external_booking).count() == 1


def test_courier_to_manual_keeps_history_and_stops_checks(catalog, external_booking, external_user, no_queue):
    client_for(external_user).post(_url(external_booking), _courier(), format="json")
    response = client_for(external_user).post(_url(external_booking), {"arrival_mode": "MANUAL"}, format="json")
    assert response.status_code == 201
    old = SampleArrival.objects.get(booking=external_booking, arrival_mode=ArrivalMode.COURIER)
    assert old.is_active is False and old.next_lookup_at is None and old.tracking_number == "D3010377227"
    data = client_for(external_user).get(_url(external_booking)).data
    assert data["active"]["arrival_mode"] == "MANUAL" and data["history"][0]["tracking_number"] == "D3010377227"


def test_manual_to_courier_revalidates_department(catalog, external_booking, external_user, iic):
    client_for(external_user).post(_url(external_booking), {"arrival_mode": "MANUAL"}, format="json")
    set_tracking(iic, False)
    assert client_for(external_user).post(_url(external_booking), _courier(), format="json").status_code == 403
    assert SampleArrival.objects.get(booking=external_booking, is_active=True).arrival_mode == ArrivalMode.MANUAL


@pytest.mark.django_db(transaction=True)
def test_concurrent_submissions_leave_one_active_record(catalog, external_user, iic, no_queue):
    booking = make_booking(external_user, make_equipment(iic))
    barrier = threading.Barrier(4)
    codes = []

    def worker(n):
        try:
            barrier.wait()
            payload = _courier(tracking_number=f"D30103772{n:02d}")
            codes.append(client_for(external_user).post(_url(booking), payload, format="json").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Serialised by the booking lock, so the per-booking change limit (3) holds even for simultaneous submissions.
    assert sorted(codes) == [201, 201, 201, 429]
    assert SampleArrival.objects.filter(booking=booking, is_active=True).count() == 1
    assert SampleArrival.objects.filter(booking=booking).count() == 3  # the rest kept as superseded history
